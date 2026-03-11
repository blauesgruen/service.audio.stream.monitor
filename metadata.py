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
    von_match = re.match(r'^"(.+?)"\s+von\s+(.+)$', stream_title, re.IGNORECASE)
    if von_match:
        return von_match.group(2).strip(), von_match.group(1).strip(), True, False

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

    return variants


__all__ = [
    'extract_stream_title',
    'parse_stream_title_simple',
    'parse_stream_title_complex',
    'get_last_separator_variant',
    'clean_title_part',
    'get_artist_variants'
]
