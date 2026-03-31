"""
SQLite-basierte Datenbank für senderspezifische Lerndaten.

Tabellen:
  songs           — bestätigte (artist, title)-Pairs pro Sender (LRU-Cache)
  generic_strings — beobachtete Nicht-Song-Texte mit Statistiken und Promotion-Flag
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
"""


def _today():
    return datetime.utcnow().strftime('%Y-%m-%d')


class SongDatabase:

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

    def _migrate(self):
        """Baut generic_strings neu auf wenn veraltetes Schema erkannt wird."""
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
        self._exec("""
            INSERT INTO songs (station_key, artist, title, last_seen, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(station_key, artist, title)
            DO UPDATE SET last_seen = excluded.last_seen, count = count + 1
        """, (station_key, a, t, _today()))
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
        # Nur prüfen wenn nötig, um unnötige DELETEs zu vermeiden
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
