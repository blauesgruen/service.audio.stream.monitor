import xbmc
import xbmcaddon
import xbmcgui
import requests
import re
import time
import threading
import json
from urllib.parse import urlparse

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_VERSION = ADDON.getAddonInfo('version')

# MusicBrainz API: User-Agent erforderlich (Richtlinie), ~1 Request/Sekunde
MUSICBRAINZ_HEADERS = {
    "User-Agent": f"RadioMonitorLight/{ADDON_VERSION} (https://github.com; Kodi addon {ADDON_ID})"
}

# Ungültige Metadaten-Werte (StreamTitle/Artist/Title)
INVALID_METADATA_VALUES = ['Unknown', 'Radio Stream', 'Internet Radio']

# Standard HTTP-Header für externe APIs (radio.de etc.)
DEFAULT_HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def _musicbrainz_escape(s):
    """
    Für MusicBrainz-Query in Anführungszeichen: Doppelanführungen escapen,
    Apostroph durch Leerzeichen ersetzen (vermeidet OSError 22 / Invalid argument unter macOS).
    """
    if not s:
        return s
    s = str(s).replace('\\', '\\\\').replace('"', '\\"')
    s = s.replace("'", " ")  # Apostroph kann Request auf manchen Systemen kaputt machen
    return s.strip() or " "


def _musicbrainz_artist_score(name):
    """Prüft ob name ein bekannter Artist in MusicBrainz ist; liefert Score (0 = unbekannt)."""
    if not name or not name.strip():
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Artist-Score 0: Name leer", xbmc.LOGDEBUG)
        return 0
    url = "https://musicbrainz.org/ws/2/artist/"
    safe = _musicbrainz_escape(name)
    params = {"query": f'artist:"{safe}"', "fmt": "json", "limit": 1}
    try:
        r = requests.get(url, params=params, headers=MUSICBRAINZ_HEADERS, timeout=5)
        data = r.json()
        artists = data.get("artists", [])
        if artists:
            return int(artists[0].get("score", 0))
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Artist-Score 0: keine Treffer für '{name}'", xbmc.LOGDEBUG)
    except Exception as e:
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Artist-Score 0: Fehler - {e}", xbmc.LOGDEBUG)
    return 0


def _musicbrainz_recording_score(artist_name, recording_name):
    """
    Prüft ob es in MusicBrainz ein Recording mit diesem Künstler und diesem Titel gibt.
    Liefert Score des besten Treffers (0 = kein passendes Recording).
    """
    if not artist_name or not recording_name:
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Recording-Score 0: Artist oder Titel leer", xbmc.LOGDEBUG)
        return 0
    url = "https://musicbrainz.org/ws/2/recording/"
    safe_artist = _musicbrainz_escape(artist_name)
    safe_recording = _musicbrainz_escape(recording_name)
    params = {
        "query": f'artist:"{safe_artist}" recording:"{safe_recording}"',
        "fmt": "json",
        "limit": 1,
    }
    try:
        r = requests.get(url, params=params, headers=MUSICBRAINZ_HEADERS, timeout=5)
        data = r.json()
        recordings = data.get("recordings", [])
        if recordings:
            return int(recordings[0].get("score", 0))
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Recording-Score 0: keine Treffer für '{artist_name}' / '{recording_name}'", xbmc.LOGDEBUG)
    except Exception as e:
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz Recording-Score 0: Fehler - {e}", xbmc.LOGDEBUG)
    return 0


def _parse_radiode_api_title(full_title, station_name=None):
    """
    Parst radio.de API Format "TITLE - ARTIST". Gibt (artist, title) zurück;
    ungültige Werte werden zu ''/None. station_name wird als ungültiger Title gefiltert.
    """
    invalid = INVALID_METADATA_VALUES + ['']
    if not full_title or ' - ' not in full_title:
        return None, None
    parts = full_title.split(' - ', 1)
    title = parts[0].strip()
    artist = parts[1].strip()
    if artist in invalid:
        artist = ''
    if title in invalid or (station_name and title == station_name):
        title = ''
    if title and re.match(r'^\d+\s*-\s*\d+$', title):
        return None, None
    return artist or None, title or None


