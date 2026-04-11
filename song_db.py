"""
SQLite-based database for station-specific learning data.

Tables:
  songs             confirmed (artist, title) pairs per station (LRU cache)
  song_daily_counts per-day play counter for (station, artist, title)
  generic_strings   observed non-song texts with stats and promotion flag
  verified_station_sources
                    shared verified station->source mapping for ASM + ASM-QF
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from constants import (
    KEYWORD_PROMOTE_MIN_SEEN,
    SONG_CACHE_MAX_PER_STATION,
    SONG_RECOUNT_WINDOW_S,
    STATION_PROFILE_KEYWORD_STATS_MAX,
)
from logger import log_warning

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    station_key  TEXT NOT NULL,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_seen_ts INTEGER NOT NULL DEFAULT 0,
    count        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (station_key, artist, title)
);
CREATE TABLE IF NOT EXISTS generic_strings (
    station_key  TEXT NOT NULL,
    string       TEXT NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0,
    last_seen    TEXT NOT NULL,
    promoted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (station_key, string)
);
CREATE TABLE IF NOT EXISTS song_daily_counts (
    station_key  TEXT NOT NULL,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    day          TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (station_key, artist, title, day)
);
CREATE TABLE IF NOT EXISTS verified_station_sources (
    station_key       TEXT NOT NULL,
    station_name      TEXT NOT NULL DEFAULT '',
    station_name_norm TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL,
    source_url_norm   TEXT NOT NULL,
    source_kind       TEXT NOT NULL DEFAULT 'stream',
    verified_by       TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL DEFAULT 1.0,
    verified_at_utc   TEXT NOT NULL DEFAULT '',
    last_seen_ts      INTEGER NOT NULL DEFAULT 0,
    meta_json         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (station_key, source_url_norm)
);
CREATE INDEX IF NOT EXISTS idx_verified_sources_url_norm
ON verified_station_sources(source_url_norm);
CREATE INDEX IF NOT EXISTS idx_verified_sources_station_norm
ON verified_station_sources(station_name_norm);
"""


def _today():
    return datetime.utcnow().strftime('%Y-%m-%d')


def _utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


