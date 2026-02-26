import xbmc
import xbmcaddon
import xbmcgui
import requests
import re
import time
import threading
import json
from difflib import SequenceMatcher

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_VERSION = ADDON.getAddonInfo('version')

# --- Konstanten ---

# API Endpunkte
MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2/recording/"
RADIODE_SEARCH_API_URL = "https://prod.radio-api.net/stations/search"
RADIODE_NOWPLAYING_API_URL = "https://api.radio.de/stations/now-playing"

# Header für API-Anfragen
MUSICBRAINZ_HEADERS = {
    "User-Agent": f"RadioMonitorLight/{ADDON_VERSION} (https://github.com; Kodi addon {ADDON_ID})"
}
DEFAULT_HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
INVALID_METADATA_VALUES = ['Unknown', 'Radio Stream', 'Internet Radio']

def _musicbrainz_escape(s):
    """
    Für MusicBrainz-Query in Anführungszeichen: nur Backslash und Anführungszeichen escapen.
    Apostroph ist in Lucene Phrase-Queries ein normales Zeichen und darf NICHT escaped werden –
    das Escape \' ist in Lucene ungültig und bricht die Query (liefert 0 Treffer).
    Beispiel: Israel "Iz" Kamakawiwo'ole → artistname:"Israel \"Iz\" Kamakawiwo'ole" ✓
    """
    if not s:
        return s
    s = str(s).replace('\\', '\\\\').replace('"', '\\"')
    return s.strip() or " "

def _musicbrainz_extract_artist(rec):
    """Extrahiert den vollständigen Artist-String aus einem MB-Recording-Dict."""
    if "artist-credit" not in rec or not rec["artist-credit"]:
        return ""
    parts = []
    for credit in rec["artist-credit"]:
        if isinstance(credit, dict):
            parts.append(credit.get("name", ""))
            joinphrase = credit.get("joinphrase", "")
            if joinphrase:
                parts.append(joinphrase)
    full = "".join(parts).strip()
    return full or rec["artist-credit"][0].get("name", "")

def _musicbrainz_extract_artist_mbid(rec):
    """Extrahiert die primäre Artist-MBID aus einem MB-Recording-Dict."""
    credits = rec.get("artist-credit", [])
    for credit in credits:
        if isinstance(credit, dict):
            artist = credit.get("artist", {})
            if isinstance(artist, dict):
                mbid = artist.get("id", "")
                if mbid:
                    return mbid
    return ""

def _musicbrainz_artist_variants(artist_part):
    """
    Liefert Artist-Varianten für MB-Fallback innerhalb derselben Query-Logik.
    Reihenfolge: Original zuerst, danach nur zusätzliche Normalisierungen.
    """
    original = (artist_part or "").strip()
    if not original:
        return [("original", original)]

    variants = []
    seen = set()

    def add_variant(label, value):
        candidate = (value or "").strip()
        if not candidate:
            return
        key = candidate.lower()
        if key in seen:
            return
        seen.add(key)
        variants.append((label, candidate))

    def normalize_apostrophes(value):
        return value.replace("’", "'").replace("`", "'")

    def remove_apostrophes(value):
        return value.replace("'", "").replace("’", "")

    def swap_comma_name(value):
        match = re.match(r"^\s*([^,]+),\s*(.+)\s*$", value)
        if not match:
            return value
        last_name = match.group(1).strip()
        first_name = match.group(2).strip()
        return f"{first_name} {last_name}".strip()

    add_variant("original", original)
    # Variante mit 'and' statt '&' (häufige Abweichung bei Kollaborationen)
    if ' & ' in original:
        add_variant("and-for-ampersand", original.replace(' & ', ' and '))

    add_variant("apostrophe-normalized", normalize_apostrophes(original))
    add_variant("apostrophe-removed", remove_apostrophes(normalize_apostrophes(original)))

    comma_swapped = swap_comma_name(original)
    if comma_swapped != original:
        add_variant("comma-swapped", comma_swapped)
        # Auch bei der Komma-Variante '&' ersetzen
        if ' & ' in comma_swapped:
            add_variant("comma-swapped+and-for-ampersand", comma_swapped.replace(' & ', ' and '))
        add_variant("comma-swapped+apostrophe-normalized", normalize_apostrophes(comma_swapped))
        add_variant("comma-swapped+apostrophe-removed", remove_apostrophes(normalize_apostrophes(comma_swapped)))

    # Erster Künstler vor & / feat. / ft. / with
    # Beispiel: "Rihanna & Mikky Ekko" → "Rihanna"
    # MB indexiert Multi-Artist-Songs oft nur unter dem Hauptkünstler.
    first_artist = re.split(r'\s*(?:&|feat\.?|ft\.?|with)\s+', original, maxsplit=1)[0].strip()
    if first_artist != original:
        add_variant("first-artist", first_artist)
        add_variant("first-artist+apostrophe-normalized", normalize_apostrophes(first_artist))

    return variants

