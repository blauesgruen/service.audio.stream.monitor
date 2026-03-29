"""
Metadaten-Parsing & Normalisierung.

Zentrale Logik für das Trennen, Bereinigen und Normalisieren von Artist- und Titel-Informationen
aus ICY-Streams und APIs.
"""
import re
from typing import Tuple, List, Optional
from constants import INVALID_METADATA_VALUES, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE

# --- ICY Extraktion ---

def extract_stream_title(metadata_raw: str) -> Optional[str]:
    """
    Extrahiert den StreamTitle aus dem ICY-Roh-String.
    Format: StreamTitle='Artist - Title';
    """
    if not metadata_raw:
        return None
    try:
        # Wichtig: Non-greedy .*? bis zum letzten ' vor ; um Apostrophe in Titeln zu unterstützen
        match = re.search(r"StreamTitle='(.*?)';", metadata_raw)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


# --- Artist & Title Parsing ---

def parse_stream_title_simple(stream_title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Einfaches Parsing eines "Artist - Title" Strings.
    """
    if not stream_title or ' - ' not in stream_title:
        return None, stream_title
    parts = stream_title.split(' - ', 1)
    return parts[0].strip(), parts[1].strip()


def parse_stream_title_complex(stream_title: str, station_name: str = None) -> Tuple[Optional[str], Optional[str], bool, bool]:
    """
    Komplexe Trennung von Artist und Title aus dem ICY-StreamTitle.
    Gibt (artist, title, is_von_format, has_multiple_separators) zurück.
    """
    invalid = INVALID_METADATA_VALUES + ["", station_name]
    
    if not stream_title or stream_title in invalid or _NUMERIC_ID_RE.match(stream_title):
        return None, None, False, False

    # --- 'von'-Format ---
    # Beispiele:
    # - "Titel" von Artist
    # - Titel von Artist
    # - Titel von Artist JETZT AUF MDR JUMP
    von_match = re.match(r'^"(.+?)"\s+von\s+(.+)$', stream_title, re.IGNORECASE)
    if von_match:
        return von_match.group(2).strip(), von_match.group(1).strip(), True, False

    von_match_plain = re.match(
        r'^(.+?)\s+von\s+(.+?)(?:\s+JETZT\s+AUF\s+.+)?$',
        stream_title,
        re.IGNORECASE
    )
    if von_match_plain:
        title = von_match_plain.group(1).strip()
        artist = von_match_plain.group(2).strip()
        if title and artist:
            return artist, title, True, False

    # --- Trennzeichen-Erkennung ---
    separators = [' - ', ' – ', ' — ', ' | ', ': ']
    for sep in separators:
        if sep in stream_title:
            parts = stream_title.split(sep, 1)
            if len(parts) == 2:
                artist = parts[0].strip()
                title = parts[1].strip()
                has_multiple = (sep == ' - ' and stream_title.count(' - ') > 1)
                return artist, title, False, has_multiple
            break
            
    # Fallback: Alles als Title
    return None, stream_title.strip(), False, False


def get_last_separator_variant(stream_title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Erzeugt eine alternative Aufteilung am LETZTEN ' - ' Trenner.
    Hintergrund: Titel wie "'74 - '75" enthalten selbst ein ' - '.
    """
    if not stream_title or stream_title.count(' - ') <= 1:
        return None, None
        
    last_idx = stream_title.rfind(' - ')
    part1 = stream_title[:last_idx].strip()
    part2 = stream_title[last_idx + 3:].strip()
    return part1, part2


# --- Bereinigung & Normalisierung ---

def clean_title_part(part: str) -> str:
    """
    Bereinigt einen Titel-Teil von Metadaten-Tags in Klammern wie (Radio Edit), (Remastered) etc.
    Inhaltliche Klammern wie (Love Theme) bleiben erhalten.
    """
    if not part:
        return ""
    
    # Bekannte Metadaten-Keywords (kleingeschrieben)
    tags = [
        'radio edit', 'remaster', 'remix', 'mix', 'feat', 'ft.', 'version', 
        'original', 'extended', 'single', 'album', 'live', 'recorded',
        'digitally', '2011', '2012', '2013', '2014', '2015', '2016', '2017', '2018', '2019', '2020', '2021', '2022', '2023', '2024'
    ]
    
    # Wir suchen nach Klammern ( ... ), die eines dieser Keywords enthalten
    # Wir machen das iterativ, um mehrere Klammern zu behandeln
    result = part
    changed = True
    while changed:
        changed = False
        matches = re.finditer(r'\(([^)]+)\)', result)
        for m in matches:
            content = m.group(1).lower()
            if any(t in content for t in tags):
                # Diese Klammer entfernen
                span = m.span()
                result = result[:span[0]] + result[span[1]:]
                result = re.sub(r'\s+', ' ', result).strip()
                changed = True
                break
    return result


def get_artist_variants(artist: str) -> List[str]:
    """
    Erzeugt verschiedene Schreibweisen eines Künstlernamens für die Suche.
    (CamelCase Splitting, Komma-Drehung, Apostroph-Normalisierung)
    """
    if not artist:
        return []

    variants = []
    
    def add_v(val):
        val = val.strip()
        if val and val not in variants:
            variants.append(val)

    add_v(artist)
    
    # 1. Komma-Drehung ("Presley, Elvis" -> "Elvis Presley")
    if ',' in artist:
        parts = artist.split(',', 1)
        add_v(f"{parts[1]} {parts[0]}")

    # 2. CamelCase Splitting ("DeBurgh" -> "De Burgh")
    # Nur wenn Wort mit kleinem Buchstaben beginnt und dann Grossbuchstabe kommt
    camel = re.sub(r'([a-z])([A-Z])', r'\1 \2', artist)
    if camel != artist:
        add_v(camel)

    # 3. Apostroph-Normalisierung
    apo_norm = artist.replace("'", "’")
    if apo_norm != artist:
        add_v(apo_norm)
    
    apo_rem = artist.replace("'", "").replace("’", "")
    if apo_rem != artist:
        add_v(apo_rem)

    # 4. Slash-Normalisierung:
    # "AC / DC" -> "AC/DC" (liefert bei MusicBrainz deutlich bessere Treffer).
    if '/' in artist:
        slash_compact = re.sub(r'\s*/\s*', '/', artist).strip()
        if slash_compact != artist:
            add_v(slash_compact)

    # 5. Semikolon-Splitting fuer Duette/Kollaborationen:
    # Manche Sender liefern "Artist1; Artist2" als kombinierten Artist-String.
    # MusicBrainz kennt keinen Artist "Artist1; Artist2" – nur die Einzelkuenstler.
    # Varianten: erster Kuenstler allein, zweiter allein, beide mit " & " und " feat. ".
    if ';' in artist:
        parts = [p.strip() for p in artist.split(';') if p.strip()]
        if len(parts) >= 2:
            add_v(parts[0])
            add_v(parts[1])
            add_v(f"{parts[0]} & {parts[1]}")
            add_v(f"{parts[0]} feat. {parts[1]}")

    return variants


# --- Generik-Filter ---

def is_song_pair(pair) -> bool:
    """Prueft ob ein (artist, title)-Tupel beide Felder belegt hat."""
    return bool(pair and pair[0] and pair[1])


def is_generic_metadata_text(text: str, station_name: str = '', extra_keywords=()) -> bool:
    """
    Prueft ob ein Metadaten-Text generisch ist (Sendername oder bekannte Keywords enthalten).
    extra_keywords: sendersspezifische Schluesselbegriffe aus dem Stationsprofil.
    """
    text_l = str(text or '').strip().lower()
    if not text_l:
        return False
    station_l = (station_name or '').strip().lower()
    if station_l and station_l in text_l:
        return True
    return any(token in text_l for token in extra_keywords)


def is_generic_song_pair(pair, station_name: str = '', extra_keywords=()) -> bool:
    """
    Prueft ob ein (artist, title)-Paar generisch ist.
    Verwendet is_generic_metadata_text fuer alle Felder inkl. Kombination.
    """
    if not is_song_pair(pair):
        return False
    a_l = str(pair[0] or '').strip().lower()
    t_l = str(pair[1] or '').strip().lower()
    return (
        is_generic_metadata_text(a_l, station_name, extra_keywords)
        or is_generic_metadata_text(t_l, station_name, extra_keywords)
        or is_generic_metadata_text(f"{a_l} - {t_l}", station_name, extra_keywords)
    )


def has_non_generic_song_pair(pair, station_name: str = '', extra_keywords=()) -> bool:
    """Gibt True zurueck wenn das Paar belegt und nicht generisch ist."""
    return is_song_pair(pair) and not is_generic_song_pair(pair, station_name, extra_keywords)


def filter_non_generic_song_pairs(pairs, station_name: str = '', extra_keywords=()):
    """Filtert eine Liste von (artist, title)-Paaren – nur nicht-generische Paare bleiben."""
    return [p for p in (pairs or []) if has_non_generic_song_pair(p, station_name, extra_keywords)]


def append_non_generic_candidate(candidates: list, source: str, artist, title,
                                 station_name: str = '', extra_keywords=(),
                                 log_fn=None) -> bool:
    """
    Fuegt einen Kandidaten zur Liste hinzu, sofern er nicht generisch ist.
    Gibt True zurueck wenn der Kandidat hinzugefuegt wurde.
    log_fn: optionales callable(msg) fuer Debug-Ausgaben.
    """
    pair = (str(artist or '').strip(), str(title or '').strip())
    if not is_song_pair(pair):
        return False
    if is_generic_song_pair(pair, station_name, extra_keywords):
        if log_fn:
            log_fn(
                f"Kandidat verworfen (generisch): source='{source}', "
                f"pair='{pair[0]} - {pair[1]}'"
            )
        return False
    candidates.append({'source': source, 'artist': pair[0], 'title': pair[1]})
    return True


__all__ = [
    'extract_stream_title',
    'parse_stream_title_simple',
    'parse_stream_title_complex',
    'get_last_separator_variant',
    'clean_title_part',
    'get_artist_variants',
    'is_song_pair',
    'is_generic_metadata_text',
    'is_generic_song_pair',
    'has_non_generic_song_pair',
    'filter_non_generic_song_pairs',
    'append_non_generic_candidate',
]