class SongDatabase:

    _SONG_SEPARATORS = (' - ', ' – ', ' — ', ' | ')

    def __init__(self, db_path):
        self._db_path = str(db_path or '')
        self._conn = None
        self._lock = threading.RLock()
        self._open()

    def _open(self):
        if not self._db_path:
            return
        with self._lock:
            try:
                dir_path = os.path.dirname(self._db_path)
                if dir_path:
                    os.makedirs(dir_path, exist_ok=True)
                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute('PRAGMA journal_mode=WAL')
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
                self._migrate()
            except Exception as e:
                self._conn = None
                log_warning(f"Song DB open fehlgeschlagen: {e}")

    @staticmethod
    def _looks_like_song(text):
        """True if the string looks like 'Artist - Title'."""
        value = str(text or '')
        for sep in SongDatabase._SONG_SEPARATORS:
            if sep in value:
                parts = value.split(sep, 1)
                if len(parts[0].strip()) >= 2 and len(parts[1].strip()) >= 2:
                    return True
        return False

    def _migrate(self):
        """Schema migrations for songs/generic_strings and cleanup."""
        songs_cursor = self._exec("PRAGMA table_info(songs)")
        if songs_cursor is not None:
            song_cols = {row['name'] for row in songs_cursor.fetchall()}
            if 'last_seen_ts' not in song_cols:
                self._exec("ALTER TABLE songs ADD COLUMN last_seen_ts INTEGER NOT NULL DEFAULT 0")
                self._commit()

        self._migrate_verified_sources_table()

        cursor = self._exec("PRAGMA table_info(generic_strings)")
        if cursor is None:
            return
        cols = {row['name'] for row in cursor.fetchall()}
        if 'seen_generic' in cols or 'seen_song' in cols:
            self._exec("DROP TABLE IF EXISTS generic_strings")
            self._exec("""
                CREATE TABLE IF NOT EXISTS generic_strings (
                    station_key  TEXT NOT NULL,
                    string       TEXT NOT NULL,
                    seen         INTEGER NOT NULL DEFAULT 0,
                    last_seen    TEXT NOT NULL,
                    promoted     INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (station_key, string)
                )
            """)
            self._commit()
            return

        # Remove wrongly stored song-like strings from generic table.
        cursor = self._exec("SELECT rowid, string FROM generic_strings")
        if cursor is None:
            return
        rows = cursor.fetchall()
        song_rowids = [row['rowid'] for row in rows if self._looks_like_song(str(row['string'] or ''))]
        if song_rowids:
            placeholders = ','.join('?' for _ in song_rowids)
            self._exec(f"DELETE FROM generic_strings WHERE rowid IN ({placeholders})", song_rowids)
            self._commit()

    def _migrate_verified_sources_table(self):
        cursor = self._exec("PRAGMA table_info(verified_station_sources)")
        if cursor is None:
            return
        cols = {row['name'] for row in cursor.fetchall()}
        if not cols:
            return

        additions = (
            ('station_name', "ALTER TABLE verified_station_sources ADD COLUMN station_name TEXT NOT NULL DEFAULT ''"),
            ('station_name_norm', "ALTER TABLE verified_station_sources ADD COLUMN station_name_norm TEXT NOT NULL DEFAULT ''"),
            ('source_url_norm', "ALTER TABLE verified_station_sources ADD COLUMN source_url_norm TEXT NOT NULL DEFAULT ''"),
            ('source_kind', "ALTER TABLE verified_station_sources ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'stream'"),
            ('verified_by', "ALTER TABLE verified_station_sources ADD COLUMN verified_by TEXT NOT NULL DEFAULT ''"),
            ('confidence', "ALTER TABLE verified_station_sources ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0"),
            ('verified_at_utc', "ALTER TABLE verified_station_sources ADD COLUMN verified_at_utc TEXT NOT NULL DEFAULT ''"),
            ('last_seen_ts', "ALTER TABLE verified_station_sources ADD COLUMN last_seen_ts INTEGER NOT NULL DEFAULT 0"),
            ('meta_json', "ALTER TABLE verified_station_sources ADD COLUMN meta_json TEXT NOT NULL DEFAULT ''"),
        )
        changed = False
        for col_name, ddl in additions:
            if col_name not in cols:
                self._exec(ddl)
                changed = True

        self._exec("""
            UPDATE verified_station_sources
            SET station_name_norm = LOWER(TRIM(station_name))
            WHERE station_name_norm = ''
        """)
        self._exec("""
            UPDATE verified_station_sources
            SET source_url_norm = LOWER(TRIM(source_url))
            WHERE source_url_norm = ''
        """)
        self._exec("""
            CREATE INDEX IF NOT EXISTS idx_verified_sources_url_norm
            ON verified_station_sources(source_url_norm)
        """)
        self._exec("""
            CREATE INDEX IF NOT EXISTS idx_verified_sources_station_norm
            ON verified_station_sources(station_name_norm)
        """)
        if changed:
            self._commit()

    def _exec(self, sql, params=()):
        with self._lock:
            if self._conn is None:
                return None
            try:
                return self._conn.execute(sql, params)
            except Exception as e:
                op = str(sql or '').strip().split('\n', 1)[0][:80]
                log_warning(f"Song DB exec fehlgeschlagen ({op}): {e}")
                return None

    def _commit(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except Exception as e:
                    log_warning(f"Song DB commit fehlgeschlagen: {e}")

    # --- Songs ---

    def _is_recent_song_duplicate(self, station_key, artist, title, now_ts, window_s):
        if window_s <= 0:
            return False
        recent_cursor = self._exec(
            """
            SELECT last_seen_ts
            FROM songs
            WHERE station_key = ? AND artist = ? AND title = ?
            """,
            (station_key, artist, title)
        )
        if recent_cursor is None:
            return False
        existing = recent_cursor.fetchone()
        if existing is None:
            return False
        last_seen_ts = int(existing['last_seen_ts'] or 0)
        return last_seen_ts > 0 and (now_ts - last_seen_ts) < window_s

    def _touch_song_last_seen(self, station_key, artist, title, day_value, now_ts):
        touch_cursor = self._exec(
            """
            UPDATE songs
            SET last_seen = ?, last_seen_ts = ?
            WHERE station_key = ? AND artist = ? AND title = ?
            """,
            (day_value, now_ts, station_key, artist, title)
        )
        if touch_cursor is None:
            log_warning(
                f"Song DB touch fehlgeschlagen: station='{station_key}', artist='{artist}', title='{title}'"
            )
        self._commit()

    def _upsert_song_counter(self, station_key, artist, title, day_value, now_ts):
        return self._exec(
            """
            INSERT INTO songs (station_key, artist, title, last_seen, last_seen_ts, count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(station_key, artist, title)
            DO UPDATE SET
                last_seen = excluded.last_seen,
                last_seen_ts = excluded.last_seen_ts,
                count = count + 1
            """,
            (station_key, artist, title, day_value, now_ts)
        )

    def _upsert_daily_counter(self, station_key, artist, title, day_value):
        return self._exec(
            """
            INSERT INTO song_daily_counts (station_key, artist, title, day, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(station_key, artist, title, day)
            DO UPDATE SET count = count + 1
            """,
            (station_key, artist, title, day_value)
        )

    def record_song(self, station_key, artist, title):
        if not station_key or not artist or not title:
            return
        a = str(artist).strip().lower()
        t = str(title).strip().lower()
        if not a or not t:
            return

        now_ts = int(time.time())
        today = _today()
        recount_window_s = max(0, int(SONG_RECOUNT_WINDOW_S or 0))

        if self._is_recent_song_duplicate(station_key, a, t, now_ts, recount_window_s):
            self._touch_song_last_seen(station_key, a, t, today, now_ts)
            return

        song_cursor = self._upsert_song_counter(station_key, a, t, today, now_ts)
        daily_cursor = self._upsert_daily_counter(station_key, a, t, today)
        if song_cursor is None or daily_cursor is None:
            log_warning(
                f"Song DB write unvollstaendig: station='{station_key}', artist='{a}', title='{t}'"
            )
            return
        self._commit()
        self._evict_songs(station_key)

    def _evict_songs(self, station_key):
        limit = int(SONG_CACHE_MAX_PER_STATION)
        self._exec("""
            DELETE FROM songs
            WHERE station_key = ?
              AND rowid NOT IN (
                  SELECT rowid FROM songs
                  WHERE station_key = ?
                  ORDER BY last_seen_ts DESC, last_seen DESC
                  LIMIT ?
              )
        """, (station_key, station_key, limit))
        self._commit()

    def get_known_songs(self, station_key):
        if not station_key:
            return frozenset()
        cursor = self._exec(
            "SELECT artist, title FROM songs WHERE station_key = ?",
            (station_key,)
        )
        if cursor is None:
            return frozenset()
        return frozenset((row['artist'], row['title']) for row in cursor.fetchall())

    def get_station_overview(self, day=None, limit=80):
        """Aggregated station stats including per-day play count."""
        target_day = str(day or _today())
        max_rows = max(1, int(limit or 80))
        cursor = self._exec("""
            SELECT
                s.station_key AS station_key,
                COUNT(*) AS unique_songs,
                COALESCE(SUM(s.count), 0) AS total_plays,
                COALESCE((
                    SELECT SUM(d.count)
                    FROM song_daily_counts d
                    WHERE d.station_key = s.station_key
                      AND d.day = ?
                ), 0) AS day_plays
            FROM songs s
            GROUP BY s.station_key
            ORDER BY day_plays DESC, total_plays DESC, station_key ASC
            LIMIT ?
        """, (target_day, max_rows))
        if cursor is None:
            return ()
        return tuple(dict(row) for row in cursor.fetchall())

    def get_station_song_history(self, station_key, day=None, limit=250):
        """Songs for one station with total and per-day counters."""
        if not station_key:
            return ()
        target_day = str(day or _today())
        max_rows = max(1, int(limit or 250))
        cursor = self._exec("""
            SELECT
                s.artist AS artist,
                s.title AS title,
                s.count AS total_count,
                s.last_seen AS last_seen,
                COALESCE(d.count, 0) AS day_count
            FROM songs s
            LEFT JOIN song_daily_counts d
              ON d.station_key = s.station_key
             AND d.artist = s.artist
             AND d.title = s.title
             AND d.day = ?
            WHERE s.station_key = ?
            ORDER BY day_count DESC, total_count DESC, last_seen DESC, artist ASC, title ASC
            LIMIT ?
        """, (target_day, str(station_key), max_rows))
        if cursor is None:
            return ()
        return tuple(dict(row) for row in cursor.fetchall())

    # --- Verified Station Sources ---

    @staticmethod
    def _normalize_station_key(station_key):
        return str(station_key or '').strip().lower()

    @staticmethod
    def _normalize_station_name(station_name):
        return ' '.join(str(station_name or '').strip().lower().split())

    @staticmethod
    def _normalize_source_url(source_url):
        return str(source_url or '').strip().lower()

    @staticmethod
    def _encode_meta_json(meta):
        if meta is None:
            return ''
        if isinstance(meta, str):
            return meta.strip()
        try:
            return json.dumps(meta, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            return str(meta)

    def record_verified_source(
        self,
        station_key,
        source_url,
        station_name='',
        source_kind='stream',
        verified_by='',
        confidence=1.0,
        meta=None,
    ):
        key = self._normalize_station_key(station_key)
        source_url_raw = str(source_url or '').strip()
        source_url_norm = self._normalize_source_url(source_url_raw)
        if not key or not source_url_raw or not source_url_norm:
            return False

        name = str(station_name or '').strip()
        name_norm = self._normalize_station_name(name)
        kind = str(source_kind or 'stream').strip().lower() or 'stream'
        verifier = str(verified_by or '').strip().lower()
        meta_json = self._encode_meta_json(meta)
        now_ts = int(time.time())
        verified_at_utc = _utc_iso()
        try:
            conf_value = float(confidence)
        except Exception:
            conf_value = 1.0
        conf_value = max(0.0, min(1.0, conf_value))

        cursor = self._exec(
            """
            INSERT INTO verified_station_sources (
                station_key, station_name, station_name_norm,
                source_url, source_url_norm, source_kind,
                verified_by, confidence, verified_at_utc, last_seen_ts, meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_key, source_url_norm)
            DO UPDATE SET
                station_name = CASE
                    WHEN excluded.station_name <> '' THEN excluded.station_name
                    ELSE verified_station_sources.station_name
                END,
                station_name_norm = CASE
                    WHEN excluded.station_name_norm <> '' THEN excluded.station_name_norm
                    ELSE verified_station_sources.station_name_norm
                END,
                source_url = excluded.source_url,
                source_kind = excluded.source_kind,
                verified_by = CASE
                    WHEN excluded.verified_by <> '' THEN excluded.verified_by
                    ELSE verified_station_sources.verified_by
                END,
                confidence = excluded.confidence,
                verified_at_utc = excluded.verified_at_utc,
                last_seen_ts = excluded.last_seen_ts,
                meta_json = CASE
                    WHEN excluded.meta_json <> '' THEN excluded.meta_json
                    ELSE verified_station_sources.meta_json
                END
            """,
            (
                key, name, name_norm,
                source_url_raw, source_url_norm, kind,
                verifier, conf_value, verified_at_utc, now_ts, meta_json
            )
        )
        if cursor is None:
            log_warning(
                f"Verified source write fehlgeschlagen: station='{key}', url='{source_url_raw}'"
            )
            return False
        self._commit()
        return True

    def get_verified_source_by_url(self, source_url):
        source_url_norm = self._normalize_source_url(source_url)
        if not source_url_norm:
            return None
        cursor = self._exec(
            """
            SELECT
                station_key, station_name, station_name_norm,
                source_url, source_url_norm, source_kind,
                verified_by, confidence, verified_at_utc, last_seen_ts, meta_json
            FROM verified_station_sources
            WHERE source_url_norm = ?
            ORDER BY confidence DESC, last_seen_ts DESC
            LIMIT 1
            """,
            (source_url_norm,)
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_verified_sources_for_station(self, station_key='', station_name='', limit=50):
        station_key_norm = self._normalize_station_key(station_key)
        station_name_norm = self._normalize_station_name(station_name)
        max_rows = max(1, int(limit or 50))
        if station_key_norm:
            cursor = self._exec(
                """
                SELECT
                    station_key, station_name, station_name_norm,
                    source_url, source_url_norm, source_kind,
                    verified_by, confidence, verified_at_utc, last_seen_ts, meta_json
                FROM verified_station_sources
                WHERE station_key = ?
                ORDER BY confidence DESC, last_seen_ts DESC, source_url ASC
                LIMIT ?
                """,
                (station_key_norm, max_rows)
            )
        elif station_name_norm:
            cursor = self._exec(
                """
                SELECT
                    station_key, station_name, station_name_norm,
                    source_url, source_url_norm, source_kind,
                    verified_by, confidence, verified_at_utc, last_seen_ts, meta_json
                FROM verified_station_sources
                WHERE station_name_norm = ?
                ORDER BY confidence DESC, last_seen_ts DESC, source_url ASC
                LIMIT ?
                """,
                (station_name_norm, max_rows)
            )
        else:
            return ()
        if cursor is None:
            return ()
        return tuple(dict(row) for row in cursor.fetchall())

    # --- Generic Strings ---

    def record_string_candidates(self, station_key, candidates):
        if not station_key or not candidates:
            return
        today = _today()
        for candidate in candidates:
            token = str(candidate or '').strip().lower()
            if not token:
                continue
            self._exec("""
                INSERT INTO generic_strings (station_key, string, seen, last_seen)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(station_key, string)
                DO UPDATE SET seen=seen+1, last_seen=excluded.last_seen
            """, (station_key, token, today))
        self._commit()
        self._evict_strings(station_key)

    def _evict_strings(self, station_key):
        limit = int(STATION_PROFILE_KEYWORD_STATS_MAX)
        # check only when needed to avoid unnecessary DELETEs
        cursor = self._exec(
            "SELECT COUNT(*) FROM generic_strings WHERE station_key = ? AND promoted = 0",
            (station_key,)
        )
        if cursor is None:
            return
        row = cursor.fetchone()
        if row is None or row[0] <= limit:
            return
        self._exec("""
            DELETE FROM generic_strings
            WHERE station_key = ? AND promoted = 0
              AND rowid NOT IN (
                  SELECT rowid FROM generic_strings
                  WHERE station_key = ? AND promoted = 0
                  ORDER BY seen DESC, last_seen DESC
                  LIMIT ?
              )
        """, (station_key, station_key, limit))
        self._commit()

    def promote_strings(self, station_key):
        if not station_key:
            return
        min_seen = int(KEYWORD_PROMOTE_MIN_SEEN)
        self._exec("""
            UPDATE generic_strings
            SET promoted = 1
            WHERE station_key = ? AND seen >= ?
        """, (station_key, min_seen))
        self._commit()

    def get_generic_strings(self, station_key):
        if not station_key:
            return ()
        cursor = self._exec(
            "SELECT string FROM generic_strings WHERE station_key = ? AND promoted = 1",
            (station_key,)
        )
        if cursor is None:
            return ()
        return tuple(row['string'] for row in cursor.fetchall())

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as e:
                    log_warning(f"Song DB close fehlgeschlagen: {e}")
                self._conn = None