def _identify_artist_title_via_musicbrainz(part1, part2):
    """
    Ermittelt Artist/Title per MusicBrainz. Aufrufer hat part1/part2 bereits getrennt;
    Sonderzeichen werden nur für die API-Query bereinigt (_musicbrainz_escape), Rückgabe
    sind die originalen part1/part2.

    Ablauf (wenig Traffic):
    1. Eine Recording-Suche: part1=Artist, part2=Title. Bei Treffer (score>0) sofort return.
    2. Nur wenn 0: zweite Recording-Suche (part2=Artist, part1=Title). Bei Treffer return.
    3. Beide 0: Standard (part1=Artist, part2=Title), keine weiteren Requests.
    4. Nur bei Gleichstand (beide Scores >0): zwei Artist-Suchen als Tie-Breaker.
    Pro Titel also 1–2 Recording-Requests, ggf. +2 Artist-Requests. limit=1, timeout=5s.
    """
    rec_1_2 = _musicbrainz_recording_score(part1, part2)
    if rec_1_2 > 0:
        return part1, part2, False
    time.sleep(1)  # MusicBrainz Rate-Limit ~1 req/s
    rec_2_1 = _musicbrainz_recording_score(part2, part1)
    if rec_2_1 > rec_1_2:
        return part2, part1, False
    if rec_1_2 == 0 and rec_2_1 == 0:
        return part1, part2, True
    score1 = _musicbrainz_artist_score(part1)
    time.sleep(1)
    score2 = _musicbrainz_artist_score(part2)
    if score1 > score2:
        return part1, part2, False
    if score2 > score1:
        return part2, part1, False
    return part1, part2, True


# Window-Properties für die Skin
WINDOW = xbmcgui.Window(10000)  # Home window

class PlayerMonitor(xbmc.Player):
    """Monitor für Player-Events um Logo SOFORT beim Stream-Start zu erfassen"""
    def __init__(self, radio_monitor):
        super(PlayerMonitor, self).__init__()
        self.radio_monitor = radio_monitor
    
    def onAVStarted(self):
        """Wird aufgerufen SOFORT wenn Stream startet - ListItem.Icon ist noch verfügbar!"""
        try:
            if self.isPlayingAudio():
                playing_file = self.getPlayingFile()
                
                # Nur bei HTTP/HTTPS Streams
                if playing_file.startswith('http://') or playing_file.startswith('https://'):
                    # SOFORT Logo vom ListItem lesen (bevor Kodi es überschreibt!)
                    listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                    
                    if listitem_icon and self.radio_monitor.is_real_logo(listitem_icon):
                        self.radio_monitor.station_logo = listitem_icon
                        xbmc.log(f"[{ADDON_NAME}] ⚡ Logo SOFORT beim Start erfasst: {listitem_icon}", xbmc.LOGINFO)
                    else:
                        xbmc.log(f"[{ADDON_NAME}] ⚠ ListItem.Icon beim Start: {listitem_icon}", xbmc.LOGDEBUG)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onAVStarted: {str(e)}", xbmc.LOGERROR)