def _musicbrainz_query_recording(title_part, artist_part):
    """
    Führt eine MusicBrainz-Recording-Query mit expliziten Feldangaben durch.
    Nutzt recording: und artistname: für präzises Matching (kein Stopword-Problem).

    Prüft bis zu 5 Treffer und wählt den aus, bei dem der MB-Artist am besten
    zu artist_part passt (Score × Ähnlichkeit). Das verhindert Fehlgriffe wenn
    der erste Treffer zwar hohen Score hat aber einen komplett anderen Artist.

    Rückgabe: (score, mb_artist, mb_title, mb_artist_mbid)
    oder (0, '', '', '') bei Fehler/kein Treffer.
    """
    safe_title = _musicbrainz_escape(title_part)
    artist_variants = _musicbrainz_artist_variants(artist_part)
    retries = 2
    for variant_label, variant_artist in artist_variants:
        safe_artist = _musicbrainz_escape(variant_artist)
        params = {
            "query": f'recording:"{safe_title}" AND artistname:"{safe_artist}"',
            "fmt":   "json",
            "limit": 5,
        }
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz Query-Variante: recording='{title_part}', "
            f"artistname='{variant_artist}' ({variant_label})",
            xbmc.LOGDEBUG
        )
        for attempt in range(retries + 1):
            try:
                r = requests.get(MUSICBRAINZ_API_URL, params=params, headers=MUSICBRAINZ_HEADERS, timeout=5)
                data = r.json()
                recordings = data.get("recordings", [])
                if not recordings:
                    xbmc.log(
                        f"[{ADDON_NAME}] MusicBrainz: kein Treffer für Variante '{variant_label}' "
                        f"(recording:'{title_part}' artistname:'{variant_artist}')",
                        xbmc.LOGDEBUG
                    )
                    break

                xbmc.log(
                    f"[{ADDON_NAME}] MusicBrainz: Treffer mit Variante '{variant_label}' "
                    f"(count={len(recordings)})",
                    xbmc.LOGDEBUG
                )

                # Besten Treffer anhand von Score × Artist-Ähnlichkeit wählen
                best_combined = -1
                best_score, best_artist, best_title, best_mbid = 0, '', '', ''

                for rec in recordings:
                    score     = int(rec.get("score", 0))
                    mb_title  = rec.get("title", "")
                    mb_artist = _musicbrainz_extract_artist(rec)
                    mb_mbid   = _musicbrainz_extract_artist_mbid(rec)
                    # Ähnlichkeit gegen den Original-Artistpart für stabile Entscheidung
                    artist_sim = _mb_similarity(mb_artist, artist_part)
                    combined   = score * artist_sim
                    xbmc.log(
                        f"[{ADDON_NAME}] MB Kandidat: Artist='{mb_artist}', Title='{mb_title}', "
                        f"Score={score}, artist_sim={artist_sim:.2f}, combined={combined:.1f}",
                        xbmc.LOGDEBUG
                    )
                    if combined > best_combined:
                        best_combined = combined
                        best_score    = score
                        best_artist   = mb_artist
                        best_title    = mb_title
                        best_mbid     = mb_mbid

                xbmc.log(
                    f"[{ADDON_NAME}] MusicBrainz Best-Match "
                    f"(title='{title_part}', artist='{artist_part}', variante='{variant_label}'): "
                    f"Score={best_score}, MB-Artist='{best_artist}', MB-Title='{best_title}', "
                    f"MBID='{best_mbid}', combined={best_combined:.1f}",
                    xbmc.LOGDEBUG
                )
                return best_score, best_artist, best_title, best_mbid

            except Exception as e:
                xbmc.log(
                    f"[{ADDON_NAME}] MusicBrainz Fehler Variante '{variant_label}' "
                    f"(Versuch {attempt+1}/{retries+1}): {e}",
                    xbmc.LOGDEBUG
                )
                if attempt < retries:
                    time.sleep(2)
                else:
                    break
    # --- Q4: Fuzzy-Fallback ohne Anführungszeichen ---
    # Greift nur wenn alle Phrase-Varianten Score=0 lieferten.
    # Ohne Quotes nutzt MB seinen eigenen Fuzzy-Index, der Compound-Schreibweisen
    # wie 'Every time' vs 'Everytime' automatisch auflöst – sprachunabhängig,
    # ohne Whitelist. Stopword-Problem ist hier akzeptabel da letzter Fallback.
    xbmc.log(
        f"[{ADDON_NAME}] MusicBrainz: alle Phrase-Varianten ohne Treffer – "
        f"versuche Fuzzy-Query ohne Quotes für recording='{title_part}', "
        f"artistname='{artist_part}'",
        xbmc.LOGINFO
    )
    try:
        time.sleep(1)
        params = {
            "query": f'recording:{_musicbrainz_escape(title_part)} AND artistname:{_musicbrainz_escape(artist_part)}',
            "fmt":   "json",
            "limit": 5,
        }
        r = requests.get(MUSICBRAINZ_API_URL, params=params, headers=MUSICBRAINZ_HEADERS, timeout=5)
        data = r.json()
        recordings = data.get("recordings", [])
        if recordings:
            best_combined = -1
            best_score, best_artist, best_title, best_mbid = 0, '', '', ''
            for rec in recordings:
                score      = int(rec.get("score", 0))
                mb_title   = rec.get("title", "")
                mb_artist  = _musicbrainz_extract_artist(rec)
                mb_mbid    = _musicbrainz_extract_artist_mbid(rec)
                artist_sim = _mb_similarity(mb_artist, artist_part)
                combined   = score * artist_sim
                xbmc.log(
                    f"[{ADDON_NAME}] MB Fuzzy-Kandidat: Artist='{mb_artist}', "
                    f"Title='{mb_title}', Score={score}, "
                    f"artist_sim={artist_sim:.2f}, combined={combined:.1f}",
                    xbmc.LOGDEBUG
                )
                if combined > best_combined:
                    best_combined = combined
                    best_score    = score
                    best_artist   = mb_artist
                    best_title    = mb_title
                    best_mbid     = mb_mbid
            xbmc.log(
                f"[{ADDON_NAME}] MB Fuzzy-Query Best-Match: "
                f"Score={best_score}, MB-Artist='{best_artist}', "
                f"MB-Title='{best_title}', MBID='{best_mbid}', "
                f"combined={best_combined:.1f}",
                xbmc.LOGINFO
            )
            # Guard: MB-Titel muss dem gesuchten Titel ähneln.
            # Ohne Quotes können Stopwords ignoriert werden, sodass MB einen
            # komplett anderen Song zurückgibt (z.B. recording:In → beliebiger
            # Song weil 'In' Stopword ist). Titel-Ähnlichkeit < 0.6 → verwerfen.
            FUZZY_TITLE_MIN_SIM = 0.6
            title_sim = _mb_similarity(best_title, title_part)
            if title_sim < FUZZY_TITLE_MIN_SIM:
                xbmc.log(
                    f"[{ADDON_NAME}] MB Fuzzy-Query verworfen: Titel-Ähnlichkeit "
                    f"zu niedrig (sim={title_sim:.2f} < {FUZZY_TITLE_MIN_SIM}) – "
                    f"MB-Title='{best_title}' vs. gesuchter Title='{title_part}'",
                    xbmc.LOGINFO
                )
                return 0, '', '', ''
            return best_score, best_artist, best_title, best_mbid
        xbmc.log(
            f"[{ADDON_NAME}] MB Fuzzy-Query: kein Treffer für "
            f"recording='{title_part}' artistname='{artist_part}'",
            xbmc.LOGDEBUG
        )
    except Exception as e:
        xbmc.log(
            f"[{ADDON_NAME}] MB Fuzzy-Query Fehler: {e}",
            xbmc.LOGDEBUG
        )
    return 0, '', '', ''

