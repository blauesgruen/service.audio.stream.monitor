"""
Metadata Parsing & Normalization - Public Interface.

Exportiert Funktionen zum Parsen und Normalisieren von Stream-Metadaten.
"""
import xbmc
from constants import ADDON_NAME


# --- Public API ---

def parse_stream_title(stream_title, station_name=None):
    """
    PUBLIC API: Parst einen Stream-Titel und versucht Artist/Title zu trennen.

    Args:
        stream_title: ICY-Metadaten StreamTitle
        station_name: Optional - Stationsname

    Returns:
        (artist, title) - beide können None sein
    """
    # Placeholder: Wird im nächsten Schritt mit echtem Code gefüllt
    xbmc.log(f"[{ADDON_NAME}] parse_stream_title: '{stream_title}' (Placeholder)", xbmc.LOGDEBUG)

    if not stream_title or ' - ' not in stream_title:
        return None, stream_title

    parts = stream_title.split(' - ', 1)
    return parts[0].strip(), parts[1].strip()


# --- Exports ---

__all__ = ['parse_stream_title']