class RadioMonitor(xbmc.Monitor):
    def __init__(self):
        super(RadioMonitor, self).__init__()
        self.player = xbmc.Player()
        self.is_playing = False
        self.current_url = None
        self.metadata_thread = None
        self.stop_thread = False
        self.station_id = None  # radio.de Station ID
        self.station_logo = None  # Logo URL von radio.de API
        self.use_api_fallback = False  # Flag für API-Fallback
        
        # Event-Handler für Player-Events
        self.player_monitor = PlayerMonitor(self)
        
        xbmc.log(f"[{ADDON_NAME}] Service gestartet", xbmc.LOGINFO)
        
    def clear_properties(self):
        """Löscht alle Radio-Properties"""
        # Reset Logo
        self.station_logo = None


        
        # Lösche auch radio.de Addon Properties
        WINDOW.clearProperty('RadioDE.StationLogo')
        WINDOW.clearProperty('RadioDE.StationName')
        
        # Window-Properties (für Fallback)
        WINDOW.clearProperty('RadioMonitor.Station')
        WINDOW.clearProperty('RadioMonitor.Title')
        WINDOW.clearProperty('RadioMonitor.Artist')
        WINDOW.clearProperty('RadioMonitor.Album')
        WINDOW.clearProperty('RadioMonitor.Genre')
        WINDOW.clearProperty('RadioMonitor.StreamTitle')
        WINDOW.clearProperty('RadioMonitor.Playing')
        WINDOW.clearProperty('RadioMonitor.Logo')
        
        # MusicPlayer-Properties (Kodi-Standard)
        # Diese können mit MusicPlayer.Property(Artist) in Skins abgerufen werden
        if self.player.isPlayingAudio():
            try:
                self.player.clearProperty('Artist')
                self.player.clearProperty('Title')
                self.player.clearProperty('Album')
                self.player.clearProperty('Genre')
                self.player.clearProperty('StreamTitle')
            except Exception:
                pass
        
        xbmc.log(f"[{ADDON_NAME}] Properties gelöscht", xbmc.LOGDEBUG)
        
    def set_property_safe(self, key, value):
        """Setzt Property nur wenn Wert vorhanden"""
        if value and value != "":
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
            self.set_property_safe('RadioMonitor.Logo', self.station_logo)
        else:
            # Kein echtes Logo → Property leer lassen (Kodi nutzt automatisch Fallback)
            WINDOW.clearProperty('RadioMonitor.Logo')
    
    def update_player_metadata(self, artist, title, station, logo=None):
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
            if station:
                info_tag.setAlbum(station)  # Station als Album
            
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
            
    def get_radiode_nowplaying(self, url):
        """Holt aktuelle Song-Info von radio.de API wenn im Stream-Parameter"""
        try:
            # radio.de Streams haben oft einen Parameter im URL
            # Versuche die Station-ID zu extrahieren
            if 'radiode' in url.lower() or 'radio.de' in url.lower() or 'radio-de' in url.lower():
                xbmc.log(f"[{ADDON_NAME}] radio.de Stream erkannt, versuche alternative Metadaten-Quelle", xbmc.LOGDEBUG)
                
                # Fallback: Stream-URL für Sender-Erkennung (z.B. stream.berliner-rundfunk.de/...)
                match = re.search(r'stream\.([^/]+)\.de/([^/]+)', url)
                if not match:
                    match = re.search(r'//([^/]+)/([^/]+)', url)
                
                if match:
                    domain = match.group(1)
                    station_slug = match.group(2)
                    station_name = station_slug.replace('-', ' ').replace('_', ' ').title()
                    
                    # Bereinige den Namen
                    station_name = station_name.replace('Brf ', 'Berliner Rundfunk ')
                    station_name = station_name.replace('100prozent', '100%')
                    
                    self.set_property_safe('RadioMonitor.Station', station_name)
                    xbmc.log(f"[{ADDON_NAME}] Station aus URL erkannt: {station_name}", xbmc.LOGDEBUG)
                    
                    # Setze Flag für API-Fallback
                    self.use_api_fallback = True
                    self.station_slug = station_slug
                    
                    return station_name
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei radio.de Metadaten-Extraktion: {str(e)}", xbmc.LOGDEBUG)
        return None
    
    def get_nowplaying_from_apis(self, station_name, stream_url):
        """Versucht nowPlaying von verschiedenen APIs zu holen"""
        
        xbmc.log(f"[{ADDON_NAME}] API-Fallback gestartet für Station: '{station_name}'", xbmc.LOGDEBUG)
        
        # 1. Versuche radio.de API
        artist, title = self.get_radiode_api_nowplaying(station_name)
        if artist or title:
            xbmc.log(f"[{ADDON_NAME}] ✓ radio.de API: {artist} - {title}", xbmc.LOGINFO)
            return artist, title
        
        # 2. Versuch: Sender-spezifische APIs basierend auf URL/Name
        # NRJ Sender
        if 'nrj' in stream_url.lower() or 'nrj' in station_name.lower():
            xbmc.log(f"[{ADDON_NAME}] Versuche NRJ-spezifische API", xbmc.LOGDEBUG)
            artist, title = self.get_nrj_nowplaying(station_name)
            if artist or title:
                return artist, title
        
        # Energy Sender
        if 'energy' in stream_url.lower() or 'energy' in station_name.lower():
            # Energy hat meist gute ICY-Metadaten, daher kein extra API nötig
            pass
        
        # 3. Versuch: Kodi Player InfoTags als letzter Ausweg
        try:
            if self.player.isPlayingAudio():
                info_tag = self.player.getMusicInfoTag()
                title = info_tag.getTitle()
                artist = info_tag.getArtist()
                
                invalid_values = INVALID_METADATA_VALUES + ['', station_name]
                if title and title not in invalid_values:
                    # Filter Zahlen-IDs
                    if re.match(r'^\d+\s*-\s*\d+$', title):
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
        """Einfache Trennung ohne API-Aufrufe (für Rekursion)"""
        if not stream_title or stream_title == "":
            return None, None
        
        # Verschiedene Trennzeichen versuchen
        separators = [' - ', ' – ', ' — ', ' | ', ': ']
        
        for sep in separators:
            if sep in stream_title:
                parts = stream_title.split(sep, 1)
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = parts[1].strip()
                    return artist, title
        
        return None, stream_title.strip()
    
    def get_nrj_nowplaying(self, station_name):
        """Versucht nowPlaying von NRJ-Sendern zu holen"""
        try:
            # NRJ hat verschiedene Webradios, versuche die generische API
            # Format kann variieren, hier ein Ansatz basierend auf öffentlichen Endpoints
            
            # Suche nach NRJ-spezifischen Stream-IDs oder Namen
            # Dies ist ein Platzhalter - müsste für jeden NRJ-Sender angepasst werden
            
            xbmc.log(f"[{ADDON_NAME}] Versuche NRJ-spezifische Metadaten für '{station_name}'", xbmc.LOGDEBUG)
            
            # NRJ-Sender werden über radio.de API + NRJ→ENERGY Fallback abgedeckt (siehe get_radiode_api_nowplaying())
            # Diese Funktion bleibt als Platzhalter falls zukünftig direkte NRJ-API gefunden wird
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei NRJ API: {str(e)}", xbmc.LOGDEBUG)
        
        return None, None
    
    def get_radiobrowser_api_nowplaying(self, station_name):
        """Radio-Browser API hat keine nowPlaying Daten - nur für zukünftige Erweiterungen"""
        # Radio-Browser ist eine Stations-Datenbank, keine Live-Metadaten-Quelle
        # Diese Funktion bleibt als Platzhalter für zukünftige Erweiterungen
        # (z.B. um Station-Homepage zu finden und dann dort zu scrapen)
        return None, None
    
    def get_radiode_api_nowplaying(self, station_name):
        """Holt aktuelle Song-Info direkt von der radio.de API"""
        try:
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
            
            search_url = f"https://prod.radio-api.net/stations/search?query={search_name.replace(' ', '+')}&count=20"
            response = requests.get(search_url, headers=DEFAULT_HTTP_HEADERS, timeout=5)
            data = response.json()
            
            xbmc.log(f"[{ADDON_NAME}] Search API: {data.get('totalCount', 0)} Treffer", xbmc.LOGDEBUG)
            
            # SCHRITT 2: Finde beste Übereinstimmung
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
                            xbmc.log(f"[{ADDON_NAME}] Wort-Match: '{station_found}' - Score: {score} (Wörter: {matching_words})", xbmc.LOGDEBUG)
                
                if best_match and best_match_score > 0:
                    station_found = best_match.get('name', '')
                    station_id = best_match.get('id', '')
                    station_logo = best_match.get('logo300x300', '')  # Logo aus API
                    
                    # Speichere Logo für spätere Verwendung
                    if station_logo:
                        self.station_logo = station_logo
                        self.set_property_safe('RadioMonitor.Logo', station_logo)
                        xbmc.log(f"[{ADDON_NAME}] Station-Logo aus API: {station_logo}", xbmc.LOGINFO)
                    
                    xbmc.log(f"[{ADDON_NAME}] Beste Übereinstimmung: '{station_found}' (Score: {best_match_score}, ID: {station_id})", xbmc.LOGDEBUG)
                    
                    # SCHRITT 2: Nutze die gefundene Station-ID für now-playing API
                    if station_id:
                        xbmc.log(f"[{ADDON_NAME}] Hole Now-Playing von: https://api.radio.de/stations/now-playing?stationIds={station_id}", xbmc.LOGDEBUG)
                        
                        try:
                            nowplaying_url = f"https://api.radio.de/stations/now-playing?stationIds={station_id}"
                            np_response = requests.get(nowplaying_url, headers=DEFAULT_HTTP_HEADERS, timeout=5)
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
                                    
                                    # FALLBACK: NRJ-Sender nutzen oft denselben Stream wie ENERGY-Sender
                                    # Versuche "NRJ" durch "ENERGY" zu ersetzen und nochmal zu suchen
                                    if 'NRJ' in station_found.upper():
                                        xbmc.log(f"[{ADDON_NAME}] Versuche NRJ→ENERGY Fallback für '{station_found}'", xbmc.LOGDEBUG)
                                        alternative_name = station_found.replace('NRJ', 'ENERGY').replace('nrj', 'ENERGY')
                                        
                                        # Suche nach der ENERGY-Variante
                                        alt_search_url = f"https://prod.radio-api.net/stations/search?query={alternative_name.replace(' ', '+')}&count=10"
                                        alt_response = requests.get(alt_search_url, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                                        alt_data = alt_response.json()
                                        
                                        if 'playables' in alt_data and len(alt_data['playables']) > 0:
                                            alt_station = alt_data['playables'][0]
                                            alt_id = alt_station.get('id', '')
                                            alt_name = alt_station.get('name', '')
                                            xbmc.log(f"[{ADDON_NAME}] Alternative Station gefunden: '{alt_name}' (ID: {alt_id})", xbmc.LOGDEBUG)
                                            
                                            if alt_id:
                                                alt_np_url = f"https://api.radio.de/stations/now-playing?stationIds={alt_id}"
                                                alt_np_response = requests.get(alt_np_url, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                                                
                                                if alt_np_response.status_code == 200:
                                                    alt_np_data = alt_np_response.json()
                                                    xbmc.log(f"[{ADDON_NAME}] Alternative now-playing Response: {alt_np_data}", xbmc.LOGDEBUG)
                                                    
                                                    if isinstance(alt_np_data, list) and len(alt_np_data) > 0:
                                                        alt_track = alt_np_data[0]
                                                        alt_full_title = alt_track.get('title', '')
                                                        artist, title = _parse_radiode_api_title(alt_full_title, alt_name)
                                                        if artist and title:
                                                            xbmc.log(f"[{ADDON_NAME}] ✓ Alternative API erfolgreich: {artist} - {title}", xbmc.LOGINFO)
                                                            return artist, title
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
    
    def api_metadata_worker(self):
        """Fallback: Pollt verschiedene APIs wenn keine ICY-Metadaten verfügbar"""
        xbmc.log(f"[{ADDON_NAME}] API Metadata Worker gestartet (Fallback-Modus)", xbmc.LOGDEBUG)
        
        last_title = ""
        poll_interval = 10  # Sekunden zwischen API-Abfragen
        station_name = WINDOW.getProperty('RadioMonitor.Station')
        stream_url = self.current_url or ''
        
        try:
            while not self.stop_thread and self.is_playing and self.use_api_fallback:
                # Versuche verschiedene APIs
                if station_name:
                    artist, title = self.get_nowplaying_from_apis(station_name, stream_url)
                    
                    if title and title != last_title:
                        last_title = title
                        
                        # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                        self.set_logo_safe()
                        
                        if artist:
                            self.set_property_safe('RadioMonitor.Artist', artist)
                            self.set_property_safe('RadioMonitor.Title', title)
                            self.set_property_safe('RadioMonitor.StreamTitle', f"{artist} - {title}")
                            xbmc.log(f"[{ADDON_NAME}] API Update: {artist} - {title}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty('RadioMonitor.Logo')
                            self.update_player_metadata(artist, title, station_name, logo if logo else None)
                        else:
                            WINDOW.clearProperty('RadioMonitor.Artist')
                            self.set_property_safe('RadioMonitor.Title', title)
                            self.set_property_safe('RadioMonitor.StreamTitle', title)
                            xbmc.log(f"[{ADDON_NAME}] API Update: {title}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty('RadioMonitor.Logo')
                            self.update_player_metadata(None, title, station_name, logo if logo else None)
                
                # Warte vor nächster Abfrage
                for _ in range(poll_interval * 2):  # 10 Sekunden in 0.5s Schritten
                    if self.stop_thread or not self.is_playing:
                        break
                    time.sleep(0.5)
                
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler im API Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            xbmc.log(f"[{ADDON_NAME}] API Metadata Worker beendet", xbmc.LOGDEBUG)
    
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
            icy_url = response.headers.get('icy-url', '')
            
            # Hole den korrekten Stationsnamen (bevorzuge MusicPlayer.Album vom Addon)
            station_name = icy_name  # Fallback
            try:
                if self.player.isPlayingAudio():
                    info_tag = self.player.getMusicInfoTag()
                    album_name = info_tag.getAlbum()
                    if album_name and album_name.strip():
                        station_name = album_name.strip()
                        xbmc.log(f"[{ADDON_NAME}] Verwende MusicPlayer.Album als Station: '{station_name}' (statt ICY: '{icy_name}')", xbmc.LOGINFO)
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen von MusicPlayer.Album: {str(e)}", xbmc.LOGDEBUG)
            
            if station_name:
                self.set_property_safe('RadioMonitor.Station', station_name)
                xbmc.log(f"[{ADDON_NAME}] Station: {station_name}", xbmc.LOGDEBUG)
            
            if icy_genre:
                self.set_property_safe('RadioMonitor.Genre', icy_genre)
                xbmc.log(f"[{ADDON_NAME}] Genre: {icy_genre}", xbmc.LOGDEBUG)
            
            # Metaint - Position der Metadaten im Stream
            metaint = response.headers.get('icy-metaint')
            if not metaint:
                xbmc.log(f"[{ADDON_NAME}] Kein icy-metaint Header gefunden - Stream sendet keine ICY-Metadaten", xbmc.LOGWARNING)
                # Versuche alternative Metadaten-Quelle
                self.get_radiode_nowplaying(url)
                response.close()
                return None
                
            metaint = int(metaint)
            xbmc.log(f"[{ADDON_NAME}] MetaInt: {metaint}", xbmc.LOGDEBUG)
            
            return {'metaint': metaint, 'response': response, 'station': station_name, 'genre': icy_genre}
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Abrufen der ICY-Metadaten: {str(e)}", xbmc.LOGERROR)
            # Versuche trotzdem Sender-Info zu extrahieren
            self.get_radiode_nowplaying(url)
            return None
            
    def extract_stream_title(self, metadata_raw):
        """Extrahiert den StreamTitle aus den rohen Metadaten"""
        try:
            # Format: StreamTitle='Artist - Title';
            # Wichtig: Non-greedy .*? bis zum letzten ' vor ; um Apostrophe in Titeln zu unterstützen
            match = re.search(r"StreamTitle='(.*?)';", metadata_raw)
            if match:
                return match.group(1)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Extrahieren des StreamTitle: {str(e)}", xbmc.LOGERROR)
        return None
        
    def parse_stream_title(self, stream_title, station_name=None, stream_url=None):
        """Trennt Artist und Title"""
        
        # Bei leerem oder fehlendem StreamTitle → keine Metadaten
        if not stream_title or stream_title == "":
            xbmc.log(f"[{ADDON_NAME}] StreamTitle ist leer", xbmc.LOGDEBUG)
            return None, None
        
        if stream_title in INVALID_METADATA_VALUES:
            xbmc.log(f"[{ADDON_NAME}] StreamTitle ist ungültig: '{stream_title}'", xbmc.LOGDEBUG)
            return None, None
        
        # FILTER: Nur Zahlen (z.B. "216093 - 221338") → interne IDs, keine Song-Infos
        if re.match(r'^\d+\s*-\s*\d+$', stream_title):
            xbmc.log(f"[{ADDON_NAME}] StreamTitle enthält nur Zahlen (Track-ID): '{stream_title}'", xbmc.LOGDEBUG)
            # Bei Zahlen-IDs: Versuche API
            if station_name and stream_url:
                api_artist, api_title = self.get_nowplaying_from_apis(station_name, stream_url)
                if api_artist and api_title:
                    xbmc.log(f"[{ADDON_NAME}] API lieferte Metadaten (Zahlen-ID Fallback): {api_artist} - {api_title}", xbmc.LOGINFO)
                    return api_artist, api_title
            return None, None
        
        # Eindeutiges Format: "Titel" von Interpret → immer Title vor, Artist nach "von"
        von_match = re.match(r'^"?(.+?)"?\s+von\s+(.+)$', stream_title, re.IGNORECASE)
        if von_match:
            title = von_match.group(1).strip()
            artist = von_match.group(2).strip()
            xbmc.log(f"[{ADDON_NAME}] 'von' Format erkannt: Title='{title}', Artist='{artist}'", xbmc.LOGDEBUG)
            return artist, title
        
        # 1) Trennen: part1 / part2 (noch unbestimmt welches Artist/Title)
        # 2) MusicBrainz: ermittelt welcher Part = Artist; Sonderzeichen werden dort nur für die API-Query bereinigt, Rückgabe sind Original-Strings
        separators = [' - ', ' – ', ' — ', ' | ', ': ']
        for sep in separators:
            if sep in stream_title:
                parts = stream_title.split(sep, 1)
                if len(parts) == 2:
                    part1 = parts[0].strip()
                    part2 = parts[1].strip()
                    artist, title, uncertain = _identify_artist_title_via_musicbrainz(part1, part2)
                    if uncertain:
                        xbmc.log(f"[{ADDON_NAME}] MusicBrainz unentschieden, nutze Standard: Artist='{artist}', Title='{title}'", xbmc.LOGDEBUG)
                    if artist in INVALID_METADATA_VALUES:
                        artist = None
                    if title in INVALID_METADATA_VALUES:
                        title = None
                    if not artist and not title:
                        return None, None
                    return artist, title
        
        if stream_title.strip() not in INVALID_METADATA_VALUES:
            return None, stream_title.strip()
        
        return None, None
        
    def metadata_worker(self, url):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        xbmc.log(f"[{ADDON_NAME}] Metadata Worker gestartet", xbmc.LOGDEBUG)
        
        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            xbmc.log(f"[{ADDON_NAME}] Keine ICY-Metadaten verfügbar - wechsle zu API-Fallback", xbmc.LOGWARNING)
            # Starte API-Fallback Worker
            if self.use_api_fallback:
                self.api_metadata_worker()
            return
            
        metaint = stream_info['metaint']
        response = stream_info['response']
        last_title = ""
        # Hinweis: response.raw.read() blockiert bis Daten da sind; bei Netzabbruch
        # kann das erst enden, wenn der Thread per stop_thread gestoppt wird.
        try:
            while not self.stop_thread and self.is_playing:
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
                    metadata_str = metadata.decode('utf-8', errors='ignore').strip('\x00')
                    
                    # KOMPLETT LOGGEN: Rohe ICY-Metadaten
                    if metadata_str:
                        xbmc.log(f"[{ADDON_NAME}] === ICY METADATA (ROH) ===", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] {metadata_str}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] =========================", xbmc.LOGDEBUG)
                    
                    stream_title = self.extract_stream_title(metadata_str)
                    
                    # Prüfe ob sich etwas geändert hat (auch leerer Titel zählt)
                    if stream_title != last_title:
                        last_title = stream_title
                        
                        xbmc.log(f"[{ADDON_NAME}] Neuer StreamTitle erkannt: '{stream_title}'", xbmc.LOGDEBUG)
                        
                        # Hole den korrekten Stationsnamen vom MusicPlayer (vom Addon, nicht vom Stream)
                        # Der ICY-Stream-Name ist oft falsch (z.B. "NRJ CLUBBIN" statt "ENERGY Clubbin'")
                        station_name = stream_info.get('station', '')  # Fallback: ICY-Name
                        try:
                            if self.player.isPlayingAudio():
                                info_tag = self.player.getMusicInfoTag()
                                album_name = info_tag.getAlbum()
                                if album_name and album_name.strip():
                                    station_name = album_name.strip()
                                    xbmc.log(f"[{ADDON_NAME}] Verwende MusicPlayer.Album als Stationsname: '{station_name}'", xbmc.LOGDEBUG)
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen von MusicPlayer.Album: {str(e)}", xbmc.LOGDEBUG)
                        
                        # Artist und Title trennen (mit Station Name und URL für API-Fallback)
                        artist, title = self.parse_stream_title(stream_title, station_name, url)
                        
                        # Wenn beide None sind (z.B. bei Zahlen-IDs ohne API-Daten), überspringe diesen Titel
                        if artist is None and title is None:
                            xbmc.log(f"[{ADDON_NAME}] Keine verwertbaren Metadaten für '{stream_title}' - RadioMonitor Properties bleiben leer", xbmc.LOGDEBUG)
                            # Properties komplett löschen, damit Skin auf MusicPlayer zurückfällt
                            WINDOW.clearProperty('RadioMonitor.Artist')
                            WINDOW.clearProperty('RadioMonitor.Title')
                            WINDOW.clearProperty('RadioMonitor.StreamTitle')
                            continue
                        
                        if stream_title not in INVALID_METADATA_VALUES:
                            self.set_property_safe('RadioMonitor.StreamTitle', stream_title)
                        
                        if artist:
                            self.set_property_safe('RadioMonitor.Artist', artist)
                            xbmc.log(f"[{ADDON_NAME}] Artist: {artist}", xbmc.LOGDEBUG)
                        else:
                            WINDOW.clearProperty('RadioMonitor.Artist')
                            artist = ''
                            
                        if title:
                            self.set_property_safe('RadioMonitor.Title', title)
                            xbmc.log(f"[{ADDON_NAME}] Title: {title}", xbmc.LOGDEBUG)
                        else:
                            WINDOW.clearProperty('RadioMonitor.Title')
                            title = ''
                        
                        # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                        self.set_logo_safe()
                        
                        # DEBUG: Zeige alle gesetzten Properties
                        xbmc.log(f"[{ADDON_NAME}] === PROPERTIES GESETZT ===", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = {WINDOW.getProperty('RadioMonitor.Playing')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty('RadioMonitor.Station')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty('RadioMonitor.Artist')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty('RadioMonitor.Title')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.StreamTitle = {WINDOW.getProperty('RadioMonitor.StreamTitle')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty('RadioMonitor.Genre')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty('RadioMonitor.Logo')}", xbmc.LOGINFO)
                        
                        # Aktualisiere Kodi Player Metadaten (für Standard InfoLabels)
                        logo = WINDOW.getProperty('RadioMonitor.Logo')
                        self.update_player_metadata(artist if artist else None, 
                                                    title if title else None, 
                                                    station_name if station_name else None,
                                                    logo if logo else None)
                        
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
                                    "sender": "service.monitor.radio_de_light",
                                    "message": "UpdateMusicInfo",
                                    "data": {
                                        "artist": artist,
                                        "title": title,
                                        "streamtitle": stream_title
                                    }
                                },
                                "id": 1
                            }
                            xbmc.executeJSONRPC(json.dumps(json_query))
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler bei JSON-RPC Notify: {str(e)}", xbmc.LOGDEBUG)
                        
                        xbmc.log(f"[{ADDON_NAME}] Neuer Titel: {stream_title} (Artist: {artist if artist else 'N/A'}, Title: {title if title else 'N/A'})", xbmc.LOGINFO)
                        
                        # Notification anzeigen (optional)
                        if artist and title:
                            notification_text = f"{artist} - {title}"
                        else:
                            notification_text = stream_title
                            
                        # Nur Notification zeigen, wenn in den Settings aktiviert
                        # xbmc.executebuiltin(f'Notification({stream_info.get("station", "Radio")}, {notification_text}, 5000)')
                        
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            try:
                response.close()
            except Exception:
                pass
            xbmc.log(f"[{ADDON_NAME}] Metadata Worker beendet", xbmc.LOGDEBUG)
            
    def start_metadata_monitoring(self, url):
        """Startet das Metadata-Monitoring in einem separaten Thread"""
        self.stop_metadata_monitoring()
        
        # Reset flags
        self.use_api_fallback = False
        self.stop_thread = False
        
        self.metadata_thread = threading.Thread(target=self.metadata_worker, args=(url,))
        self.metadata_thread.daemon = True
        self.metadata_thread.start()
        
    def stop_metadata_monitoring(self):
        """Stoppt das Metadata-Monitoring"""
        if self.metadata_thread and self.metadata_thread.is_alive():
            self.stop_thread = True
            self.metadata_thread.join(timeout=2)
            self.metadata_thread = None
            
    def check_playing(self):
        """Überprüft, was gerade abgespielt wird"""
        if self.player.isPlaying():
            try:
                # URL des aktuellen Streams
                playing_file = self.player.getPlayingFile()
                
                # Prüfen ob es ein Stream ist (http/https)
                if playing_file.startswith('http://') or playing_file.startswith('https://'):
                    
                    if playing_file != self.current_url:
                        self.current_url = playing_file
                        self.is_playing = True
                        
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
                            
                            # 4. Fallback: API-Logo (falls alle anderen leer sind)
                            if not self.station_logo or not self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Kein Player-Logo verfügbar, hole von API", xbmc.LOGDEBUG)
                            
                            # Diese Infos als Fallback setzen
                            if title:
                                self.set_property_safe('RadioMonitor.Title', title)
                            if artist:
                                self.set_property_safe('RadioMonitor.Artist', artist)
                            if album:
                                self.set_property_safe('RadioMonitor.Album', album)
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            if self.station_logo and self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Logo gesetzt: {self.station_logo}", xbmc.LOGINFO)
                            else:
                                xbmc.log(f"[{ADDON_NAME}] Kein echtes Logo, nutze Kodi-Fallback", xbmc.LOGDEBUG)
                        except Exception:
                            pass
                        
                        # Hole Logo von radio.de API (falls NDR/WDR/etc.) NUR wenn noch kein Logo vorhanden
                        if album and (not self.station_logo or self.station_logo == 'DefaultAudio.png'):
                            try:
                                xbmc.log(f"[{ADDON_NAME}] Hole Station-Logo für: {album}", xbmc.LOGDEBUG)
                                # Suche Station in radio.de API
                                search_name = album
                                search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
                                search_name = search_name.strip()
                                
                                search_url = f"https://prod.radio-api.net/stations/search?query={search_name.replace(' ', '+')}&count=5"
                                response = requests.get(search_url, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                                data = response.json()
                                
                                if 'playables' in data and len(data['playables']) > 0:
                                    # Nimm erste Station
                                    station = data['playables'][0]
                                    logo_url = station.get('logo300x300', '')
                                    if logo_url:
                                        self.station_logo = logo_url
                                        self.set_property_safe('RadioMonitor.Logo', logo_url)
                                        xbmc.log(f"[{ADDON_NAME}] Station-Logo gefunden: {logo_url}", xbmc.LOGINFO)
                            except Exception as e:
                                xbmc.log(f"[{ADDON_NAME}] Fehler beim Holen des Station-Logos: {str(e)}", xbmc.LOGDEBUG)
                        
                        # Playing-Flag setzen
                        WINDOW.setProperty('RadioMonitor.Playing', 'true')
                        
                        xbmc.log(f"[{ADDON_NAME}] === STREAM GESTARTET - INITIAL STATE ===", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = true", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty('RadioMonitor.Station')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty('RadioMonitor.Artist')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty('RadioMonitor.Title')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty('RadioMonitor.Logo')}", xbmc.LOGINFO)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty('RadioMonitor.Genre')}", xbmc.LOGINFO)
                        
                        # Zeige was vom Player kommt
                        try:
                            if self.player.isPlayingAudio():
                                info_tag = self.player.getMusicInfoTag()
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Artist = {info_tag.getArtist()}", xbmc.LOGINFO)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Title = {info_tag.getTitle()}", xbmc.LOGINFO)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Album = {info_tag.getAlbum()}", xbmc.LOGINFO)
                        except Exception:
                            pass
                        xbmc.log(f"[{ADDON_NAME}] ========================================", xbmc.LOGDEBUG)
                        
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
