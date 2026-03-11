import xbmc
import xbmcgui
import requests
import re
import time
import threading
import json
from urllib.parse import urlparse, parse_qs, unquote

# --- Modul-Imports ---
from constants import (
    ADDON, ADDON_ID, ADDON_NAME, ADDON_VERSION,
    RADIODE_SEARCH_API_URL, RADIODE_NOWPLAYING_API_URL, RADIODE_DETAILS_API_URL,
    DEFAULT_HTTP_HEADERS, INVALID_METADATA_VALUES,
    SONG_TIMEOUT_FALLBACK_S, SONG_TIMEOUT_BUFFER_S,
    PropertyNames as _P, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE
)
from logger import log_debug, log_info, log_warning, log_error
from musicbrainz import (
    identify_artist_title_via_musicbrainz as _identify_artist_title_via_musicbrainz,
    musicbrainz_query_artist_info as _musicbrainz_query_artist_info,
    musicbrainz_query_recording as _musicbrainz_query_recording,
    mb_similarity as _mb_similarity,
    _mb_cache,
)
from radiode import parse_radiode_api_title as _parse_radiode_api_title
from metadata import (
    extract_stream_title as _extract_stream_title,
    parse_stream_title_simple as _parse_stream_title_simple,
    parse_stream_title_complex as _parse_metadata_complex,
    get_last_separator_variant as _get_last_separator_variant
)


# Window-Properties für die Skin
WINDOW = xbmcgui.Window(10000)  # Home window

