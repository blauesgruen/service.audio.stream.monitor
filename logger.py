"""
Logging-Wrapper für das Audio Stream Monitor Addon.

Zentralisiert xbmc.log-Aufrufe mit automatischem Addon-Präfix.
"""
import xbmc
from constants import ADDON_NAME


def log_debug(msg):
    """Log-Nachricht mit Debug-Level."""
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
