"""
SQLite-based database for station-specific learning data.

Tables:
  songs             confirmed (artist, title) pairs per station (LRU cache)
  song_daily_counts per-day play counter for (station, artist, title)
  generic_strings   observed non-song texts with stats and promotion flag
"""
import os
import sqlite3
from datetime import datetime

from constants import (
    KEYWORD_PROMOTE_MIN_SEEN,
    SONG_CACHE_MAX_PER_STATION,
    STATION_PROFILE_KEYWORD_STATS_MAX,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    station_key  TEXT NOT NULL,
    artist       TEXT NOT NULL,
    title        TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
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
"""


def _today():
    return datetime.utcnow().strftime('%Y-%m-%d')


class SongDatabase:

    _SONG_SEPARATORS = (' - ', ' – ', ' — ', ' | ')

    def __init__(self, db_path):
        self._db_path = str(db_path or '')
        self._conn = None
        self._open()

    def _open(self):
        if not self._db_path:
            return
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
        except Exception:
            self._conn = None

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
        """Schema migrations for generic_strings and cleanup."""
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

    def _exec(self, sql, params=()):
        if self._conn is None:
            return None
        try:
            return self._conn.execute(sql, params)
        except Exception:
            return None

    def _commit(self):
        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:
                pass

    # --- Songs ---

    def record_song(self, station_key, artist, title):
        if not station_key or not artist or not title:
            return
        a = str(artist).strip().lower()
        t = str(title).strip().lower()
        if not a or not t:
            return

        today = _today()
        self._exec("""
            INSERT INTO songs (station_key, artist, title, last_seen, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(station_key, artist, title)
            DO UPDATE SET last_seen = excluded.last_seen, count = count + 1
        """, (station_key, a, t, today))
        self._exec("""
            INSERT INTO song_daily_counts (station_key, artist, title, day, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(station_key, artist, title, day)
            DO UPDATE SET count = count + 1
        """, (station_key, a, t, today))
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
                  ORDER BY last_seen DESC
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
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
