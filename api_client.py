"""
HTTP-Client für API-Anfragen mit Retry-Logik.

Unterstützt persistente Sessions, Exponential-Backoff und automatische Retries.
"""
import time
import random
import requests
from typing import Optional, Dict, Any
from logger import log_warning


class APIClient:
    """
    HTTP-Client mit automatischen Retries und Exponential-Backoff.

    Features:
    - Persistente TCP-Verbindungen (Session)
    - Exponential-Backoff mit Jitter bei Fehlern
    - Konfigurierbares Retry-Verhalten
    """

    def __init__(self, headers: Optional[Dict[str, str]] = None, retry_count: int = 3):
        """
        Args:
            headers: Standard-Headers für alle Requests
            retry_count: Anzahl der Versuche bei Fehlern (Standard: 3)
        """
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)
        self.retry_count = retry_count

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 5,
        **request_kwargs
    ) -> requests.Response:
        """
        Führt GET-Request mit automatischen Retries durch.

        Args:
            url: Ziel-URL
            params: Query-Parameter
            timeout: Timeout in Sekunden

        Returns:
            Response-Objekt

        Raises:
            Exception: Bei dauerhaftem Fehler nach allen Retries
        """
        last_exc = Exception("GET: kein Versuch")

        for attempt in range(self.retry_count):
            try:
                response = self.session.get(url, params=params, timeout=timeout, **request_kwargs)
                response.raise_for_status()
                return response

            except Exception as e:
                last_exc = e

                if attempt < self.retry_count - 1:
                    # Exponential-Backoff mit Jitter
                    wait = 2 ** attempt + random.uniform(0, 1)
                    log_warning(f"API-GET Retry {attempt+1}/{self.retry_count} in {wait:.1f}s: {e}")
                    time.sleep(wait)

        raise last_exc

    def head(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 5) -> requests.Response:
        """
        Führt HEAD-Request mit automatischen Retries durch.

        Args:
            url: Ziel-URL
            params: Query-Parameter
            timeout: Timeout in Sekunden

        Returns:
            Response-Objekt

        Raises:
            Exception: Bei dauerhaftem Fehler nach allen Retries
        """
        last_exc = Exception("HEAD: kein Versuch")

        for attempt in range(self.retry_count):
            try:
                response = self.session.head(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response

            except Exception as e:
                last_exc = e

                if attempt < self.retry_count - 1:
                    wait = 2 ** attempt + random.uniform(0, 1)
                    log_warning(f"API-HEAD Retry {attempt+1}/{self.retry_count} in {wait:.1f}s: {e}")
                    time.sleep(wait)

        raise last_exc

    def close(self):
        """Schließt die Session und gibt Ressourcen frei."""
        self.session.close()
