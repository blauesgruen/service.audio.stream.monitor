"""
UI entry point for song history in addon settings.
"""
import os
import sys
from datetime import datetime

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE:
    try:
        while _HERE in sys.path:
            sys.path.remove(_HERE)
    except Exception:
        pass
    sys.path.insert(0, _HERE)

from constants import SONG_DB_FILENAME, SONG_HISTORY_SONG_LIMIT, SONG_HISTORY_STATION_LIMIT
from song_db import SongDatabase


try:
    ADDON = xbmcaddon.Addon()
except Exception:
    ADDON = xbmcaddon.Addon(id='service.audio.stream.monitor')
ADDON_ID = ADDON.getAddonInfo('id')


def _get_text(msg_id):
    try:
        return ADDON.getLocalizedString(int(msg_id))
    except Exception:
        return str(msg_id)


def _profile_path():
    try:
        return xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
    except Exception:
        return ''


def _song_db_path():
    base = _profile_path()
    if not base:
        base = os.path.join(os.path.dirname(__file__), 'profile')
    return os.path.join(base, SONG_DB_FILENAME)


def _display_station_name(station_key):
    value = str(station_key or '').strip()
    if not value:
        return '-'
    if ':' in value:
        return value.split(':', 1)[1].strip() or value
    return value


def _render_station_rows(rows):
    labels = []
    for row in rows:
        name = _display_station_name(row.get('station_key', ''))
        day_plays = int(row.get('day_plays') or 0)
        total_plays = int(row.get('total_plays') or 0)
        unique_songs = int(row.get('unique_songs') or 0)
        labels.append(
            f"{name}  |  {_get_text(32036)}: {day_plays}  |  {_get_text(32037)}: {total_plays}  |  {_get_text(32038)}: {unique_songs}"
        )
    return labels


def _render_song_history(station_key, rows, day):
    station_name = _display_station_name(station_key)
    lines = [
        f"{_get_text(32039)}: {station_name}",
        f"{_get_text(32040)}: {day}",
        ""
    ]
    for idx, row in enumerate(rows, start=1):
        artist = str(row.get('artist') or '').strip()
        title = str(row.get('title') or '').strip()
        if not artist and not title:
            continue
        day_count = int(row.get('day_count') or 0)
        total_count = int(row.get('total_count') or 0)
        last_seen = str(row.get('last_seen') or '').strip()
        lines.append(
            f"{idx:>3}. {artist} - {title}  |  {_get_text(32036)}: {day_count}  |  {_get_text(32037)}: {total_count}  |  {_get_text(32041)}: {last_seen}"
        )
    return "\n".join(lines)


def show_song_history(day=None):
    target_day = str(day or datetime.utcnow().strftime('%Y-%m-%d'))
    db = SongDatabase(_song_db_path())
    try:
        stations = db.get_station_overview(day=target_day, limit=SONG_HISTORY_STATION_LIMIT)
        if not stations:
            xbmcgui.Dialog().ok(_get_text(32032), _get_text(32033))
            return

        labels = _render_station_rows(stations)
        selected = xbmcgui.Dialog().select(_get_text(32034), labels)
        if selected < 0:
            return

        station_key = stations[selected].get('station_key', '')
        rows = db.get_station_song_history(
            station_key=station_key,
            day=target_day,
            limit=SONG_HISTORY_SONG_LIMIT
        )
        if not rows:
            xbmcgui.Dialog().ok(_get_text(32032), _get_text(32035))
            return

        content = _render_song_history(station_key, rows, target_day)
        xbmcgui.Dialog().textviewer(_get_text(32032), content, usemono=True)
    finally:
        db.close()


def run(argv):
    try:
        args = [str(v).strip() for v in (argv or []) if str(v).strip()]
        xbmc.log(f"[Audio Stream Monitor] Song history run args={args}", xbmc.LOGINFO)
        if not args or args[0] == 'show_song_history':
            show_song_history()
    except Exception as exc:
        xbmc.log(f"[Audio Stream Monitor] Song history run error: {exc}", xbmc.LOGERROR)
        xbmcgui.Dialog().ok(_get_text(32032), f"{_get_text(32042)}\n{exc}")


if __name__ == '__main__':
    import sys
    run(sys.argv[1:])
