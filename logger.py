"""
Logging-Wrapper für das Audio Stream Monitor Addon.

Zentralisiert xbmc.log-Aufrufe mit automatischem Addon-Präfix.
"""
import xbmc
from constants import ADDON_NAME, ADDON


def _as_bool(value):
    """Robustes Bool-Parsing fuer Kodi-Setting-Strings."""
    text = str(value or "").strip().lower()
    return text in ("true", "1", "yes", "on")


def _is_debug_logging_enabled():
    """
    Liefert True, wenn Debug-Logging aktiv ist.
    Prioritaet:
    1) Addon-Setting 'debug_logging' (falls vorhanden)
    2) Kodi globales Debug-Flag
    """
    # Wichtig: getSettingBool() kann in Kodi "Invalid setting type" ins Log schreiben,
    # wenn das Setting fehlt oder anders typisiert ist. Deshalb nur String-API nutzen.
    try:
        if _as_bool(ADDON.getSetting('debug_logging')):
            return True
    except Exception:
        pass

    try:
        return bool(xbmc.getCondVisibility('System.GetBool(debug.showloginfo)'))
    except Exception:
        return False


def log_debug(msg):
    """Log-Nachricht mit Debug-Level."""
    if _is_debug_logging_enabled():
        xbmc.log(f"[{ADDON_NAME}] {msg}", xbmc.LOGDEBUG)


def log_info(msg):
    """Log-Nachricht mit Info-Level."""
    xbmc.log(f"[{ADDON_NAME}] {msg}", xbmc.LOGINFO)


def log_warning(msg):
    """Log-Nachricht mit Warning-Level."""
    xbmc.log(f"[{ADDON_NAME}] {msg}", xbmc.LOGWARNING)


def log_error(msg):
    """Log-Nachricht mit Error-Level."""
    xbmc.log(f"[{ADDON_NAME}] {msg}", xbmc.LOGERROR)
