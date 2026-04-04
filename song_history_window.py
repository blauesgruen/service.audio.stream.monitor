"""
WindowXMLDialog for song history in addon settings.
"""
import xbmc
import xbmcgui

from constants import (
    SONG_HISTORY_ACTION_NAV_BACK,
    SONG_HISTORY_ACTION_PARENT_DIR,
    SONG_HISTORY_ACTION_PREVIOUS_MENU,
    SONG_HISTORY_CTRL_CLOSE_BUTTON,
    SONG_HISTORY_CTRL_SONG_LIST,
    SONG_HISTORY_CTRL_STATION_MENU,
    SONG_HISTORY_CTRL_SUMMARY_LABEL,
    SONG_HISTORY_SONG_LIMIT,
)


def _display_station_name(station_key):
    value = str(station_key or '').strip()
    if not value:
        return '-'
    if ':' in value:
        return value.split(':', 1)[1].strip() or value
    return value


class SongHistoryWindow(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self._stations = tuple(kwargs.pop('stations', ()) or ())
        self._db = kwargs.pop('db')
        self._day = str(kwargs.pop('day', ''))
        self._get_text = kwargs.pop('get_text')
        self._selected_index = 0
        self._station_menu = None
        self._song_list = None
        self._summary_label = None

    def onInit(self):
        self._station_menu = self.getControl(SONG_HISTORY_CTRL_STATION_MENU)
        self._song_list = self.getControl(SONG_HISTORY_CTRL_SONG_LIST)
        self._summary_label = self.getControl(SONG_HISTORY_CTRL_SUMMARY_LABEL)
        self._station_menu.setVisible(True)

        self._fill_station_menu()
        self._select_station(0)
        self.setFocusId(SONG_HISTORY_CTRL_STATION_MENU)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (
            SONG_HISTORY_ACTION_PREVIOUS_MENU,
            SONG_HISTORY_ACTION_PARENT_DIR,
            SONG_HISTORY_ACTION_NAV_BACK,
        ):
            if self._station_menu and self._station_menu.isVisible():
                self.close()
            else:
                self.close()
            return
        super().onAction(action)
        self._sync_station_selection()

    def onClick(self, control_id):
        if control_id == SONG_HISTORY_CTRL_CLOSE_BUTTON:
            self.close()
            return
        if control_id == SONG_HISTORY_CTRL_STATION_MENU:
            selected = self._station_menu.getSelectedPosition()
            self._select_station(selected)
            return
        self._sync_station_selection()

    def _fill_station_menu(self):
        self._station_menu.reset()
        for station in self._stations:
            name = _display_station_name(station.get('station_key', ''))
            self._station_menu.addItem(xbmcgui.ListItem(label=name))
        self._station_menu.selectItem(max(0, self._selected_index))
        xbmc.log(f"[Audio Stream Monitor] SongHistoryWindow stations={self._station_menu.size()}", xbmc.LOGINFO)

    def _sync_station_selection(self):
        if not self._station_menu or self.getFocusId() != SONG_HISTORY_CTRL_STATION_MENU:
            return
        selected = self._station_menu.getSelectedPosition()
        if selected != self._selected_index:
            self._select_station(selected)

    def _select_station(self, index):
        if not self._stations:
            return
        safe_index = min(max(0, int(index)), len(self._stations) - 1)
        self._selected_index = safe_index
        station = self._stations[safe_index]

        day_plays = int(station.get('day_plays') or 0)
        total_plays = int(station.get('total_plays') or 0)
        unique_songs = int(station.get('unique_songs') or 0)
        self._summary_label.setLabel(
            f"{self._get_text(32036)}: {day_plays}   |   "
            f"{self._get_text(32037)}: {total_plays}   |   "
            f"{self._get_text(32038)}: {unique_songs}"
        )
        self._fill_song_list(station_key=station.get('station_key', ''))

    def _fill_song_list(self, station_key):
        self._song_list.reset()
        rows = self._db.get_station_song_history(
            station_key=station_key,
            day=self._day,
            limit=SONG_HISTORY_SONG_LIMIT,
        )
        xbmc.log(
            f"[Audio Stream Monitor] SongHistoryWindow songs station='{station_key}' rows={len(rows)}",
            xbmc.LOGINFO,
        )
        if not rows:
            self._song_list.addItem(xbmcgui.ListItem(label=self._get_text(32035), label2=''))
            return

        for row in rows:
            artist = str(row.get('artist') or '').strip()
            title = str(row.get('title') or '').strip()
            day_count = int(row.get('day_count') or 0)
            total_count = int(row.get('total_count') or 0)
            last_seen = str(row.get('last_seen') or '').strip()
            main_label = f"{artist} - {title}" if artist and title else (artist or title or '-')
            secondary = (
                f"{self._get_text(32036)}: {day_count}   |   "
                f"{self._get_text(32037)}: {total_count}   |   "
                f"{self._get_text(32041)}: {last_seen}"
            )
            self._song_list.addItem(xbmcgui.ListItem(label=main_label, label2=secondary))
