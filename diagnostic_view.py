"""Live diagnostics window for the Audio Stream Monitor."""
import os
import sys

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE and _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from constants import DIAGNOSTIC_WINDOW_XML, DIAGNOSTIC_WINDOW_SKIN, DIAGNOSTIC_WINDOW_RESOLUTION


def show_diagnostics():
    addon = xbmcaddon.Addon(id='service.audio.stream.monitor')
    path = xbmcvfs.translatePath(addon.getAddonInfo('path'))
    window = xbmcgui.WindowXMLDialog(
        DIAGNOSTIC_WINDOW_XML, path, DIAGNOSTIC_WINDOW_SKIN,
        DIAGNOSTIC_WINDOW_RESOLUTION,
    )
    window.doModal()
    del window
