"""
Cache-Management für MusicBrainz-Abfragen.

Thread-safe Cache mit TTL (Time-To-Live) für Song-Metadaten.
"""
import time
import threading
from typing import Optional, Tuple
from logger import log_debug


class MusicBrainzCache:
    """
    Thread-safe Cache für MusicBrainz Song-Abfragen.

    Cache-Key: (title_lower, artist_lower)
    Cache-Value: (result_tuple, timestamp)
    """

    def __init__(self, ttl: int = 86400):
        """
        Args:
            ttl: Time-To-Live in Sekunden (Standard: 24 Stunden)
        """
        self._cache = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, title: str, artist: str) -> Optional[Tuple]:
        """
        Holt Ergebnis aus dem Cache wenn vorhanden und nicht abgelaufen.

        Args:
            title: Song-Titel
            artist: Künstler-Name

        Returns:
            Gecachtes Ergebnis-Tuple oder None
        """
        cache_key = (title.lower().strip(), artist.lower().strip())

        with self._lock:
            cached = self._cache.get(cache_key)

            if cached:
                result, timestamp = cached
                if time.time() - timestamp < self._ttl:
                    log_debug(f"MB Song-Cache Treffer: '{title}' / '{artist}'")
                    return result
                else:
                    # Abgelaufen → entfernen
                    del self._cache[cache_key]

        return None

    def set(self, title: str, artist: str, result: Tuple):
        """
        Speichert Ergebnis im Cache.
        Loest nach jeweils 50 Eintraegen eine Bereinigung abgelaufener Eintraege aus.

        Args:
            title: Song-Titel
            artist: Künstler-Name
            result: Zu cachendes Ergebnis-Tuple
        """
        cache_key = (title.lower().strip(), artist.lower().strip())

        with self._lock:
            self._cache[cache_key] = (result, time.time())
            if len(self._cache) % 50 == 0:
                self._cleanup_expired_unlocked()

    def _cleanup_expired_unlocked(self):
        """Entfernt abgelaufene Cache-Eintraege (muss unter _lock aufgerufen werden)."""
        now = time.time()
        expired_keys = [
            key for key, (_, timestamp) in self._cache.items()
            if now - timestamp >= self._ttl
        ]
        for key in expired_keys:
            del self._cache[key]
        if expired_keys:
            log_debug(f"Cache Cleanup: {len(expired_keys)} abgelaufene Eintraege entfernt")

    def clear(self):
        """Leert den gesamten Cache."""
        with self._lock:
            self._cache.clear()
            log_debug("Cache komplett geleert")

    def size(self) -> int:
        """Gibt die Anzahl der gecachten Einträge zurück."""
        with self._lock:
            return len(self._cache)
