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


class DiagnosticWindow(xbmcgui.WindowXMLDialog):
    def onClick(self, control_id):
        if control_id == 100:
            self.close()

    def onAction(self, action):
        # Back/Escape should close the modal diagnostic view as well.
        if action.getId() in (9, 10, 92):
            self.close()
            return
        super().onAction(action)


def show_diagnostics():
    addon = xbmcaddon.Addon(id='service.audio.stream.monitor')
    path = xbmcvfs.translatePath(addon.getAddonInfo('path'))
    window = DiagnosticWindow(
        DIAGNOSTIC_WINDOW_XML, path, DIAGNOSTIC_WINDOW_SKIN,
        DIAGNOSTIC_WINDOW_RESOLUTION,
    )
    window.doModal()
    del window
