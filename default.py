import sys
import os

import xbmc
import xbmcgui

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE:
    try:
        while _HERE in sys.path:
            sys.path.remove(_HERE)
    except Exception:
        pass
    sys.path.insert(0, _HERE)

from song_history_view import run


if __name__ == '__main__':
    try:
        xbmc.log('[Audio Stream Monitor] Song history action gestartet', xbmc.LOGINFO)
        run(sys.argv[1:])
    except Exception as exc:
        xbmc.log(f'[Audio Stream Monitor] Song history action Fehler: {exc}', xbmc.LOGERROR)
        xbmcgui.Dialog().notification('Audio Stream Monitor', f'Songverlauf Fehler: {exc}', xbmcgui.NOTIFICATION_ERROR, 6000)