class PlayerMonitor(xbmc.Player):
    """Monitor für Player-Events um Logo SOFORT beim Stream-Start zu erfassen"""
    def __init__(self, radio_monitor):
        super(PlayerMonitor, self).__init__()
        self.radio_monitor = radio_monitor
    
    def onPlayBackStarted(self):
        """Liest Plugin-Slug aus radio.de light URL – vor Stream-Auflösung verfügbar"""
        self.radio_monitor.plugin_slug = None  # immer zurücksetzen, auch bei Nicht-radio.de-Streams
        try:
            playing_file = self.getPlayingFile()
            if 'plugin.audio.radio_de_light' in playing_file:
                parsed = urlparse(playing_file)
                params = parse_qs(parsed.query)
                iconimage_list = params.get('iconimage', [])
                if iconimage_list:
                    iconimage = unquote(iconimage_list[0])
                    slug_match = re.search(r'/([^/]+)\.[a-z]+(?:\?|$)', iconimage)
                    if slug_match:
                        slug = slug_match.group(1)
                        self.radio_monitor.plugin_slug = slug
                        xbmc.log(f"[{ADDON_NAME}] Plugin-Slug aus iconimage: '{slug}'", xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onPlayBackStarted: {e}", xbmc.LOGDEBUG)

    def onAVStarted(self):
        """Wird aufgerufen SOFORT wenn Stream startet - ListItem.Icon ist noch verfügbar!"""
        try:
            if self.isPlayingVideo():
                # Video gestartet → Radio-Properties sofort löschen
                xbmc.log(f"[{ADDON_NAME}] Video gestartet - lösche Radio-Properties sofort", xbmc.LOGINFO)
                self.radio_monitor.is_playing = False
                self.radio_monitor.current_url = None
                self.radio_monitor.stop_metadata_monitoring()
                self.radio_monitor.clear_properties()
                return

            if self.isPlayingAudio():
                playing_file = self.getPlayingFile()

                # Lokale Datei → Radio-Properties sofort löschen
                if not (playing_file.startswith('http://') or playing_file.startswith('https://')):
                    xbmc.log(f"[{ADDON_NAME}] Lokale Datei gestartet - lösche Radio-Properties sofort", xbmc.LOGINFO)
                    self.radio_monitor.is_playing = False
                    self.radio_monitor.current_url = None
                    self.radio_monitor.stop_metadata_monitoring()
                    self.radio_monitor.clear_properties()
                    return

                # HTTP/HTTPS Audio-Stream → SOFORT Logo vom ListItem lesen
                listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                if listitem_icon and self.radio_monitor.is_real_logo(listitem_icon):
                    self.radio_monitor.station_logo = listitem_icon
                    xbmc.log(f"[{ADDON_NAME}] ⚡ Logo SOFORT beim Start erfasst: {listitem_icon}", xbmc.LOGINFO)
                else:
                    xbmc.log(f"[{ADDON_NAME}] ⚠ ListItem.Icon beim Start: {listitem_icon}", xbmc.LOGDEBUG)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onAVStarted: {str(e)}", xbmc.LOGERROR)

class RadioMonitor(xbmc.Monitor):
    """
    Hauptklasse für das Monitoring und die Verwaltung von Radio-Streams, Metadaten und Player-Events.
    Verantwortlich für das Setzen und Löschen von Properties, das Aktualisieren von Metadaten und das Handling von API-Fallbacks.
    """
    def __init__(self):
        super(RadioMonitor, self).__init__()
        self.player = xbmc.Player()
        self.is_playing = False
        self.current_url = None
        self.metadata_thread = None
        self.stop_thread = False
        self.metadata_generation = 0  # invalidates stale workers on restart
        self.station_id = None    # radio.de Station ID
        self.station_logo = None  # Logo URL von radio.de API
        self.station_slug = None  # Sender-Slug aus Stream-URL (für API-Fallback)
        self.plugin_slug = None   # Sender-Slug aus radio.de light Plugin-URL (iconimage)
        self.use_api_fallback = False  # Flag für API-Fallback
        # Zentrale Song-Timeout-Status (wird von Metadata-Workern geteilt)
        self._last_song_time = 0.0
        self._song_timeout = SONG_TIMEOUT_FALLBACK_S
        
        # Event-Handler für Player-Events
        self.player_monitor = PlayerMonitor(self)
        
        xbmc.log(f"[{ADDON_NAME}] Service gestartet", xbmc.LOGINFO)
        
    def clear_properties(self):
        """Löscht alle Radio-Properties"""
        # Reset Logo und Plugin-Slug
        self.station_logo = None
        self.plugin_slug = None

        # Lösche auch radio.de Addon Properties
        WINDOW.clearProperty('RadioDE.StationLogo')
        WINDOW.clearProperty('RadioDE.StationName')
        
        # Window-Properties (für Fallback)
        WINDOW.clearProperty(_P.STATION)
        WINDOW.clearProperty(_P.TITLE)
        WINDOW.clearProperty(_P.ARTIST)
        WINDOW.clearProperty(_P.ALBUM)
        WINDOW.clearProperty(_P.ALBUM_DATE)
        WINDOW.clearProperty(_P.GENRE)
        WINDOW.clearProperty(_P.MBID)
        WINDOW.clearProperty(_P.FIRST_REL)
        WINDOW.clearProperty(_P.STREAM_TTL)
        WINDOW.clearProperty(_P.PLAYING)
        WINDOW.clearProperty(_P.LOGO)
        WINDOW.clearProperty(_P.BAND_FORM)
        WINDOW.clearProperty(_P.BAND_MEM)
        
        xbmc.log(f"[{ADDON_NAME}] Properties gelöscht", xbmc.LOGDEBUG)
        
    def set_property_safe(self, key, value):
        """Setzt eine Window-Property nur wenn der Wert nicht leer ist."""
        if value:
            WINDOW.setProperty(key, str(value))
    
    def is_real_logo(self, url):
        """Prüft ob es ein echtes Logo ist (keine Kodi-Fallbacks)"""
        if not url:
            return False
        invalid = ['DefaultAudio', 'DefaultAlbum', 'no_image', 'no-image', 'default.png', 'Default']
        return not any(x in str(url) for x in invalid)
    
    def set_logo_safe(self):
        """Setzt Logo-Property nur wenn echtes Logo vorhanden, sonst Kodi-Fallback"""
        if self.station_logo and self.is_real_logo(self.station_logo):
            self.set_property_safe(_P.LOGO, self.station_logo)
        else:
            # Kein echtes Logo → Property leer lassen (Kodi nutzt automatisch Fallback)
            WINDOW.clearProperty(_P.LOGO)
    
    def update_player_metadata(self, artist, title, album_or_station, logo=None, mbid=None):
        """Versucht die Kodi Player Metadaten zu aktualisieren (für Standard InfoLabels)"""
        try:
            if not self.player.isPlayingAudio():
                return
            
            # Erstelle ein ListItem mit den korrekten Metadaten
            list_item = xbmcgui.ListItem()
            
            # Setze MusicInfoTag
            info_tag = list_item.getMusicInfoTag()
            if title:
                info_tag.setTitle(title)
            if artist:
                info_tag.setArtist(artist)
            if album_or_station:
                info_tag.setAlbum(album_or_station)  # Album oder Station als Fallback
            if mbid:
                # Kodi/Python API unterscheidet je nach Version bei Methodennamen und Parametertyp.
                set_mbid_methods = [
                    ('setMusicBrainzArtistID', [mbid]),
                    ('setMusicBrainzArtistID', mbid),
                    ('setMusicBrainzArtistId', [mbid]),
                    ('setMusicBrainzArtistId', mbid),
                ]
                for method_name, arg in set_mbid_methods:
                    method = getattr(info_tag, method_name, None)
                    if callable(method):
                        try:
                            method(arg)
                            xbmc.log(f"[{ADDON_NAME}] Player MBID gesetzt über {method_name}: {mbid}", xbmc.LOGDEBUG)
                            break
                        except Exception:
                            continue
            
            # Setze Logo als Cover Art
            if logo and logo != "DefaultAudio.png":
                list_item.setArt({'thumb': logo, 'poster': logo, 'icon': logo})
            
            # Versuche den Player zu aktualisieren (klappt möglicherweise nicht bei allen Kodi Versionen)
            # Dies ist ein "Best Effort" - es kann sein, dass es nicht funktioniert
            try:
                # Diese Methode existiert ab Kodi 18+
                self.player.updateInfoTag(list_item)
                xbmc.log(f"[{ADDON_NAME}] Player InfoTag aktualisiert: {artist} - {title}", xbmc.LOGDEBUG)
            except AttributeError:
                # Fallback: Setze Properties, die Skins nutzen können
                xbmc.log(f"[{ADDON_NAME}] updateInfoTag() nicht verfügbar - nutze nur Window Properties", xbmc.LOGDEBUG)
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Aktualisieren der Player Metadaten: {str(e)}", xbmc.LOGDEBUG)
            
    def _setup_api_fallback_from_url(self, url):
        """
        Versucht den Stationsnamen aus der Stream-URL zu extrahieren und setzt
        das API-Fallback-Flag, wenn kein icy-metaint Header verfügbar ist.
        Wird aufgerufen wenn der Stream keine ICY-Metadaten liefert.
        """
        try:
            if 'radiode' in url.lower() or 'radio.de' in url.lower() or 'radio-de' in url.lower():
                xbmc.log(f"[{ADDON_NAME}] radio.de Stream erkannt, versuche Stationsnamen aus URL", xbmc.LOGDEBUG)

                match = re.search(r'stream\.([^/]+)\.de/([^/]+)', url)
                if not match:
                    match = re.search(r'//([^/]+)/([^/]+)', url)

                if match:
                    station_slug = match.group(2)
                    station_name = station_slug.replace('-', ' ').replace('_', ' ').title()

                    # Bekannte Sonderfälle normalisieren
                    station_name = station_name.replace('Brf ', 'Berliner Rundfunk ')
                    station_name = station_name.replace('100prozent', '100%')

                    self.set_property_safe(_P.STATION, station_name)
                    xbmc.log(f"[{ADDON_NAME}] Station aus URL erkannt: {station_name}", xbmc.LOGDEBUG)

                    self.use_api_fallback = True
                    self.station_slug = station_slug

                    return station_name
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei URL-Analyse fuer API-Fallback: {str(e)}", xbmc.LOGDEBUG)
        return None
    
    def get_nowplaying_from_apis(self, station_name, stream_url):
        """Versucht nowPlaying von verschiedenen APIs zu holen"""
        xbmc.log(f"[{ADDON_NAME}] API-Fallback gestartet für Station: '{station_name}'", xbmc.LOGDEBUG)

        # 1. Versuche radio.de API (sender-unabhängig, funktioniert für alle Stationen)
        artist, title = self.get_radiode_api_nowplaying(station_name)
        if artist or title:
            xbmc.log(f"[{ADDON_NAME}] ✓ radio.de API: {artist} - {title}", xbmc.LOGINFO)
            return artist, title

        # 2. Fallback: Kodi Player InfoTags
        try:
            if self.player.isPlayingAudio():
                info_tag = self.player.getMusicInfoTag()
                title = info_tag.getTitle()
                artist = info_tag.getArtist()
                
                invalid_values = INVALID_METADATA_VALUES + ['', station_name]
                if title and title not in invalid_values:
                    # Filter Zahlen-IDs
                    if _NUMERIC_ID_RE.match(title):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag enthält Zahlen-ID, ignoriere: {title}", xbmc.LOGDEBUG)
                        return None, None
                    
                    # Filter einzelne Zahlen als Artist
                    if artist and re.match(r'^\d+$', artist):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag Artist ist nur eine Zahl, ignoriere: {artist}", xbmc.LOGDEBUG)
                        artist = None
                    
                    # Filter einzelne Zahlen als Title
                    if title and re.match(r'^\d+$', title):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag Title ist nur eine Zahl, ignoriere: {title}", xbmc.LOGDEBUG)
                        return None, None
                    
                    # Filter bekannte Platzhalter bei Artist
                    if artist and artist in invalid_values:
                        artist = None
                    
                    # Wenn Artist valide ist
                    if artist:
                        return artist, title
                    else:
                        # Versuche zu parsen
                        parsed_artist, parsed_title = self.parse_stream_title_simple(title)
                        if parsed_artist and parsed_title:
                            return parsed_artist, parsed_title
                        return None, title
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen Player InfoTags: {str(e)}", xbmc.LOGDEBUG)
        
        return None, None
    
    def parse_stream_title_simple(self, stream_title):
        """Einfache Trennung ohne API-Aufrufe (Nutzt zentrales metadata Modul)"""
        return _parse_stream_title_simple(stream_title)
    
    def get_radiode_api_nowplaying(self, station_name):
        """Holt aktuelle Song-Info direkt von der radio.de API"""
        try:
            # Slug-Quelle: Plugin-URL hat Priorität, Logo-URL als Fallback
            slug = self.plugin_slug
            if not slug and self.station_logo:
                logo_match = re.search(r'radio-assets\.com/\d+/([^./?]+)', self.station_logo)
                if logo_match:
                    slug = logo_match.group(1)

            if slug:
                xbmc.log(f"[{ADDON_NAME}] Station-Slug: '{slug}' (plugin={bool(self.plugin_slug)})", xbmc.LOGDEBUG)
                try:
                    det_response = requests.get(RADIODE_DETAILS_API_URL, params={'stationIds': slug}, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                    xbmc.log(f"[{ADDON_NAME}] Details-API Status: {det_response.status_code}, URL: {det_response.url}", xbmc.LOGDEBUG)
                    if det_response.status_code == 200:
                        det_data = det_response.json()
                        xbmc.log(f"[{ADDON_NAME}] Details-API Response: {str(det_data)[:300]}", xbmc.LOGDEBUG)
                        if isinstance(det_data, list) and len(det_data) > 0:
                            proper_name = det_data[0].get('name', '')
                            if proper_name:
                                self.set_property_safe(_P.STATION, proper_name)
                                xbmc.log(f"[{ADDON_NAME}] Station aus Details-API: '{proper_name}'", xbmc.LOGINFO)
                            det_logo = det_data[0].get('logo300x300', '')
                            if det_logo and not self.station_logo:
                                self.station_logo = det_logo
                                self.set_logo_safe()
                                xbmc.log(f"[{ADDON_NAME}] Logo aus Details-API: '{det_logo}'", xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler bei Details-API: {e}", xbmc.LOGDEBUG)

                try:
                    np_response = requests.get(RADIODE_NOWPLAYING_API_URL, params={'stationIds': slug}, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                    if np_response.status_code == 200:
                        np_data = np_response.json()
                        xbmc.log(f"[{ADDON_NAME}] now-playing API Response (Slug): {np_data}", xbmc.LOGDEBUG)
                        if isinstance(np_data, list) and len(np_data) > 0:
                            full_title = np_data[0].get('title', '')
                            if full_title and ' - ' in full_title:
                                artist, title = _parse_radiode_api_title(full_title, station_name)
                                if artist or title:
                                    xbmc.log(f"[{ADDON_NAME}] ✓ now-playing via Slug: {artist} - {title}", xbmc.LOGINFO)
                                    return artist, title
                        xbmc.log(f"[{ADDON_NAME}] ✗ Slug-Abfrage ohne Ergebnis – weiter mit Suche", xbmc.LOGDEBUG)
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler bei now-playing via Slug: {e}", xbmc.LOGDEBUG)

            # Bereinige den Sendernamen FÜR DIE SUCHE
            search_name = station_name
            
            # Entferne technische Suffixe
            search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
            search_name = re.sub(r'\s*-\s*[A-Z]{2,3}\s*$', '', search_name)  # z.B. " - RK"
            
            # Entferne spezielle Zusätze die die Suche stören
            search_name = re.sub(r'\s*-\s*100%.*$', '', search_name, flags=re.IGNORECASE)  # "- 100% Deutsch"
            search_name = re.sub(r'\s*91\.4.*$', '', search_name, flags=re.IGNORECASE)  # "91.4"
            search_name = re.sub(r'\s*-\s*\d+\.\d+.*$', '', search_name)  # Frequenzen wie "- 91.4"
            
            search_name = search_name.strip()
            
            xbmc.log(f"[{ADDON_NAME}] Suche radio.de API mit: '{search_name}' (Original: '{station_name}')", xbmc.LOGDEBUG)
            
            params = {'query': search_name, 'count': 20}
            response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
            if response.status_code != 200:
                xbmc.log(f"[{ADDON_NAME}] radio.de API: ungültige Antwort (Status {response.status_code})", xbmc.LOGWARNING)
                return None, None
            data = response.json()
            
            xbmc.log(f"[{ADDON_NAME}] Search API: {data.get('totalCount', 0)} Treffer", xbmc.LOGDEBUG)
            
            # Schritt 1: Stationsname bereinigen und radio.de API durchsuchen
            if 'playables' in data and len(data['playables']) > 0:
                # Suche die beste Übereinstimmung
                best_match = None
                best_match_score = 0
                
                # Normalisiere beide Namen für Vergleich
                search_normalized = search_name.lower().replace('-', ' ').replace('_', ' ').strip()
                
                for station in data['playables'][:20]:  # Prüfe die ersten 20 Treffer
                    station_found = station.get('name', '')
                    station_normalized = station_found.lower().replace('-', ' ').replace('_', ' ').strip()
                    
                    # Exakter Match (Priorität)
                    if station_normalized == search_normalized:
                        best_match = station
                        best_match_score = 1000  # Höchste Priorität
                        xbmc.log(f"[{ADDON_NAME}] EXAKTER MATCH gefunden: '{station_found}'", xbmc.LOGDEBUG)
                        break
                    
                    # Substring-Match (Station enthält Suchbegriff)
                    if search_normalized in station_normalized:
                        score = 100 + len(search_normalized)  # Je länger der Match, desto besser
                        if score > best_match_score:
                            best_match = station
                            best_match_score = score
                            xbmc.log(f"[{ADDON_NAME}] Substring-Match: '{station_found}' - Score: {score}", xbmc.LOGDEBUG)
                    
                    # Wort-basierter Match
                    elif search_normalized:
                        search_words = set(search_normalized.split())
                        station_words = set(station_normalized.split())
                        matching_words = search_words.intersection(station_words)
                        score = len(matching_words) * 10
                        
                        if score > best_match_score:
                            best_match = station
                            best_match_score = score
                            xbmc.log(f"[{ADDON_NAME}] Wort-Match: '{station_found}' - Score: {score} (Woerter: {matching_words})", xbmc.LOGDEBUG)
                
                if best_match and best_match_score > 0:
                    station_found = best_match.get('name', '')
                    station_id = best_match.get('id', '')
                    station_logo = best_match.get('logo300x300', '')  # Logo aus API

                    # Speichere Logo für spätere Verwendung
                    if station_logo:
                        self.station_logo = station_logo
                        self.set_logo_safe()
                        xbmc.log(f"[{ADDON_NAME}] Station-Logo aus API: {station_logo}", xbmc.LOGINFO)

                    xbmc.log(f"[{ADDON_NAME}] Beste Uebereinstimmung: '{station_found}' (Score: {best_match_score}, ID: {station_id})", xbmc.LOGDEBUG)
                    
                    # Schritt 2: Station-ID für now-playing API verwenden
                    if station_id:
                        xbmc.log(f"[{ADDON_NAME}] Hole Now-Playing von: {RADIODE_NOWPLAYING_API_URL}?stationIds={station_id}", xbmc.LOGDEBUG)
                        
                        try:
                            params = {'stationIds': station_id}
                            np_response = requests.get(RADIODE_NOWPLAYING_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                            if np_response.status_code == 200:
                                np_data = np_response.json()
                                xbmc.log(f"[{ADDON_NAME}] now-playing API Response: {np_data}", xbmc.LOGDEBUG)
                                
                                # Response ist ein Array: [{"title":"ARTIST - TITLE","stationId":"..."}]
                                if isinstance(np_data, list) and len(np_data) > 0:
                                    track_info = np_data[0]
                                    full_title = track_info.get('title', '')
                                    
                                    xbmc.log(f"[{ADDON_NAME}] Empfangener Titel: '{full_title}'", xbmc.LOGDEBUG)
                                    
                                    if full_title and ' - ' in full_title:
                                        artist, title = _parse_radiode_api_title(full_title, station_name)
                                        if artist is not None or title is not None:
                                            if artist and title:
                                                xbmc.log(f"[{ADDON_NAME}] ✓ now-playing API erfolgreich: {artist} - {title}", xbmc.LOGINFO)
                                                return artist, title
                                            if title:
                                                xbmc.log(f"[{ADDON_NAME}] ✓ now-playing API erfolgreich (nur Title): {title}", xbmc.LOGINFO)
                                                return None, title
                                    else:
                                        xbmc.log(f"[{ADDON_NAME}] ✗ Titel-Format unbekannt: '{full_title}'", xbmc.LOGDEBUG)
                                else:
                                    xbmc.log(f"[{ADDON_NAME}] ✗ Leere now-playing Response", xbmc.LOGDEBUG)
                            else:
                                xbmc.log(f"[{ADDON_NAME}] ✗ now-playing API Fehler: {np_response.status_code}", xbmc.LOGDEBUG)
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler bei now-playing API: {str(e)}", xbmc.LOGWARNING)

                    else:
                        xbmc.log(f"[{ADDON_NAME}] ✗ Keine Station-ID gefunden", xbmc.LOGDEBUG)
                else:
                    xbmc.log(f"[{ADDON_NAME}] ✗ Kein Match gefunden (Score zu niedrig)", xbmc.LOGDEBUG)
            else:
                xbmc.log(f"[{ADDON_NAME}] ✗ Keine Treffer für '{search_name}'", xbmc.LOGDEBUG)
                        
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei radio.de API Abfrage: {str(e)}", xbmc.LOGWARNING)
        
        return None, None
    
    def api_metadata_worker(self, generation):
        """Fallback: Pollt verschiedene APIs wenn keine ICY-Metadaten verfügbar"""
        xbmc.log(f"[{ADDON_NAME}] API Metadata Worker gestartet (Fallback-Modus)", xbmc.LOGDEBUG)
        
        last_title = ""
        poll_interval = 10  # Sekunden zwischen API-Abfragen
        station_name = WINDOW.getProperty(_P.STATION)
        stream_url = self.current_url or ''

        try:
            while (
                not self.stop_thread
                and self.is_playing
                and (self.use_api_fallback or self.plugin_slug)
                and generation == self.metadata_generation
            ):
                # station_name aktualisieren falls API in get_radiode_api_nowplaying
                # den korrekten Namen nachträglich gesetzt hat (z.B. via Details-API)
                fresh_station = WINDOW.getProperty(_P.STATION)
                if fresh_station:
                    station_name = fresh_station

                # Versuche verschiedene APIs (plugin_slug erlaubt Abfrage ohne station_name)
                if station_name or self.plugin_slug:
                    artist, title = self.get_nowplaying_from_apis(station_name, stream_url)
                    
                    if title and title != last_title:
                        last_title = title
                        
                        # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                        self.set_logo_safe()
                        album, album_date, mbid, first_release, duration_ms = '', '', '', '', 0
                        if artist and title:
                            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
                            if uncertain:
                                mbid = ''
                                duration_ms = 0
                            else:
                                album = mb_album
                                album_date = mb_album_date
                                first_release = mb_first_release
                                if mb_artist and mb_title and (
                                    _mb_similarity(mb_artist, artist) < 0.8 or _mb_similarity(mb_title, title) < 0.8
                                ):
                                    # Nur MBID/Album nutzen, wenn MB den API-Titel plausibel bestätigt.
                                    mbid = ''
                                    album = ''
                                    album_date = ''
                                    first_release = ''
                                    duration_ms = 0
                        
                        if artist:
                            # Reihenfolge: MBID und Title vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.MBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            self.set_property_safe(_P.TITLE, title)
                            self.set_property_safe(_P.STREAM_TTL, f"{artist} - {title}")
                            if album:
                                self.set_property_safe(_P.ALBUM, album)
                            else:
                                WINDOW.clearProperty(_P.ALBUM)
                            if album_date:
                                self.set_property_safe(_P.ALBUM_DATE, album_date)
                            else:
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                            if mbid:
                                self.set_property_safe(_P.MBID, mbid)
                            else:
                                WINDOW.clearProperty(_P.MBID)
                            if first_release:
                                self.set_property_safe(_P.FIRST_REL, first_release)
                            else:
                                WINDOW.clearProperty(_P.FIRST_REL)
                            # Artist-Trigger zuerst setzen, dann Artist-Info nachziehen.
                            self.set_property_safe(_P.ARTIST, artist)
                            xbmc.log(f"[{ADDON_NAME}] API Update: {artist} - {title}", xbmc.LOGINFO)
                            if mbid and artist:
                                time.sleep(1)  # MusicBrainz Rate-Limit einhalten
                                band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                                if band_formed:
                                    self.set_property_safe(_P.BAND_FORM, band_formed)
                                else:
                                    WINDOW.clearProperty(_P.BAND_FORM)
                                if band_members:
                                    self.set_property_safe(_P.BAND_MEM, band_members)
                                else:
                                    WINDOW.clearProperty(_P.BAND_MEM)
                                if mb_genre:
                                    self.set_property_safe(_P.GENRE, mb_genre)
                            else:
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)

                            # Logo sofort nach Artist setzen
                            self.set_logo_safe()

                            # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                            self._last_song_time = time.time()
                            self._song_timeout = (duration_ms / 1000 + SONG_TIMEOUT_BUFFER_S) if duration_ms else SONG_TIMEOUT_FALLBACK_S
                            xbmc.log(
                                f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s (MB-Länge: {duration_ms}ms)",
                                xbmc.LOGDEBUG
                            )

                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty(_P.LOGO)
                            self.update_player_metadata(artist, title, album if album else station_name, logo if logo else None, mbid if mbid else None)
                        else:
                            # Artist und MBID bewusst NICHT löschen —
                            # alten Wert behalten bis neuer gesichert ist.
                            # ArtistSlideshow zeigt weiter den letzten bekannten Künstler
                            # statt auf rohes ICY-Metadaten-Fallback zurückzufallen.
                            # Gelöscht wird nur beim echten Stream-Stop (clear_properties).
                            WINDOW.clearProperty(_P.ALBUM)
                            WINDOW.clearProperty(_P.ALBUM_DATE)
                            WINDOW.clearProperty(_P.FIRST_REL)
                            WINDOW.clearProperty(_P.BAND_FORM)
                            WINDOW.clearProperty(_P.BAND_MEM)
                            self.set_property_safe(_P.TITLE, title)
                            self.set_property_safe(_P.STREAM_TTL, title)
                            xbmc.log(f"[{ADDON_NAME}] API Update: {title}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty(_P.LOGO)
                            self.update_player_metadata(None, title, station_name, logo if logo else None, None)

                        # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                        self._last_song_time = time.time()
                        self._song_timeout = (duration_ms / 1000 + SONG_TIMEOUT_BUFFER_S) if duration_ms else SONG_TIMEOUT_FALLBACK_S
                        xbmc.log(
                            f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s (MB-Länge: {duration_ms}ms)",
                            xbmc.LOGDEBUG
                        )

                # Song-Timeout: Properties löschen wenn der Song abgelaufen ist.
                if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                    xbmc.log(
                        f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                        xbmc.LOGDEBUG
                    )
                    WINDOW.clearProperty(_P.ARTIST)
                    WINDOW.clearProperty(_P.TITLE)
                    WINDOW.clearProperty(_P.ALBUM)
                    WINDOW.clearProperty(_P.ALBUM_DATE)
                    WINDOW.clearProperty(_P.MBID)
                    WINDOW.clearProperty(_P.FIRST_REL)
                    WINDOW.clearProperty(_P.BAND_FORM)
                    WINDOW.clearProperty(_P.BAND_MEM)
                    WINDOW.clearProperty(_P.GENRE)
                    WINDOW.clearProperty(_P.STREAM_TTL)
                    self._last_song_time = 0.0

                # Warte vor nächster Abfrage
                for _ in range(poll_interval * 2):  # 10 Sekunden in 0.5s Schritten
                    if (
                        self.stop_thread
                        or not self.is_playing
                        or generation != self.metadata_generation
                    ):
                        break
                    time.sleep(0.5)
                
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler im API Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            xbmc.log(f"[{ADDON_NAME}] API Metadata Worker beendet", xbmc.LOGDEBUG)

    def _musicplayer_metadata_fallback(self, generation):
        """Fallback für Streams ohne ICY und ohne radio.de-API.
        Pollt MusicPlayer.Artist/Title auf Änderungen (wie api_metadata_worker die radio.de API).
        Titelwechsel auf Live-Streams (z.B. Mother Earth Radio) werden so erkannt.
        Für Library-Streams (Ampache) beendet sich die Schleife beim nächsten onAVStarted
        automatisch via metadata_generation-Check.
        Wenn MusicPlayer leer bleibt, wird RadioMonitor.Playing gecleart.
        Station wird nicht gesetzt – es gibt keinen ICY-Stationsnamen.
        """
        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback aktiv (kein ICY, kein API-Fallback)", xbmc.LOGDEBUG)

        last_artist = ''
        last_title = ''
        poll_interval = 5  # Sekunden zwischen MusicPlayer-Checks

        try:
            while (
                not self.stop_thread
                and self.is_playing
                and generation == self.metadata_generation
            ):
                # MusicPlayer-Metadaten lesen
                try:
                    if not self.player.isPlayingAudio():
                        WINDOW.clearProperty(_P.PLAYING)
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: kein Audio - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
                        return
                    info_tag = self.player.getMusicInfoTag()
                    mp_artist = (info_tag.getArtist() or '').strip()
                    mp_title = (info_tag.getTitle() or '').strip()
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Fehler beim Lesen der Metadaten: {e}", xbmc.LOGDEBUG)
                    WINDOW.clearProperty(_P.PLAYING)
                    return

                # Erster Durchlauf und beide leer → deaktivieren
                if not last_artist and not last_title and not mp_artist and not mp_title:
                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Artist und Title leer - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
                    WINDOW.clearProperty(_P.PLAYING)
                    return

                # Titelwechsel (oder erster Durchlauf mit Inhalt)?
                if mp_artist != last_artist or mp_title != last_title:
                    last_artist = mp_artist
                    last_title = mp_title

                    if generation != self.metadata_generation:
                        return

                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Artist='{mp_artist}', Title='{mp_title}'", xbmc.LOGINFO)

                    # MusicBrainz-Lookup
                    _, mb_artist, mb_title, mbid, mb_album, mb_album_date, mb_first_release, duration_ms = \
                        _musicbrainz_query_recording(mp_title, mp_artist)

                    artist = mb_artist or mp_artist
                    title = mb_title or mp_title

                    if generation != self.metadata_generation:
                        return

                    # Properties setzen – MBID vor Artist (AS-Trigger)
                    if title:
                        self.set_property_safe(_P.TITLE, title)
                    else:
                        WINDOW.clearProperty(_P.TITLE)
                    if mb_album:
                        self.set_property_safe(_P.ALBUM, mb_album)
                    else:
                        WINDOW.clearProperty(_P.ALBUM)
                    if mb_album_date:
                        self.set_property_safe(_P.ALBUM_DATE, mb_album_date)
                    else:
                        WINDOW.clearProperty(_P.ALBUM_DATE)
                    if mbid:
                        self.set_property_safe(_P.MBID, mbid)
                    else:
                        WINDOW.clearProperty(_P.MBID)
                    if mb_first_release:
                        self.set_property_safe(_P.FIRST_REL, mb_first_release)
                    else:
                        WINDOW.clearProperty(_P.FIRST_REL)
                    if artist:
                        self.set_property_safe(_P.ARTIST, artist)
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback gesetzt: Artist='{artist}', Title='{title}', MBID='{mbid}'", xbmc.LOGINFO)
                    else:
                        WINDOW.clearProperty(_P.ARTIST)
                        WINDOW.clearProperty(_P.PLAYING)
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: kein Artist ermittelbar - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
                        return

                    # Player.Icon bei Titelwechsel neu lesen (z.B. AzuraCast liefert pro Song anderes Album-Cover)
                    try:
                        current_icon = xbmc.getInfoLabel('Player.Icon')
                        if current_icon and self.is_real_logo(current_icon):
                            self.station_logo = current_icon
                    except Exception:
                        pass
                    self.set_logo_safe()

                    if mbid and artist:
                        time.sleep(1)
                        band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                        if band_formed:
                            self.set_property_safe(_P.BAND_FORM, band_formed)
                        else:
                            WINDOW.clearProperty(_P.BAND_FORM)
                        if band_members:
                            self.set_property_safe(_P.BAND_MEM, band_members)
                        else:
                            WINDOW.clearProperty(_P.BAND_MEM)
                        if mb_genre:
                            self.set_property_safe(_P.GENRE, mb_genre)
                    else:
                        WINDOW.clearProperty(_P.BAND_FORM)
                        WINDOW.clearProperty(_P.BAND_MEM)

                    # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                    self._last_song_time = time.time()
                    self._song_timeout = (duration_ms / 1000 + SONG_TIMEOUT_BUFFER_S) if duration_ms else SONG_TIMEOUT_FALLBACK_S
                    xbmc.log(
                        f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s (MB-Länge: {duration_ms}ms)",
                        xbmc.LOGDEBUG
                    )

                # Song-Timeout: Properties löschen wenn der Song abgelaufen ist.
                if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                    xbmc.log(
                        f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                        xbmc.LOGDEBUG
                    )
                    WINDOW.clearProperty(_P.ARTIST)
                    WINDOW.clearProperty(_P.TITLE)
                    WINDOW.clearProperty(_P.ALBUM)
                    WINDOW.clearProperty(_P.ALBUM_DATE)
                    WINDOW.clearProperty(_P.MBID)
                    WINDOW.clearProperty(_P.FIRST_REL)
                    WINDOW.clearProperty(_P.BAND_FORM)
                    WINDOW.clearProperty(_P.BAND_MEM)
                    WINDOW.clearProperty(_P.GENRE)
                    WINDOW.clearProperty(_P.STREAM_TTL)
                    self._last_song_time = 0.0

                # Warte vor nächster Abfrage (in 0.5s-Schritten für schnelles Beenden)
                for _ in range(poll_interval * 2):
                    if (
                        self.stop_thread
                        or not self.is_playing
                        or generation != self.metadata_generation
                    ):
                        break
                    time.sleep(0.5)

        finally:
            xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback beendet", xbmc.LOGDEBUG)

    def parse_icy_metadata(self, url):
        """Liest ICY-Metadaten aus dem Stream"""
        try:
            headers = {'Icy-MetaData': '1', **DEFAULT_HTTP_HEADERS}
            response = requests.get(url, headers=headers, stream=True, timeout=5)
            
            # KOMPLETT LOGGEN: Alle ICY-Header
            xbmc.log(f"[{ADDON_NAME}] === ALLE ICY RESPONSE HEADERS ===", xbmc.LOGDEBUG)
            for header_name, header_value in response.headers.items():
                if 'icy' in header_name.lower() or 'ice' in header_name.lower():
                    xbmc.log(f"[{ADDON_NAME}]   {header_name}: {header_value}", xbmc.LOGDEBUG)
            xbmc.log(f"[{ADDON_NAME}] =================================", xbmc.LOGDEBUG)
            
            # ICY-Metadaten aus den Headers
            icy_name = response.headers.get('icy-name', '')
            icy_genre = response.headers.get('icy-genre', '')
            
            # Station initial aus ICY-Header icy-name (wird von API überschrieben falls verfügbar)
            station_name = icy_name
            if station_name:
                self.set_property_safe(_P.STATION, station_name)
                xbmc.log(f"[{ADDON_NAME}] Station (ICY): {station_name}", xbmc.LOGDEBUG)
            else:
                WINDOW.clearProperty(_P.STATION)
                xbmc.log(f"[{ADDON_NAME}] Kein icy-name - Station geleert", xbmc.LOGDEBUG)
            
            if icy_genre:
                self.set_property_safe(_P.GENRE, icy_genre)
                xbmc.log(f"[{ADDON_NAME}] Genre: {icy_genre}", xbmc.LOGDEBUG)
            
            # Metaint - Position der Metadaten im Stream
            metaint = response.headers.get('icy-metaint')
            if not metaint:
                xbmc.log(f"[{ADDON_NAME}] Kein icy-metaint Header gefunden - Stream sendet keine ICY-Metadaten", xbmc.LOGWARNING)
                self._setup_api_fallback_from_url(url)
                response.close()
                return None

            metaint = int(metaint)
            xbmc.log(f"[{ADDON_NAME}] MetaInt: {metaint}", xbmc.LOGDEBUG)
            
            return {'metaint': metaint, 'response': response, 'station': station_name, 'genre': icy_genre}
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Abrufen der ICY-Metadaten: {str(e)}", xbmc.LOGERROR)
            self._setup_api_fallback_from_url(url)
            return None
            
    def extract_stream_title(self, metadata_raw):
        """Extrahiert den StreamTitle aus den rohen Metadaten (Nutzt zentrales metadata Modul)"""
        return _extract_stream_title(metadata_raw)
        
    def parse_stream_title(self, stream_title, station_name=None, stream_url=None):
        """
        Trennt Artist und Title aus dem ICY-StreamTitle.
        Priorität:
        1. API (radio.de) – erste Quelle; Nebeneffekte: Station+Logo werden gesetzt
        2. 'von'-Format → eindeutig, MusicBrainz zur Bestätigung
        3. Trennzeichen → part1/part2, MusicBrainz zur Reihenfolge-Bestimmung
        4. Fallback: ganzer String als Title
        """
        invalid = INVALID_METADATA_VALUES + ["", station_name]

        # --- API als erste Quelle ---
        if station_name and stream_url:
            api_artist, api_title = self.get_nowplaying_from_apis(station_name, stream_url)
            if api_artist and api_title and api_artist not in invalid and api_title not in invalid:
                # Prüfe Übereinstimmung API <-> ICY
                api_combined = f"{api_artist} - {api_title}".strip()
                is_numeric_icy = bool(stream_title and _NUMERIC_ID_RE.match(stream_title))
                effective_stream_title = None if is_numeric_icy else stream_title
                try:
                    sim = _mb_similarity((effective_stream_title or '').strip(), api_combined)
                except Exception:
                    sim = 0.0

                if (effective_stream_title and sim >= 0.9) or (not effective_stream_title):
                    xbmc.log(f"[{ADDON_NAME}] API-Daten (erste Quelle): Artist='{api_artist}', Title='{api_title}' (sim={sim:.2f})", xbmc.LOGINFO)
                    mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = \
                        _identify_artist_title_via_musicbrainz(api_artist, api_title)
                    if uncertain:
                        mb_artist, mb_title = api_artist, api_title
                        mb_album, mb_album_date, mbid, mb_first_release, duration_ms = '', '', '', '', 0
                    if mb_artist in invalid: mb_artist = None
                    if mb_title in invalid:  mb_title  = None
                    if mb_artist or mb_title:
                        return mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, duration_ms

        # --- ICY-Analyse (via metadata Modul) ---
        artist, title, is_von, has_multi = _parse_metadata_complex(stream_title, station_name)
        if not artist and not title:
            return None, None, '', '', '', '', 0

        # MusicBrainz zur Verifikation und Vervollständigung
        mb_first_release = ''
        duration_ms = 0
        
        if has_multi:
            # Mehrfaches ' - ' -> last-separator Variante prüfen
            alt_p1, alt_p2 = _get_last_separator_variant(stream_title)
            xbmc.log(f"[{ADDON_NAME}] MusicBrainz: prüfe last-separator Variante: Title='{alt_p1}', Artist='{alt_p2}'", xbmc.LOGINFO)
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(alt_p1, alt_p2)
            if uncertain:
                # Fallback auf Standard-Split
                mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
        else:
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)

        if uncertain:
            # ICY-Standard beibehalten
            mb_artist, mb_title = artist or None, title or None
            mb_album, mb_album_date, mbid, mb_first_release, duration_ms = '', '', '', '', 0
            
        if mb_artist in invalid: mb_artist = None
        if mb_title in invalid:  mb_title  = None
        if not mb_artist and not mb_title:
            return None, None, '', '', '', '', 0
            
        return mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, duration_ms
        
    def metadata_worker(self, url, generation):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        xbmc.log(f"[{ADDON_NAME}] Metadata Worker gestartet", xbmc.LOGDEBUG)
        
        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            xbmc.log(f"[{ADDON_NAME}] Keine ICY-Metadaten verfuegbar - wechsle zu Fallback", xbmc.LOGWARNING)
            if (self.use_api_fallback or self.plugin_slug) and generation == self.metadata_generation:
                self.api_metadata_worker(generation)
            elif generation == self.metadata_generation:
                self._musicplayer_metadata_fallback(generation)
            return
            
        metaint = stream_info['metaint']
        response = stream_info.get('response')
        last_title = ""
        # Nutze zentrale Shared-Timer im RadioMonitor-Objekt
        self._last_song_time = 0.0        # Zeitpunkt des letzten gültigen Titelwechsels
        self._song_timeout   = SONG_TIMEOUT_FALLBACK_S  # wird überschrieben wenn MB eine Länge liefert
        # Hinweis: response.raw.read() blockiert bis Daten da sind; bei Netzabbruch
        # kann das erst enden, wenn der Thread per stop_thread gestoppt wird.
        try:
            while (
                not self.stop_thread
                and self.is_playing
                and generation == self.metadata_generation
            ):
                try:
                    audio_data = response.raw.read(metaint)
                    if not audio_data:
                        break
                        
                    # Metadaten-Länge lesen (1 Byte * 16)
                    meta_length_byte = response.raw.read(1)
                    if not meta_length_byte:
                        break
                        
                    meta_length = ord(meta_length_byte) * 16
                    
                    if meta_length > 0:
                        # Metadaten lesen
                        metadata = response.raw.read(meta_length)
                        if generation != self.metadata_generation:
                            break
                        metadata_str = metadata.decode('utf-8', errors='ignore').strip('\x00')
                        
                        # KOMPLETT LOGGEN: Rohe ICY-Metadaten
                        if metadata_str:
                            xbmc.log(f"[{ADDON_NAME}] === ICY METADATA (ROH) ===", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] {metadata_str}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] =========================", xbmc.LOGDEBUG)
                        
                        stream_title = self.extract_stream_title(metadata_str)
                        
                        # Prüfe ob sich etwas geändert hat (auch leerer Titel zählt)
                        if stream_title != last_title:
                            # Bei neuem StreamTitle: MusicBrainz-Cache invalidieren
                            try:
                                _mb_cache.clear()
                                xbmc.log(f"[{ADDON_NAME}] MB-Cache invalidiert wegen Titelwechsel", xbmc.LOGDEBUG)
                            except Exception:
                                pass
                            last_title = stream_title
                            
                            xbmc.log(f"[{ADDON_NAME}] Neuer StreamTitle erkannt: '{stream_title}'", xbmc.LOGDEBUG)
                            
                            # Station ausschließlich aus ICY-Header (bereits in parse_icy_metadata gesetzt)
                            station_name = stream_info.get('station', '')
                            xbmc.log(f"[{ADDON_NAME}] ICY-Daten: station='{station_name}', stream_title='{stream_title}'", xbmc.LOGINFO)

                            # Artist und Title trennen – API wird intern in parse_stream_title aufgerufen
                            artist, title, album, album_date, mbid, first_release, duration_ms = self.parse_stream_title(stream_title, station_name, url)

                            # Wenn beide None sind (z.B. bei Zahlen-IDs ohne API-Daten), überspringe diesen Titel
                            if artist is None and title is None:
                                xbmc.log(f"[{ADDON_NAME}] Keine verwertbaren Metadaten fuer '{stream_title}' - RadioMonitor Properties bleiben leer", xbmc.LOGDEBUG)
                                # Properties komplett löschen, damit Skin auf MusicPlayer zurückfällt
                                WINDOW.clearProperty(_P.ARTIST)
                                WINDOW.clearProperty(_P.TITLE)
                                WINDOW.clearProperty(_P.ALBUM)
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                                WINDOW.clearProperty(_P.MBID)
                                WINDOW.clearProperty(_P.FIRST_REL)
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)
                                WINDOW.clearProperty(_P.GENRE)
                                WINDOW.clearProperty(_P.STREAM_TTL)
                                self._last_song_time = 0.0  # kein gültiger Song → Timer deaktivieren
                                continue
                            
                            if stream_title not in INVALID_METADATA_VALUES:
                                self.set_property_safe(_P.STREAM_TTL, stream_title)
                            
                            # Reihenfolge: Title und MBID vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.MBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            if title:
                                self.set_property_safe(_P.TITLE, title)
                                xbmc.log(f"[{ADDON_NAME}] Title: {title}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.TITLE)
                                title = ''
                            if album:
                                self.set_property_safe(_P.ALBUM, album)
                                xbmc.log(f"[{ADDON_NAME}] Album: {album}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.ALBUM)
                            if album_date:
                                self.set_property_safe(_P.ALBUM_DATE, album_date)
                                xbmc.log(f"[{ADDON_NAME}] AlbumDate: {album_date}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                            if mbid:
                                self.set_property_safe(_P.MBID, mbid)
                                xbmc.log(f"[{ADDON_NAME}] MBID: {mbid}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.MBID)
                            if first_release:
                                self.set_property_safe(_P.FIRST_REL, first_release)
                                xbmc.log(f"[{ADDON_NAME}] FirstRelease: {first_release}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.FIRST_REL)
                            if artist:
                                self.set_property_safe(_P.ARTIST, artist)
                                xbmc.log(f"[{ADDON_NAME}] Artist: {artist}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.ARTIST)
                                artist = ''

                            # Logo sofort nach Artist setzen – vor dem optionalen Artist-Info-Call,
                            # damit der time.sleep(1) das Logo nicht verzögert.
                            self.set_logo_safe()

                            # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                            # Bei MB-Länge: Länge + Puffer; sonst Fallback.
                            if title:
                                self._last_song_time = time.time()
                                self._song_timeout = (duration_ms / 1000 + SONG_TIMEOUT_BUFFER_S) if duration_ms else SONG_TIMEOUT_FALLBACK_S
                                xbmc.log(
                                    f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s "
                                    f"(MB-Länge: {duration_ms}ms)",
                                    xbmc.LOGDEBUG
                                )

                            # Artist-Info (Gründungsjahr + Mitglieder) erst NACH Artist-Property setzen.
                            # Grund: RadioMonitor.Artist ist der AS-Trigger – er darf nicht durch den
                            # sleep im Artist-Lookup verzögert werden. Die MBID ist nur gesetzt wenn
                            # MB den Artist sicher bestimmt hat (uncertain=False), daher ist sie hier
                            # ein verlässlicher Indikator für einen validen Artist-Match.
                            if mbid and artist:
                                time.sleep(1)  # MusicBrainz Rate-Limit einhalten
                                band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                                if band_formed:
                                    self.set_property_safe(_P.BAND_FORM, band_formed)
                                    xbmc.log(f"[{ADDON_NAME}] BandFormed: {band_formed}", xbmc.LOGDEBUG)
                                else:
                                    WINDOW.clearProperty(_P.BAND_FORM)
                                if band_members:
                                    self.set_property_safe(_P.BAND_MEM, band_members)
                                    xbmc.log(f"[{ADDON_NAME}] BandMembers: {band_members}", xbmc.LOGDEBUG)
                                else:
                                    WINDOW.clearProperty(_P.BAND_MEM)
                                if mb_genre:
                                    self.set_property_safe(_P.GENRE, mb_genre)
                                    xbmc.log(f"[{ADDON_NAME}] Genre (MB): {mb_genre}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)
                            
                            # DEBUG: Zeige alle gesetzten Properties
                            xbmc.log(f"[{ADDON_NAME}] === PROPERTIES GESETZT ===", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = {WINDOW.getProperty(_P.PLAYING)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Album = {WINDOW.getProperty(_P.ALBUM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.AlbumDate = {WINDOW.getProperty(_P.ALBUM_DATE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.MBID = {WINDOW.getProperty(_P.MBID)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.FirstRelease = {WINDOW.getProperty(_P.FIRST_REL)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.BandFormed = {WINDOW.getProperty(_P.BAND_FORM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.BandMembers = {WINDOW.getProperty(_P.BAND_MEM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.StreamTitle = {WINDOW.getProperty(_P.STREAM_TTL)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}", xbmc.LOGDEBUG)
                            
                            # Aktualisiere Kodi Player Metadaten (für Standard InfoLabels)
                            logo = WINDOW.getProperty(_P.LOGO)
                            self.update_player_metadata(artist if artist else None, 
                                                        title if title else None, 
                                                        album if album else station_name,
                                                        logo if logo else None,
                                                        mbid if mbid else None)
                            
                            # DEBUG: Zeige was Kodi Player hat
                            try:
                                if self.player.isPlayingAudio():
                                    info_tag = self.player.getMusicInfoTag()
                                    xbmc.log(f"[{ADDON_NAME}] === KODI PLAYER INFOTAGS ===", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Artist = {info_tag.getArtist()}", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Title = {info_tag.getTitle()}", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Album = {info_tag.getAlbum()}", xbmc.LOGDEBUG)
                            except Exception as e:
                                xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen Player InfoTags: {str(e)}", xbmc.LOGDEBUG)
                            
                            xbmc.log(f"[{ADDON_NAME}] ========================", xbmc.LOGDEBUG)
                            
                            # Versuche die MusicPlayer InfoLabels zu überschreiben
                            # indem wir die JSON-RPC API nutzen
                            try:
                                json_query = {
                                    "jsonrpc": "2.0",
                                    "method": "JSONRPC.NotifyAll",
                                    "params": {
                                        "sender": "service.audio.stream.monitor",
                                        "message": "UpdateMusicInfo",
                                        "data": {
                                            "artist": artist,
                                            "title": title,
                                            "streamtitle": stream_title,
                                            "mbid": mbid if mbid else ""
                                        }
                                    },
                                    "id": 1
                                }
                                xbmc.executeJSONRPC(json.dumps(json_query))
                            except Exception as e:
                                xbmc.log(f"[{ADDON_NAME}] Fehler bei JSON-RPC Notify: {str(e)}", xbmc.LOGDEBUG)
                            
                            xbmc.log(f"[{ADDON_NAME}] Neuer Titel: {stream_title} (Artist: {artist if artist else 'N/A'}, Title: {title if title else 'N/A'}, Album: {album if album else 'N/A'})", xbmc.LOGINFO)

                    # Song-Timeout: Properties löschen wenn der Song abgelaufen ist.
                    # Läuft jede Iteration (~1s) – kein extra Thread notwendig.
                    if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                        xbmc.log(
                            f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                            xbmc.LOGDEBUG
                        )
                        WINDOW.clearProperty(_P.ARTIST)
                        WINDOW.clearProperty(_P.TITLE)
                        WINDOW.clearProperty(_P.ALBUM)
                        WINDOW.clearProperty(_P.ALBUM_DATE)
                        WINDOW.clearProperty(_P.MBID)
                        WINDOW.clearProperty(_P.FIRST_REL)
                        WINDOW.clearProperty(_P.BAND_FORM)
                        WINDOW.clearProperty(_P.BAND_MEM)
                        WINDOW.clearProperty(_P.GENRE)
                        WINDOW.clearProperty(_P.STREAM_TTL)
                        self._last_song_time = 0.0  # Verhindert wiederholtes Löschen

                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata-Loop (Thread läuft weiter): {str(e)}", xbmc.LOGERROR)
                    time.sleep(1)
                    continue

        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Stream-Response konnte nicht geschlossen werden: {e}", xbmc.LOGDEBUG)
            xbmc.log(f"[{ADDON_NAME}] Metadata Worker beendet", xbmc.LOGDEBUG)
            
    def start_metadata_monitoring(self, url):
        """Startet das Metadata-Monitoring in einem separaten Thread"""
        self.stop_metadata_monitoring()
        
        # Reset flags
        self.use_api_fallback = False
        self.stop_thread = False
        self.metadata_generation += 1
        generation = self.metadata_generation
        
        self.metadata_thread = threading.Thread(target=self.metadata_worker, args=(url, generation))
        self.metadata_thread.daemon = True
        self.metadata_thread.start()
        
    def stop_metadata_monitoring(self):
        """Stoppt das Metadata-Monitoring"""
        if self.metadata_thread and self.metadata_thread.is_alive():
            self.stop_thread = True
            self.metadata_generation += 1
            self.metadata_thread.join(timeout=0.5)  # kurz warten, Thread bricht selbst ab da is_playing=False
            if not self.metadata_thread.is_alive():
                self.metadata_thread = None
            
    def check_playing(self):
        """Überprüft, was gerade abgespielt wird"""
        if self.player.isPlaying():
            try:
                # Nur Audio-Streams überwachen, kein Video
                if not self.player.isPlayingAudio():
                    if self.is_playing:
                        self.is_playing = False
                        self.current_url = None
                        self.stop_metadata_monitoring()
                        self.clear_properties()
                        xbmc.log(f"[{ADDON_NAME}] Video läuft - kein Radio-Monitoring", xbmc.LOGDEBUG)
                    return

                # URL des aktuellen Streams
                playing_file = self.player.getPlayingFile()
                
                # Prüfen ob es ein Stream ist (http/https)
                if playing_file.startswith('http://') or playing_file.startswith('https://'):
                    
                    if playing_file != self.current_url:
                        self.current_url = playing_file
                        self.is_playing = True
                        title = None
                        artist = None
                        album = None
                        WINDOW.clearProperty(_P.MBID)
                        WINDOW.clearProperty(_P.ALBUM)
                        WINDOW.clearProperty(_P.STATION)
                        
                        # Basis-Informationen aus dem Player
                        try:
                            info_tag = self.player.getMusicInfoTag()
                            title = info_tag.getTitle()
                            artist = info_tag.getArtist()
                            album = info_tag.getAlbum()
                            
                            # Hole das Logo/Thumbnail vom aktuellen Item
                            # Prüfe verschiedene Quellen in Prioritätsreihenfolge
                            logo = None
                            
                            # 1. HÖCHSTE Priorität: ListItem.Icon (echtes Logo vom Addon, BEVOR Kodi es cached)
                            listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                            if self.is_real_logo(listitem_icon):
                                logo = listitem_icon
                                self.station_logo = logo
                                xbmc.log(f"[{ADDON_NAME}] Logo vom ListItem.Icon: {logo}", xbmc.LOGINFO)
                            
                            # 2. Fallback: Window-Property vom radio.de Addon
                            if not logo:
                                radiode_logo = WINDOW.getProperty('RadioDE.StationLogo')
                                if self.is_real_logo(radiode_logo):
                                    logo = radiode_logo
                                    self.station_logo = logo
                                    xbmc.log(f"[{ADDON_NAME}] Logo vom radio.de Addon (Window-Property): {logo}", xbmc.LOGINFO)
                            
                            # 3. Fallback: Player Art
                            if not logo:
                                for source in ['Player.Art(poster)', 'Player.Icon', 'Player.Art(thumb)', 'MusicPlayer.Cover']:
                                    player_logo = xbmc.getInfoLabel(source)
                                    if self.is_real_logo(player_logo):
                                        logo = player_logo
                                        self.station_logo = logo
                                        xbmc.log(f"[{ADDON_NAME}] Logo von {source}: {logo}", xbmc.LOGINFO)
                                        break

                            if not self.station_logo or not self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Kein Player-Logo, wird spaeter von API geholt", xbmc.LOGDEBUG)
                            
                            # Nur Title als vorläufige Info setzen.
                            # RadioMonitor.Artist und RadioMonitor.Album werden bewusst NICHT gesetzt.
                            # Artist ist der Trigger für AS, und ohne MBID würde AS mit falschen Daten starten.
                            # Album wird erst nach erfolgreicher MB-Query gesetzt.
                            # Artist, Album + MBID werden zusammen vom Metadata-Worker gesetzt.
                            if title:
                                self.set_property_safe(_P.TITLE, title)
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            if self.station_logo and self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Logo gesetzt: {self.station_logo}", xbmc.LOGINFO)
                            else:
                                xbmc.log(f"[{ADDON_NAME}] Kein echtes Logo, nutze Kodi-Fallback", xbmc.LOGDEBUG)
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen von InfoTag/Logo beim Stream-Start: {e}", xbmc.LOGDEBUG)
                        if album and (not self.station_logo or self.station_logo == 'DefaultAudio.png'):
                            try:
                                xbmc.log(f"[{ADDON_NAME}] Hole Station-Logo für: {album}", xbmc.LOGDEBUG)
                                # Suche Station in radio.de API
                                search_name = album
                                search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
                                search_name = search_name.strip()
                                
                                params = {'query': search_name, 'count': 5}
                                response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                                data = response.json()
                                
                                if 'playables' in data and len(data['playables']) > 0:
                                    # Nimm erste Station
                                    station = data['playables'][0]
                                    logo_url = station.get('logo300x300', '')
                                    if logo_url:
                                        self.station_logo = logo_url
                                        self.set_property_safe(_P.LOGO, logo_url)
                                        xbmc.log(f"[{ADDON_NAME}] Station-Logo gefunden: {logo_url}", xbmc.LOGINFO)
                            except Exception as e:
                                xbmc.log(f"[{ADDON_NAME}] Fehler beim Holen des Station-Logos: {str(e)}", xbmc.LOGDEBUG)
                        
                        # Playing-Flag setzen
                        WINDOW.setProperty(_P.PLAYING, 'true')
                        
                        xbmc.log(f"[{ADDON_NAME}] === STREAM GESTARTET - INITIAL STATE ===", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = true", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}", xbmc.LOGDEBUG)
                        
                        # Zeige was vom Player kommt
                        try:
                            if self.player.isPlayingAudio():
                                info_tag = self.player.getMusicInfoTag()
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Artist = {info_tag.getArtist()}", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Title = {info_tag.getTitle()}", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Album = {info_tag.getAlbum()}", xbmc.LOGDEBUG)
                        except Exception:
                            pass
                        xbmc.log(f"[{ADDON_NAME}] ========================================", xbmc.LOGINFO)
                        
                        # ICY-Metadaten-Monitoring starten
                        self.start_metadata_monitoring(playing_file)

                        xbmc.log(f"[{ADDON_NAME}] Stream erkannt: {playing_file}", xbmc.LOGINFO)
                else:
                    # Kein Stream - Properties löschen
                    if self.is_playing:
                        self.is_playing = False
                        self.current_url = None
                        self.stop_metadata_monitoring()
                        self.clear_properties()
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Fehler beim Überprüfen des Players: {str(e)}", xbmc.LOGERROR)
        else:
            # Nichts wird abgespielt
            if self.is_playing:
                self.is_playing = False
                self.current_url = None
                self.stop_metadata_monitoring()
                self.clear_properties()
                xbmc.log(f"[{ADDON_NAME}] Wiedergabe gestoppt", xbmc.LOGINFO)
                
    def run(self):
        """Haupt-Loop des Services"""
        # Initial properties löschen
        self.clear_properties()
        
        # Haupt-Loop
        while not self.abortRequested():
            # Alle 2 Sekunden überprüfen
            if self.waitForAbort(2):
                break
                
            self.check_playing()
            
        # Cleanup beim Beenden
        self.stop_metadata_monitoring()
        self.clear_properties()
        xbmc.log(f"[{ADDON_NAME}] Service beendet", xbmc.LOGINFO)

if __name__ == '__main__':
    monitor = RadioMonitor()
    monitor.run()