def _parse_radiode_api_title(full_title, station_name=None):
    """
    Parst radio.de API Format "ARTIST - TITLE". Gibt (artist, title) zurück;
    ungültige Werte werden zu ''/None. station_name wird als ungültiger Title gefiltert.
    """
    invalid = INVALID_METADATA_VALUES + ['']
    if not full_title or ' - ' not in full_title:
        return None, None
    parts = full_title.split(' - ', 1)
    artist = parts[0].strip()
    title = parts[1].strip()
    if artist in invalid:
        artist = ''
    if title in invalid or (station_name and title == station_name):
        title = ''
    if title and re.match(r'^\d+\s*-\s*\d+$', title):
        return None, None
    return artist or None, title or None

def _mb_similarity(a, b):
    """
    Ähnlichkeit zweier Strings (0.0 - 1.0), case-insensitive.

    Kombiniert drei Methoden und gibt das Maximum zurück, um typische
    ICY-Schreibweisabweichungen robust zu erkennen:
      - raw:        direkter Zeichenvergleich (SequenceMatcher)
      - normalized: Punkte und Sonderzeichen entfernt (R. Kelly → r kelly)
      - token_sort: Wörter sortiert verglichen (Ray jr Parker → Ray Parker Jr.)
    """
    if not a or not b:
        return 0.0

    def normalize(s):
        s = s.lower().replace('.', '')
        s = re.sub(r'[^\w\s]', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    def token_sort(s):
        return ' '.join(sorted(normalize(s).split()))

    a_norm, b_norm = normalize(a), normalize(b)
    return max(
        SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio(),
        SequenceMatcher(None, a_norm, b_norm).ratio(),
        SequenceMatcher(None, token_sort(a), token_sort(b)).ratio(),
    )

def _musicbrainz_query_title_only(title_part, artist_hints=None):
    """
    Sucht in MusicBrainz nur nach dem Titel, ohne artistname-Filter.

    Wird als Fallback genutzt wenn Q1+Q2 beide Score=0 liefern – was passiert wenn
    der ICY-Artistname so abweicht dass MB ihn nicht als Phrase findet
    (z.B. "Chris DeBurgh" statt "Chris de Burgh").

    MB gibt seinen eigenen, korrekten Artistnamen zurück. Dieser wird dann per
    Ähnlichkeitsvergleich gegen beide ICY-Parts geprüft um die Reihenfolge zu bestimmen.

    Rückgabe: (score, mb_artist, mb_title, mb_artist_mbid)
    oder (0, '', '', '') bei Fehler/kein Treffer.
    """
    safe_title = _musicbrainz_escape(title_part)
    params = {
        "query": f'recording:"{safe_title}"',
        "fmt":   "json",
        "limit": 5,
    }
    try:
        r = requests.get(MUSICBRAINZ_API_URL, params=params, headers=MUSICBRAINZ_HEADERS, timeout=5)
        data = r.json()
        recordings = data.get("recordings", [])
        if not recordings:
            xbmc.log(
                f"[{ADDON_NAME}] MB Fallback-Query: kein Treffer für recording:'{title_part}'",
                xbmc.LOGDEBUG
            )
            return 0, '', '', ''

        hints = [h for h in (artist_hints or []) if h]
        best = None
        best_combined = -1.0
        best_hint_sim = 0.0

        for rec in recordings:
            score = int(rec.get("score", 0))
            mb_title = rec.get("title", "")
            mb_artist = _musicbrainz_extract_artist(rec)
            hint_sim = max([_mb_similarity(mb_artist, h) for h in hints], default=0.0)
            combined = score * hint_sim if hints else float(score)
            xbmc.log(
                f"[{ADDON_NAME}] MB Fallback-Kandidat: Artist='{mb_artist}', Title='{mb_title}', "
                f"Score={score}, hint_sim={hint_sim:.2f}, combined={combined:.1f}",
                xbmc.LOGDEBUG
            )
            if combined > best_combined or (combined == best_combined and hint_sim > best_hint_sim):
                best = rec
                best_combined = combined
                best_hint_sim = hint_sim

        score     = int(best.get("score", 0))
        mb_title  = best.get("title", "")
        mb_artist = _musicbrainz_extract_artist(best)
        mb_mbid   = _musicbrainz_extract_artist_mbid(best)
        xbmc.log(
            f"[{ADDON_NAME}] MB Fallback-Query Best-Match: "
            f"Score={score}, Artist='{mb_artist}', Title='{mb_title}', MBID='{mb_mbid}', "
            f"hint_sim={best_hint_sim:.2f}, combined={best_combined:.1f}",
            xbmc.LOGDEBUG
        )
        return score, mb_artist, mb_title, mb_mbid

    except Exception as e:
        xbmc.log(f"[{ADDON_NAME}] MB Fallback-Query Fehler: {e}", xbmc.LOGDEBUG)
        return 0, '', '', ''


def _identify_artist_title_via_musicbrainz(part1, part2):
    """
    Ermittelt welcher der beiden ICY-Parts der Artist ist, via MusicBrainz.

    Strategie:
      Q1: recording:"part1" AND artistname:"part2"  → Normalfall (part1=Title, part2=Artist)
      Q2: recording:"part2" AND artistname:"part1"  → Umgekehrt  (part2=Title, part1=Artist)
      Q3: recording:"part1" (nur Titel, kein Artist-Filter) → Fallback wenn Q1+Q2 Score=0

    Q3 greift wenn der ICY-Artistname so von MB abweicht, dass die Phrase-Query
    keinen Treffer liefert (z.B. "Chris DeBurgh" vs. MB "Chris de Burgh").
    MB gibt dann seinen korrekten Artistnamen zurück; der Ähnlichkeitsvergleich
    gegen beide ICY-Parts bestimmt die Reihenfolge.

    Rückgabe: (artist, title, mbid, uncertain)
      uncertain=True  → kein verlässlicher Treffer, ICY-Standard behalten
      uncertain=False → Reihenfolge sicher bestimmt
    """
    MIN_SCORE = 85
    THRESHOLD = 0.7  # Ähnlichkeitsschwelle MB-Artist ↔ ICY-Part

    if not part1 or not part2:
        return part1, part2, '', True

    # --- Bereinigung der Titel-Parts für die MusicBrainz-Suche ---
    # Entfernt häufige, störende Suffixe in Klammern, die die MB-Suche stören,
    # z.B. "(Radio Edit)", "(feat. ...)", "(The official... song)".
    def clean_title_part(part):
        """
        Bereinigt einen Titel-Part für die MusicBrainz-Suche.

        Schritt 1: Bekannte Klammer-Keywords entfernen (Radio Edit, feat., Remix ...)
        Schritt 2: Alle verbleibenden abschließenden Klammerausdrücke entfernen
                   (z.B. "(Love theme)", "(Remastered 2011)", "(Original Soundtrack)")
                   Nur am Ende – führende Klammern wie "(I've Had) The Time of My Life"
                   bleiben unberührt.
        """
        keywords = [
            'Official', 'Radio', 'Original', 'Live', 'Remix', 'Edit',
            'Version', 'Mix', 'Acoustic', 'feat', 'ft', 'with', 'TM'
        ]
        keyword_pattern = r'\s*[\(\[][^\)\]]*(' + '|'.join(keywords) + r')[^\)\]]*[\)\]]'
        cleaned = re.sub(keyword_pattern, '', part, flags=re.IGNORECASE).strip()
        # Iterativ alle restlichen abschließenden Klammerausdrücke entfernen
        prev = None
        while prev != cleaned:
            prev = cleaned
            cleaned = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', '', cleaned).strip()
        return cleaned

    p1_cleaned = clean_title_part(part1)
    p2_cleaned = clean_title_part(part2)

    xbmc.log(f"[{ADDON_NAME}] MusicBrainz: Suche Recording für '{part1}' / '{part2}'", xbmc.LOGDEBUG)
    if p1_cleaned != part1 or p2_cleaned != part2:
        xbmc.log(f"[{ADDON_NAME}] MusicBrainz: Bereinigte Parts für Titel-Suche: '{p1_cleaned}' / '{p2_cleaned}'", xbmc.LOGDEBUG)

    # --- Q1: part1=Title, part2=Artist ---
    score_1, mb_artist_1, mb_title_1, mbid_1 = _musicbrainz_query_recording(
        title_part=p1_cleaned, artist_part=part2
    )

    # --- Q2: part2=Title, part1=Artist ---
    time.sleep(1)  # MusicBrainz Rate-Limit: ~1 req/s
    score_2, mb_artist_2, mb_title_2, mbid_2 = _musicbrainz_query_recording(
        title_part=p2_cleaned, artist_part=part1
    )

    # --- Entscheidung anhand combined-Score (MB-Score × Artist-Ähnlichkeit) ---
    sim_1_p2 = _mb_similarity(mb_artist_1, part2)  # Q1: MB-Artist sollte part2 ähneln
    sim_2_p1 = _mb_similarity(mb_artist_2, part1)  # Q2: MB-Artist sollte part1 ähneln
    combined_1 = score_1 * sim_1_p2
    combined_2 = score_2 * sim_2_p1

    xbmc.log(
        f"[{ADDON_NAME}] MusicBrainz Entscheidung: "
        f"Q1(score={score_1}, artist_sim={sim_1_p2:.2f}, combined={combined_1:.1f}) | "
        f"Q2(score={score_2}, artist_sim={sim_2_p1:.2f}, combined={combined_2:.1f})",
        xbmc.LOGINFO
    )

    # --- Q3: Fallback wenn beide Scores 0 (Schreibweisabweichung im Artistnamen) ---
    # Beispiel: ICY "Chris DeBurgh" → MB findet "Chris de Burgh" nicht per Phrase-Query.
    # Q3 sucht nur nach dem Titel, MB liefert seinen eigenen Artistnamen zurück,
    # der Ähnlichkeitsvergleich gegen part1/part2 bestimmt dann die Reihenfolge.
    if score_1 == 0 and score_2 == 0:
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz Q1+Q2 ohne Treffer – versuche Fallback-Query "
            f"ohne artistname-Filter für '{part1}'",
            xbmc.LOGINFO
        )
        time.sleep(1)
        score_f, mb_artist_f, mb_title_f, mbid_f = _musicbrainz_query_title_only(
            p1_cleaned, artist_hints=[part1, part2]
        )

        if score_f >= MIN_SCORE:
            sim_f_p1 = _mb_similarity(mb_artist_f, part1)
            sim_f_p2 = _mb_similarity(mb_artist_f, part2)
            xbmc.log(
                f"[{ADDON_NAME}] MB Fallback-Query: Score={score_f}, "
                f"MB-Artist='{mb_artist_f}', "
                f"sim_p1={sim_f_p1:.2f}, sim_p2={sim_f_p2:.2f}",
                xbmc.LOGINFO
            )
            # MB-Artist ähnelt part2 → part2 ist Artist, part1 ist Title
            if sim_f_p2 >= THRESHOLD and sim_f_p2 > sim_f_p1:
                xbmc.log(
                    f"[{ADDON_NAME}] MB Fallback: Artist='{part2}', Title='{part1}' "
                    f"(MB-Artist='{mb_artist_f}', sim_p2={sim_f_p2:.2f})",
                    xbmc.LOGINFO
                )
                return part2, part1, mbid_f, False
            # MB-Artist ähnelt part1 → part1 ist Artist, part2 ist Title (ICY-Standard stimmt)
            if sim_f_p1 >= THRESHOLD and sim_f_p1 > sim_f_p2:
                xbmc.log(
                    f"[{ADDON_NAME}] MB Fallback: Artist='{part1}', Title='{part2}' "
                    f"(MB-Artist='{mb_artist_f}', sim_p1={sim_f_p1:.2f})",
                    xbmc.LOGINFO
                )
                return part1, part2, mbid_f, False
            xbmc.log(
                f"[{ADDON_NAME}] MB Fallback: Artist-Ähnlichkeit zu niedrig "
                f"(sim_p1={sim_f_p1:.2f}, sim_p2={sim_f_p2:.2f}), behalte ICY-Original",
                xbmc.LOGINFO
            )
        else:
            xbmc.log(
                f"[{ADDON_NAME}] MB Fallback-Query Score zu niedrig ({score_f}), behalte ICY-Original",
                xbmc.LOGINFO
            )
        return part1, part2, '', True

    # Beide combined-Scores zu niedrig → uncertain
    if combined_1 < MIN_SCORE * THRESHOLD and combined_2 < MIN_SCORE * THRESHOLD:
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz: beide combined-Scores zu niedrig "
            f"({combined_1:.1f}/{combined_2:.1f}), behalte ICY-Original: "
            f"Artist='{part1}', Title='{part2}'",
            xbmc.LOGINFO
        )
        return part1, part2, '', True

    # Q1 gewinnt → part1=Title, part2=Artist
    if combined_1 >= combined_2:
        if sim_1_p2 >= THRESHOLD:
            xbmc.log(
                f"[{ADDON_NAME}] MusicBrainz Q1 gewinnt: Artist='{part2}', Title='{part1}' "
                f"(MB-Artist='{mb_artist_1}', sim={sim_1_p2:.2f})",
                xbmc.LOGINFO
            )
            return part2, part1, mbid_1, False
        # MB-Artist ähnelt eher part1 (Reihenfolge stimmt schon)
        sim_1_p1 = _mb_similarity(mb_artist_1, part1)
        if sim_1_p1 >= THRESHOLD:
            xbmc.log(
                f"[{ADDON_NAME}] MusicBrainz Q1 gewinnt (Artist=part1): Artist='{part1}', Title='{part2}' "
                f"(MB-Artist='{mb_artist_1}', sim={sim_1_p1:.2f})",
                xbmc.LOGINFO
            )
            return part1, part2, mbid_1, False
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz Q1 gewinnt aber Artist passt nicht gut "
            f"(sim_p1={_mb_similarity(mb_artist_1, part1):.2f}, sim_p2={sim_1_p2:.2f}), "
            f"behalte Original",
            xbmc.LOGINFO
        )
        return part1, part2, '', True

    # Q2 gewinnt → part2=Title, part1=Artist
    if sim_2_p1 >= THRESHOLD:
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz Q2 gewinnt: Artist='{part1}', Title='{part2}' "
            f"(MB-Artist='{mb_artist_2}', sim={sim_2_p1:.2f})",
            xbmc.LOGINFO
        )
        return part1, part2, mbid_2, False
    # MB-Artist ähnelt eher part2
    sim_2_p2 = _mb_similarity(mb_artist_2, part2)
    if sim_2_p2 >= THRESHOLD:
        xbmc.log(
            f"[{ADDON_NAME}] MusicBrainz Q2 gewinnt (Artist=part2): Artist='{part2}', Title='{part1}' "
            f"(MB-Artist='{mb_artist_2}', sim={sim_2_p2:.2f})",
            xbmc.LOGINFO
        )
        return part2, part1, mbid_2, False
    xbmc.log(
        f"[{ADDON_NAME}] MusicBrainz Q2 gewinnt aber Artist passt nicht gut "
        f"(sim_p1={sim_2_p1:.2f}, sim_p2={_mb_similarity(mb_artist_2, part2):.2f}), "
        f"behalte Original",
        xbmc.LOGINFO
    )
    return part1, part2, '', True

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
        WINDOW.clearProperty('RadioMonitor.MBID')
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
                self.player.clearProperty('MBID')
                self.player.clearProperty('StreamTitle')
            except Exception:
                pass
        
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
            self.set_property_safe('RadioMonitor.Logo', self.station_logo)
        else:
            # Kein echtes Logo → Property leer lassen (Kodi nutzt automatisch Fallback)
            WINDOW.clearProperty('RadioMonitor.Logo')
    
    def update_player_metadata(self, artist, title, station, logo=None, mbid=None):
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

                    self.set_property_safe('RadioMonitor.Station', station_name)
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
            
            params = {'query': search_name, 'count': 20}
            response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
            if response.status_code != 200 or not response.content:
                xbmc.log(f"[{ADDON_NAME}] radio.de API: ungültige Antwort (Status {response.status_code})", xbmc.LOGDEBUG)
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
                        self.set_property_safe('RadioMonitor.Logo', station_logo)
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
        station_name = WINDOW.getProperty('RadioMonitor.Station')
        stream_url = self.current_url or ''
        
        try:
            while (
                not self.stop_thread
                and self.is_playing
                and self.use_api_fallback
                and generation == self.metadata_generation
            ):
                # Versuche verschiedene APIs
                if station_name:
                    artist, title = self.get_nowplaying_from_apis(station_name, stream_url)
                    
                    if title and title != last_title:
                        last_title = title
                        
                        # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                        self.set_logo_safe()
                        mbid = ''
                        if artist and title:
                            mb_artist, mb_title, mbid, uncertain = _identify_artist_title_via_musicbrainz(artist, title)
                            if uncertain:
                                mbid = ''
                            elif mb_artist and mb_title and (
                                _mb_similarity(mb_artist, artist) < 0.8 or _mb_similarity(mb_title, title) < 0.8
                            ):
                                # Nur MBID nutzen, wenn MB den API-Titel plausibel bestätigt.
                                mbid = ''
                        
                        if artist:
                            self.set_property_safe('RadioMonitor.Artist', artist)
                            self.set_property_safe('RadioMonitor.Title', title)
                            self.set_property_safe('RadioMonitor.StreamTitle', f"{artist} - {title}")
                            if mbid:
                                self.set_property_safe('RadioMonitor.MBID', mbid)
                            else:
                                WINDOW.clearProperty('RadioMonitor.MBID')
                            xbmc.log(f"[{ADDON_NAME}] API Update: {artist} - {title}", xbmc.LOGINFO)
                             
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty('RadioMonitor.Logo')
                            self.update_player_metadata(artist, title, station_name, logo if logo else None, mbid if mbid else None)
                        else:
                            WINDOW.clearProperty('RadioMonitor.Artist')
                            WINDOW.clearProperty('RadioMonitor.MBID')
                            self.set_property_safe('RadioMonitor.Title', title)
                            self.set_property_safe('RadioMonitor.StreamTitle', title)
                            xbmc.log(f"[{ADDON_NAME}] API Update: {title}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty('RadioMonitor.Logo')
                            self.update_player_metadata(None, title, station_name, logo if logo else None, None)
                
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
        """
        Trennt Artist und Title aus dem ICY-StreamTitle.
        Priorität:
        1. Stationsname → immer aus ICY (wird hier nicht geändert)
        2. 'von'-Format → eindeutig, MusicBrainz zur Bestätigung
        3. Trennzeichen → part1/part2:
           a. API prüfen (nur wenn artist UND title gefüllt)
           b. Kreuz-Validierung API gegen ICY-Parts
           c. MusicBrainz immer zur Bestätigung/Korrektur
        4. Fallback: nur ICY + MusicBrainz
        """
        invalid = INVALID_METADATA_VALUES + ["", station_name]

        # --- StreamTitle Grundvalidierung ---
        if not stream_title or stream_title in INVALID_METADATA_VALUES or re.match(r'^\d+\s*-\s*\d+$', stream_title):
            xbmc.log(f"[{ADDON_NAME}] StreamTitle leer/ungueltig: '{stream_title}'", xbmc.LOGDEBUG)
            # Kein ICY-Titel → nur API als letzte Chance (beide Felder müssen gefüllt sein)
            if station_name and stream_url:
                api_artist, api_title = self.get_nowplaying_from_apis(station_name, stream_url)
                if api_artist and api_title and api_artist not in invalid and api_title not in invalid:
                    xbmc.log(f"[{ADDON_NAME}] API-Fallback (kein ICY): Artist='{api_artist}', Title='{api_title}'", xbmc.LOGINFO)
                    return api_artist, api_title, ''
            return None, None, ''

        # --- 'von'-Format → nur wenn Title in Anführungszeichen (sonst Programm-Ansage) ---
        von_match = re.match(r'^"(.+?)"\s+von\s+(.+)$', stream_title, re.IGNORECASE)
        if von_match:
            title  = von_match.group(1).strip()
            artist = von_match.group(2).strip()
            xbmc.log(f"[{ADDON_NAME}] 'von' Format erkannt: Artist='{artist}', Title='{title}'", xbmc.LOGDEBUG)
            mb_artist, mb_title, mbid, uncertain = _identify_artist_title_via_musicbrainz(artist, title)
            if uncertain:
                xbmc.log(f"[{ADDON_NAME}] MusicBrainz unentschieden, nutze 'von'-Ergebnis: Artist='{artist}', Title='{title}'", xbmc.LOGDEBUG)
                mb_artist, mb_title = artist, title
            if mb_artist in invalid: mb_artist = None
            if mb_title in invalid:  mb_title  = None
            return mb_artist or None, mb_title or None, mbid

        # --- Trennzeichen → part1 / part2 ---
        part1, part2 = None, None
        separators = [' - ', ' – ', ' — ', ' | ', ': ']
        for sep in separators:
            if sep in stream_title:
                parts = stream_title.split(sep, 1)
                if len(parts) == 2:
                    part1 = parts[0].strip()
                    part2 = parts[1].strip()
                    break

        if not part1 or not part2:
            # Kein Trennzeichen → ganzer String ist vermutlich nur Title
            # Stationsname als Titel ausschließen
            clean = stream_title.strip()
            if clean in INVALID_METADATA_VALUES:
                return None, None, ''
            if station_name and _mb_similarity(clean.lower(), station_name.lower()) >= 0.8:
                xbmc.log(f"[{ADDON_NAME}] Kein Trennzeichen, aber String aehnelt Stationsname -> ignoriert: '{clean}'", xbmc.LOGDEBUG)
                return None, None, ''
            return None, clean, ''

        # Stationsname in part1 oder part2 → kein Song sondern Sender-Info
        station_lower = (station_name or '').lower().strip()
        if station_lower and (
            _mb_similarity(part1.lower(), station_lower) >= 0.8 or
            _mb_similarity(part2.lower(), station_lower) >= 0.8
        ):
            xbmc.log(f"[{ADDON_NAME}] Stationsname in ICY-Parts erkannt → kein Song: '{stream_title}'", xbmc.LOGDEBUG)
            return None, None, ''

        # --- API prüfen ---
        api_artist, api_title = None, None
        if station_name and stream_url:
            raw_artist, raw_title = self.get_nowplaying_from_apis(station_name, stream_url)

            # Beide Felder müssen gefüllt sein
            if raw_artist and raw_title and raw_artist not in invalid and raw_title not in invalid:

                # Kreuz-Validierung: API-Parts müssen den ICY-Parts ähneln
                a_matches_p1 = _mb_similarity(raw_artist, part1) >= 0.8
                a_matches_p2 = _mb_similarity(raw_artist, part2) >= 0.8
                t_matches_p1 = _mb_similarity(raw_title,  part1) >= 0.8
                t_matches_p2 = _mb_similarity(raw_title,  part2) >= 0.8

                if (a_matches_p2 and t_matches_p1) or (a_matches_p1 and t_matches_p2):
                    xbmc.log(f"[{ADDON_NAME}] API gegen ICY validiert: Artist='{raw_artist}', Title='{raw_title}'", xbmc.LOGINFO)
                    api_artist, api_title = raw_artist, raw_title
                else:
                    xbmc.log(f"[{ADDON_NAME}] API-Daten passen nicht zu ICY-Parts → ignoriert", xbmc.LOGDEBUG)
            else:
                xbmc.log(f"[{ADDON_NAME}] API: ein oder beide Felder leer → ignoriert", xbmc.LOGDEBUG)

        # --- MusicBrainz zur Bestätigung/Korrektur ---
        # Wenn API validiert: MB bekommt API-Artist/Title zur Bestätigung
        # Wenn keine API:     MB bekommt ICY-Parts zur Ermittlung der Reihenfolge
        if api_artist and api_title:
            mb_artist, mb_title, mbid, uncertain = _identify_artist_title_via_musicbrainz(api_artist, api_title)
            if uncertain:
                xbmc.log(f"[{ADDON_NAME}] MusicBrainz unentschieden, nutze API-Ergebnis: Artist='{api_artist}', Title='{api_title}'", xbmc.LOGDEBUG)
                mb_artist, mb_title = api_artist, api_title
                mbid = ''
        else:
            mb_artist, mb_title, mbid, uncertain = _identify_artist_title_via_musicbrainz(part1, part2)
            if uncertain:
                # ICY-Standard beibehalten: part1=Artist, part2=Title
                # (nicht stream_title als Ganzes – das verliert die Trennung)
                xbmc.log(
                    f"[{ADDON_NAME}] MusicBrainz unentschieden, nutze ICY-Standard: "
                    f"Artist='{part1}', Title='{part2}'",
                    xbmc.LOGDEBUG
                )
                mb_artist, mb_title = part1, part2
                mbid = ''

        if mb_artist in invalid: mb_artist = None
        if mb_title in invalid:  mb_title  = None
        if not mb_artist and not mb_title:
            return None, None, ''
        return mb_artist, mb_title, mbid
        
    def metadata_worker(self, url, generation):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        xbmc.log(f"[{ADDON_NAME}] Metadata Worker gestartet", xbmc.LOGDEBUG)
        
        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            xbmc.log(f"[{ADDON_NAME}] Keine ICY-Metadaten verfuegbar - wechsle zu API-Fallback", xbmc.LOGWARNING)
            # Starte API-Fallback Worker
            if self.use_api_fallback and generation == self.metadata_generation:
                self.api_metadata_worker(generation)
            return
            
        metaint = stream_info['metaint']
        response = stream_info['response']
        last_title = ""
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
                            last_title = stream_title
                            
                            xbmc.log(f"[{ADDON_NAME}] Neuer StreamTitle erkannt: '{stream_title}'", xbmc.LOGDEBUG)
                            
                            # Hole den korrekten Stationsnamen vom MusicPlayer
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
                            
                            xbmc.log(f"[{ADDON_NAME}] ICY-Daten: station='{station_name}', stream_title='{stream_title}'", xbmc.LOGINFO)

                            # Artist und Title trennen – API wird intern in parse_stream_title aufgerufen
                            artist, title, mbid = self.parse_stream_title(stream_title, station_name, url)

                            # Wenn beide None sind (z.B. bei Zahlen-IDs ohne API-Daten), überspringe diesen Titel
                            if artist is None and title is None:
                                xbmc.log(f"[{ADDON_NAME}] Keine verwertbaren Metadaten fuer '{stream_title}' - RadioMonitor Properties bleiben leer", xbmc.LOGDEBUG)
                                # Properties komplett löschen, damit Skin auf MusicPlayer zurückfällt
                                WINDOW.clearProperty('RadioMonitor.Artist')
                                WINDOW.clearProperty('RadioMonitor.Title')
                                WINDOW.clearProperty('RadioMonitor.MBID')
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
                            if mbid:
                                self.set_property_safe('RadioMonitor.MBID', mbid)
                                xbmc.log(f"[{ADDON_NAME}] MBID: {mbid}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty('RadioMonitor.MBID')
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            
                            # DEBUG: Zeige alle gesetzten Properties
                            xbmc.log(f"[{ADDON_NAME}] === PROPERTIES GESETZT ===", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = {WINDOW.getProperty('RadioMonitor.Playing')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty('RadioMonitor.Station')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty('RadioMonitor.Artist')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty('RadioMonitor.Title')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.MBID = {WINDOW.getProperty('RadioMonitor.MBID')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.StreamTitle = {WINDOW.getProperty('RadioMonitor.StreamTitle')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty('RadioMonitor.Genre')}", xbmc.LOGINFO)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty('RadioMonitor.Logo')}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten (für Standard InfoLabels)
                            logo = WINDOW.getProperty('RadioMonitor.Logo')
                            self.update_player_metadata(artist if artist else None, 
                                                        title if title else None, 
                                                        station_name if station_name else None,
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
                                        "sender": "service.monitor.radio_de_light",
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
                            
                            xbmc.log(f"[{ADDON_NAME}] Neuer Titel: {stream_title} (Artist: {artist if artist else 'N/A'}, Title: {title if title else 'N/A'})", xbmc.LOGINFO)

                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata-Loop (Thread läuft weiter): {str(e)}", xbmc.LOGERROR)
                    time.sleep(1)
                    continue

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
                        WINDOW.clearProperty('RadioMonitor.MBID')
                        
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
                                
                                params = {'query': search_name, 'count': 5}
                                response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
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
