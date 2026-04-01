"""
MusicBrainz API - vollständige Implementierung.

Enthält alle MB-Abfragefunktionen, Hilfsfunktionen und öffentliche API.
"""
import re
import time
from difflib import SequenceMatcher

from constants import (
    MUSICBRAINZ_API_URL, MUSICBRAINZ_ARTIST_URL, MUSICBRAINZ_HEADERS,
    MB_SONG_CACHE_TTL, INVALID_METADATA_VALUES,
    MB_WORK_CONTEXT_ENABLED, MB_WORK_CONTEXT_MAX_SECONDS,
    MB_WORK_CONTEXT_MAX_PAGES, MB_WORK_CONTEXT_MAX_DETAIL_LOOKUPS,
    MB_WORK_CONTEXT_RATE_LIMIT_S,
    NUMERIC_ID_PATTERN as _NUMERIC_ID_RE,
)
from api_client import APIClient
from cache import MusicBrainzCache
from logger import log_debug, log_info, log_warning
from metadata import clean_title_part as _clean_title_part, get_artist_variants as _get_artist_variants


# --- Module-Level Singletons ---

_mb_client = APIClient(headers=MUSICBRAINZ_HEADERS, retry_count=3)
_mb_cache = MusicBrainzCache(ttl=MB_SONG_CACHE_TTL)
_artist_info_cache = {}       # Artist-Info Cache (in-memory, kein TTL)
_ARTIST_INFO_CACHE_MAX = 200  # maximale Eintraege; aelteste werden bei Ueberschreitung entfernt


def _artist_cache_set(mbid, value):
    """Schreibt in den Artist-Info-Cache und entfernt aelteste Eintraege wenn Limit erreicht."""
    if len(_artist_info_cache) >= _ARTIST_INFO_CACHE_MAX:
        # Ersten (aeltesten) Eintrag entfernen
        oldest = next(iter(_artist_info_cache))
        del _artist_info_cache[oldest]
    _artist_info_cache[mbid] = value


# ── Private Hilfsfunktionen ──────────────────────────────────────────────────

def _frd_year(frd):
    """Extrahiert eine Jahreszahl aus einem FirstReleaseDate-String. 9999 wenn unbekannt."""
    year = (frd or "")[:4]
    return int(year) if year.isdigit() else 9999


def _mb_year(value):
    """Gibt das 4-stellige Jahr aus einem MB-Datum zurück oder ''."""
    year = (value or "")[:4]
    return year if year.isdigit() else ""


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


def _musicbrainz_extract_album(releases_or_rec, first_release_date=""):
    """Ermittelt das erste passende Album für einen Song anhand von FirstRelease.

    Strategie:
      Aus allen Releases werden zunächst nur echte Alben gezogen
      (primary-type == "Album", kein Live, kein Karaoke, kein VA, keine Compilation).
      Mit first_release_date wird das früheste Release ab diesem Jahr gewählt
      (erstes erschienenes Album mit dem Song).

      Es werden ausschließlich echte Alben ohne Compilation verwendet.
      Wenn kein solches Album-Release gefunden wird, bleibt Album leer.

      Ohne first_release_date wird das älteste passende Release genommen.

    Bekannte MB-Platzhalter ("Unclustered Files", etc.) werden immer ausgeschlossen.
    Rückgabe: (album_title, album_year) – beide können leer sein, Jahr immer 4-stellig.
    """
    MB_PLACEHOLDER_TITLES = {"unclustered files", "[standalone recordings]"}

    if isinstance(releases_or_rec, dict):
        releases = releases_or_rec.get("releases", [])
    else:
        releases = releases_or_rec
    if not releases or not isinstance(releases, list):
        return "", ""

    candidates = [
        r for r in releases
        if isinstance(r, dict)
        and r.get("title")
        and (r.get("title") or "").lower() not in MB_PLACEHOLDER_TITLES
    ]
    if not candidates:
        return "", ""

    # --- Hilfsfunktionen ---

    def release_year(r):
        return (r.get("date") or "")[:4]

    def primary_type(r):
        return (r.get("release-group", {}).get("primary-type") or "").lower()

    def secondary_types(r):
        return [s.lower() for s in r.get("release-group", {}).get("secondary-types", [])]

    def is_various_artists(r):
        for credit in r.get("artist-credit", []):
            if isinstance(credit, dict):
                name        = (credit.get("name") or "").lower()
                artist_name = (credit.get("artist", {}).get("name") or "").lower()
                if "various" in name or "various" in artist_name:
                    return True
        return False

    def is_live(r):
        if "live" in secondary_types(r):
            return True
        # Heuristik: Titel beginnt mit ISO-Datum → Konzert-Bootleg
        return bool(re.match(
            r'^\d{4}[-\u2010\u2011\u2012\u2013/]\d{2}[-\u2010\u2011\u2012\u2013/]\d{2}\b',
            r.get("title", "")
        ))

    def is_karaoke(r):
        return "karaoke" in secondary_types(r) or "karaoke" in r.get("title", "").lower()

    def is_compilation(r):
        return "compilation" in secondary_types(r)

    def is_album(r):
        return primary_type(r) == "album"

    # --- Basis-Ausschlüsse gelten immer ---
    clean = [
        r for r in candidates
        if not is_live(r) and not is_karaoke(r) and not is_various_artists(r)
    ]

    # --- Qualitätsstufen ---
    # Nur echte Alben ohne Compilation.
    # Fehlt ein solcher Album-Typ, wird bewusst kein Album gesetzt.
    quality_pools = [
        ("album", [r for r in clean if is_album(r) and not is_compilation(r)]),
    ]

    # --- Anker-Jahr ---
    anchor_year = (first_release_date or "")[:4]

    def first_release_album(pool):
        """
        Wählt das früheste Release ab anchor_year.
        Wenn kein Release >= anchor_year vorhanden ist, fällt es auf das älteste
        datierte Release zurück.
        Ohne Anker: ältestes datiertes Release.
        Undatierte Releases nur wenn kein datiertes vorhanden.
        """
        if not pool:
            return None

        dated   = [r for r in pool if release_year(r)]
        undated = [r for r in pool if not release_year(r)]

        def title_quality_key(r):
            """
            Bevorzugt innerhalb desselben Jahres das "Basisalbum" statt Sondereditionen.
            Niedriger ist besser.
            """
            title = (r.get("title") or "").lower()
            edition_tokens = [
                "deluxe", "exclusive", "special edition", "edition", "bonus",
                "expanded", "remaster", "remastered", "anniversary", "reissue",
                "walmart", "target", "japan", "tour", "collector", "collectors",
                "hits", "best of", "greatest", "formel"
            ]
            penalty = sum(1 for token in edition_tokens if token in title)
            # Kürzere, weniger "dekorierte" Titel als Tie-Breaker bevorzugen.
            normalized = re.sub(r'\([^)]*\)', '', title).strip()
            return (penalty, len(normalized), normalized)

        def pick_from_year_bucket(items):
            if not items:
                return None
            return min(items, key=title_quality_key)

        if not dated:
            return undated[0] if undated else None

        if not anchor_year:
            oldest_year = min(int(release_year(r)) for r in dated)
            bucket = [r for r in dated if int(release_year(r)) == oldest_year]
            return pick_from_year_bucket(bucket)

        try:
            anchor = int(anchor_year)
        except ValueError:
            oldest_year = min(int(release_year(r)) for r in dated)
            bucket = [r for r in dated if int(release_year(r)) == oldest_year]
            return pick_from_year_bucket(bucket)

        same_or_later = [r for r in dated if int(release_year(r)) >= anchor]
        if same_or_later:
            first_year = min(int(release_year(r)) for r in same_or_later)
            bucket = [r for r in same_or_later if int(release_year(r)) == first_year]
            return pick_from_year_bucket(bucket)

        # Kein Album ab anchor_year – besser leer als falsch datiertes Album
        return None

    # --- Erste Stufe die ein Ergebnis liefert gewinnt ---
    for pool_label, pool in quality_pools:
        result = first_release_album(pool)
        if result:
            album_title = result.get("title", "")
            album_year  = release_year(result)
            log_info(
                f"Album-Auswahl: '{album_title}' ({album_year}) "
                f"[Anker={anchor_year or '–'}, Pool={pool_label}, "
                f"Kandidaten={len(candidates)}]"
            )
            return album_title, album_year

    return "", ""


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

    add_variant("original", original)
    
    # Nutze zentrale Normalisierungen aus metadata.py
    for variant in _get_artist_variants(original):
        if variant == original: continue
        add_variant("normalized-variant", variant)

    if ' & ' in original:
        add_variant("and-for-ampersand", original.replace(' & ', ' and '))

    # Erster Künstler vor & / feat. / ft. / with
    first_artist = re.split(r'\s*(?:&|feat\.?|ft\.?|with)\s+', original, maxsplit=1)[0].strip()
    if first_artist != original:
        add_variant("first-artist", first_artist)

    # Letzter Fallback: Token-Query ohne Anführungszeichen.
    add_variant("no-quotes", original)

    return variants


def mb_similarity(a, b):
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


def _musicbrainz_resolve_song_context(recording_mbid, expected_artist, fallback_first_release, fallback_releases):
    """
    Ermittelt song-weites FirstRelease und Album über Work-Recordings.

    Hintergrund:
      recording.first-release-date gilt nur für ein konkretes Recording.
      Für das tatsächliche Song-FirstRelease werden daher alle Recordings
      derselben Work betrachtet.

    Rückgabe: (first_release_year, album_title, album_year)
    """
    fallback_year = _mb_year(fallback_first_release)
    fallback_album, fallback_album_date = _musicbrainz_extract_album(fallback_releases, fallback_year)

    if not recording_mbid:
        return fallback_year, fallback_album, fallback_album_date
    if not MB_WORK_CONTEXT_ENABLED:
        return fallback_year, fallback_album, fallback_album_date

    try:
        started_at = time.time()

        def budget_exhausted():
            return (time.time() - started_at) >= MB_WORK_CONTEXT_MAX_SECONDS
        # 1) Work-IDs des gewählten Recordings laden
        rec_lookup = _mb_client.get(
            f"{MUSICBRAINZ_API_URL}{recording_mbid}",
            params={"fmt": "json", "inc": "work-rels"},
        ).json()
        work_ids = []
        for rel in rec_lookup.get("relations", []):
            if not isinstance(rel, dict):
                continue
            work = rel.get("work", {})
            work_id = work.get("id") if isinstance(work, dict) else ""
            if work_id and work_id not in work_ids:
                work_ids.append(work_id)

        if not work_ids:
            log_debug(f"MB Work-Kontext: keine Work-ID für Recording {recording_mbid}")
            return fallback_year, fallback_album, fallback_album_date

        # 2) Alle Work-Recordings holen (Browse + Paging).
        recording_by_id = {}
        for work_id in work_ids:
            offset = 0
            page_size = 100
            max_pages = max(1, int(MB_WORK_CONTEXT_MAX_PAGES))
            for _ in range(max_pages):
                if budget_exhausted():
                    break
                response = _mb_client.get(
                    MUSICBRAINZ_API_URL,
                    params={
                        "work": work_id,
                        "fmt": "json",
                        "limit": page_size,
                        "offset": offset,
                        "inc": "artist-credits",
                    },
                )
                data = response.json()
                recordings = data.get("recordings", [])
                if not recordings:
                    break
                for rec in recordings:
                    rec_id = rec.get("id", "")
                    if rec_id and rec_id not in recording_by_id:
                        recording_by_id[rec_id] = rec
                if len(recordings) < page_size:
                    break
                offset += page_size
                if MB_WORK_CONTEXT_RATE_LIMIT_S > 0:
                    time.sleep(MB_WORK_CONTEXT_RATE_LIMIT_S)
            if budget_exhausted():
                break

        all_recordings = list(recording_by_id.values())
        if not all_recordings:
            return fallback_year, fallback_album, fallback_album_date

        # 3) Auf erwarteten Artist filtern (sonst Werk-Cover-Versionen)
        artist_filtered = []
        for rec in all_recordings:
            rec_artist = _musicbrainz_extract_artist(rec)
            if expected_artist and mb_similarity(rec_artist, expected_artist) < 0.70:
                continue
            artist_filtered.append(rec)
        if not artist_filtered:
            artist_filtered = all_recordings

        # 4) Echte Song-Erstveröffentlichung über alle passenden Recordings
        first_release_candidates = [
            rec.get("first-release-date")
            for rec in artist_filtered
            if rec.get("first-release-date")
        ]
        song_first_release = min(first_release_candidates)[:4] if first_release_candidates else fallback_year

        # 5) Detail-Lookups: Releases für die frühesten passenden Work-Recordings laden
        def sort_key(rec):
            year = _mb_year(rec.get("first-release-date"))
            return (int(year) if year else 9999, rec.get("id", ""))

        detail_candidates = sorted(artist_filtered, key=sort_key)
        max_detail_lookups = max(0, int(MB_WORK_CONTEXT_MAX_DETAIL_LOOKUPS))
        detail_candidates = detail_candidates[:max_detail_lookups]

        # Fallback-Releases vorseeden: Das Recording aus dem initialen Query hat
        # typischerweise viele Releases (inkl. erstem Album).
        all_releases = list(fallback_releases or [])
        album, album_date = _musicbrainz_extract_album(all_releases, song_first_release)
        actual_lookups = 0
        if not album:
            for rec_stub in detail_candidates:
                if budget_exhausted():
                    break
                rec_id = rec_stub.get("id", "")
                if not rec_id:
                    continue
                rec_detail = _mb_client.get(
                    f"{MUSICBRAINZ_API_URL}{rec_id}",
                    params={"fmt": "json", "inc": "releases+release-groups"},
                ).json()
                actual_lookups += 1
                all_releases.extend(rec_detail.get("releases", []) or [])
                album, album_date = _musicbrainz_extract_album(all_releases, song_first_release)
                if album:
                    break
                if MB_WORK_CONTEXT_RATE_LIMIT_S > 0:
                    time.sleep(MB_WORK_CONTEXT_RATE_LIMIT_S)

        log_debug(
            f"MB Work-Kontext: works={len(work_ids)}, "
            f"recordings={len(all_recordings)}, artist_filtered={len(artist_filtered)}, "
            f"detail_lookups={actual_lookups}/{len(detail_candidates)}, FirstRelease='{song_first_release or '-'}', "
            f"Releases={len(all_releases)}, "
            f"Album='{album or ''}', "
            f"elapsed={time.time() - started_at:.2f}s"
        )
        return song_first_release, album, album_date

    except Exception as e:
        log_warning(f"MB Work-Kontext Fehler: {e}")
        return fallback_year, fallback_album, fallback_album_date


def _musicbrainz_query_recording(title_part, artist_part):
    """
    Führt eine MusicBrainz-Recording-Query mit expliziten Feldangaben durch.
    Nutzt recording: und artistname: für präzises Matching (kein Stopword-Problem).

    Prüft bis zu 100 Treffer und wählt den aus, bei dem der MB-Artist am besten
    zu artist_part passt (Score × Ähnlichkeit).

    Rückgabe: (score, mb_artist, mb_title, mb_mbid, mb_album, mb_album_date, first_release, duration_ms)
    oder (0, '', '', '', '', '', '', 0) bei Fehler/kein Treffer.
    """
    cached = _mb_cache.get(title_part, artist_part)
    if cached:
        log_debug(f"MB Song-Cache Treffer: '{title_part}' / '{artist_part}'")
        return cached

    safe_title = _musicbrainz_escape(title_part)
    artist_variants = _musicbrainz_artist_variants(artist_part)
    for variant_label, variant_artist in artist_variants:
        safe_artist = _musicbrainz_escape(variant_artist)
        # Token-Fallback: ohne Anführungszeichen → tokenbasierte Suche statt Phrase
        if variant_label == "no-quotes":
            query_str = f'recording:"{safe_title}" AND artistname:{safe_artist}'
        else:
            query_str = f'recording:"{safe_title}" AND artistname:"{safe_artist}"'
        params = {
            "query": query_str,
            "fmt":   "json",
            "limit": 100,
            "inc":   "releases+release-groups",
        }
        log_debug(
            f"MusicBrainz Query-Variante: recording='{title_part}', "
            f"artistname='{variant_artist}' ({variant_label})"
        )
        try:
            data = _mb_client.get(MUSICBRAINZ_API_URL, params=params).json()
            recordings = data.get("recordings", [])
            if not recordings:
                log_debug(
                    f"MusicBrainz: kein Treffer für Variante '{variant_label}' "
                    f"(recording:'{title_part}' artistname:'{variant_artist}')"
                )
                continue

            log_debug(
                f"MusicBrainz: Treffer mit Variante '{variant_label}' "
                f"(count={len(recordings)})"
            )

            best_combined = -1
            best_score, best_artist, best_title, best_mbid = 0, '', '', ''
            best_recording_mbid = ''
            best_releases = []
            best_first_release = ''
            best_first_release_year = 9999
            best_duration_ms = 0
            _log_count = 0

            for rec in recordings:
                score     = int(rec.get("score", 0))
                mb_title  = rec.get("title", "")
                mb_artist = _musicbrainz_extract_artist(rec)
                mb_mbid   = _musicbrainz_extract_artist_mbid(rec)
                rec_mbid  = rec.get("id", "")
                releases  = rec.get("releases", [])
                frd       = rec.get("first-release-date") or ""
                artist_sim = mb_similarity(mb_artist, artist_part)
                combined   = score * artist_sim
                if score >= 90 and _log_count < 5:
                    log_debug(
                        f"MB Kandidat: Artist='{mb_artist}', Title='{mb_title}', "
                        f"Score={score}, artist_sim={artist_sim:.2f}, combined={combined:.1f}"
                    )
                    _log_count += 1
                candidate_year = _frd_year(frd)
                is_better = (
                    combined > best_combined
                    or (
                        combined == best_combined and (
                            score > best_score
                            or (score == best_score and candidate_year < best_first_release_year)
                        )
                    )
                )
                if is_better:
                    best_combined = combined
                    best_score    = score
                    best_artist   = mb_artist
                    best_title    = mb_title
                    best_mbid     = mb_mbid
                    best_recording_mbid = rec_mbid
                    best_releases = releases
                    best_first_release = frd[:4] if frd else ''
                    best_first_release_year = candidate_year
                    best_duration_ms = int(rec.get("length") or 0)

            log_debug(
                f"MB Best-Recording für Album-Auswahl: "
                f"{len(best_releases)} Releases, FirstRelease='{best_first_release or '-'}'"
            )
            best_first_release, best_album, best_album_date = _musicbrainz_resolve_song_context(
                best_recording_mbid, best_artist, best_first_release, best_releases
            )

            log_debug(
                f"MusicBrainz Best-Match "
                f"(title='{title_part}', artist='{artist_part}', variante='{variant_label}'): "
                f"Score={best_score}, MB-Artist='{best_artist}', MB-Title='{best_title}', "
                f"MBID='{best_mbid}', Album='{best_album}', AlbumDate='{best_album_date}', "
                f"FirstRelease='{best_first_release}', Duration={best_duration_ms}ms, "
                f"combined={best_combined:.1f}"
            )
            result = (best_score, best_artist, best_title, best_mbid, best_album, best_album_date, best_first_release, best_duration_ms)
            _mb_cache.set(title_part, artist_part, result)
            return result

        except Exception as e:
            log_warning(f"MusicBrainz Fehler Variante '{variant_label}': {e}")

    log_debug(
        f"MusicBrainz: keine Variante lieferte Treffer "
        f"für recording:'{title_part}' artist:'{artist_part}'"
    )
    return 0, '', '', '', '', '', '', 0


def _musicbrainz_query_title_only(title_part, artist_hints=None):
    """
    Sucht in MusicBrainz nur nach dem Titel, ohne artistname-Filter.

    Wird als Fallback genutzt wenn Q1+Q2 beide Score=0 liefern.

    Rückgabe: (score, mb_artist, mb_title, mb_mbid, mb_album, mb_album_date, first_release, duration_ms)
    oder (0, '', '', '', '', '', '', 0) bei Fehler/kein Treffer.
    """
    safe_title = _musicbrainz_escape(title_part)
    params = {
        "query": f'recording:"{safe_title}"',
        "fmt":   "json",
        "limit": 100,
        "inc":   "releases+release-groups",
    }
    try:
        data = _mb_client.get(MUSICBRAINZ_API_URL, params=params).json()
        recordings = data.get("recordings", [])
        if not recordings:
            log_debug(f"MB Fallback-Query: kein Treffer für recording:'{title_part}'")
            return 0, '', '', '', '', '', '', 0

        hints = [h for h in (artist_hints or []) if h]
        best_combined = -1.0
        best_hint_sim = 0.0
        best_score, best_artist, best_title, best_mbid = 0, '', '', ''
        best_recording_mbid = ''
        best_releases = []
        best_first_release = ''
        best_first_release_year = 9999
        best_duration_ms = 0

        for rec in recordings:
            score = int(rec.get("score", 0))
            mb_title = rec.get("title", "")
            mb_artist = _musicbrainz_extract_artist(rec)
            mb_mbid   = _musicbrainz_extract_artist_mbid(rec)
            rec_mbid  = rec.get("id", "")
            releases  = rec.get("releases", [])
            frd       = rec.get("first-release-date") or ""
            hint_sim = max([mb_similarity(mb_artist, h) for h in hints], default=0.0)
            combined = score * hint_sim if hints else float(score)
            log_debug(
                f"MB Fallback-Kandidat: Artist='{mb_artist}', Title='{mb_title}', "
                f"Score={score}, hint_sim={hint_sim:.2f}, combined={combined:.1f}"
            )
            candidate_year = _frd_year(frd)
            is_better = (
                combined > best_combined
                or (
                    combined == best_combined and (
                        hint_sim > best_hint_sim
                        or (
                            hint_sim == best_hint_sim and (
                                score > best_score
                                or (score == best_score and candidate_year < best_first_release_year)
                            )
                        )
                    )
                )
            )
            if is_better:
                best_combined = combined
                best_hint_sim = hint_sim
                best_score    = score
                best_artist   = mb_artist
                best_title    = mb_title
                best_mbid     = mb_mbid
                best_recording_mbid = rec_mbid
                best_releases = releases
                best_first_release = frd[:4] if frd else ''
                best_first_release_year = candidate_year
                best_duration_ms = int(rec.get("length") or 0)

        log_debug(
            f"MB Fallback Best-Recording für Album-Auswahl: "
            f"{len(best_releases)} Releases, FirstRelease='{best_first_release or '-'}'"
        )
        best_first_release, mb_album, mb_album_date = _musicbrainz_resolve_song_context(
            best_recording_mbid, best_artist, best_first_release, best_releases
        )
        log_debug(
            f"MB Fallback-Query Best-Match: "
            f"Score={best_score}, Artist='{best_artist}', Title='{best_title}', "
            f"Album='{mb_album}', AlbumDate='{mb_album_date}', MBID='{best_mbid}', "
            f"FirstRelease='{best_first_release}', "
            f"hint_sim={best_hint_sim:.2f}, combined={best_combined:.1f}"
        )
        return best_score, best_artist, best_title, best_mbid, mb_album, mb_album_date, best_first_release, best_duration_ms

    except Exception as e:
        log_warning(f"MB Fallback-Query Fehler: {e}")
        return 0, '', '', '', '', '', '', 0


def _identify_artist_title_via_musicbrainz(part1, part2):
    """
    Ermittelt welcher der beiden ICY-Parts der Artist ist, via MusicBrainz.

    Strategie:
      Q1: recording:"part1" AND artistname:"part2"  → Normalfall (part1=Title, part2=Artist)
      Q2: recording:"part2" AND artistname:"part1"  → Umgekehrt  (part2=Title, part1=Artist)
      Q3: recording:"part1" (nur Titel, kein Artist-Filter) → Fallback wenn Q1+Q2 Score=0

    Rückgabe: (artist, title, album, album_date, mbid, first_release, uncertain, duration_ms)
      uncertain=True  → kein verlässlicher Treffer, ICY-Standard behalten
      uncertain=False → Reihenfolge sicher bestimmt
    """
    MIN_SCORE = 85
    THRESHOLD = 0.7  # Ähnlichkeitsschwelle MB-Artist ↔ ICY-Part

    if not part1 or not part2:
        return part1, part2, '', '', '', '', True, 0

    # --- Bereinigung der Titel-Parts für die MusicBrainz-Suche ---
    p1_cleaned = _clean_title_part(part1)
    p2_cleaned = _clean_title_part(part2)

    log_debug(f"MusicBrainz: Suche Recording für '{part1}' / '{part2}'")
    if p1_cleaned != part1 or p2_cleaned != part2:
        log_debug(f"MusicBrainz: Bereinigte Parts für Titel-Suche: '{p1_cleaned}' / '{p2_cleaned}'")

    # --- Q1: part1=Title, part2=Artist ---
    score_1, mb_artist_1, mb_title_1, mbid_1, album_1, album_date_1, first_release_1, duration_ms_1 = _musicbrainz_query_recording(
        title_part=p1_cleaned, artist_part=part2
    )

    # --- Q2: part2=Title, part1=Artist ---
    time.sleep(1)  # MusicBrainz Rate-Limit: ~1 req/s
    score_2, mb_artist_2, mb_title_2, mbid_2, album_2, album_date_2, first_release_2, duration_ms_2 = _musicbrainz_query_recording(
        title_part=p2_cleaned, artist_part=part1
    )

    # --- Entscheidung anhand combined-Score (MB-Score × Artist-Ähnlichkeit) ---
    sim_1_p2 = mb_similarity(mb_artist_1, part2)  # Q1: MB-Artist sollte part2 ähneln
    sim_2_p1 = mb_similarity(mb_artist_2, part1)  # Q2: MB-Artist sollte part1 ähneln
    combined_1 = score_1 * sim_1_p2
    combined_2 = score_2 * sim_2_p1

    log_info(
        f"MusicBrainz Entscheidung: "
        f"Q1(score={score_1}, artist_sim={sim_1_p2:.2f}, combined={combined_1:.1f}) | "
        f"Q2(score={score_2}, artist_sim={sim_2_p1:.2f}, combined={combined_2:.1f})"
    )

    # --- Q3: Fallback wenn beide Scores 0 ---
    if score_1 == 0 and score_2 == 0:
        log_info(
            f"MusicBrainz Q1+Q2 ohne Treffer – versuche Fallback-Query "
            f"ohne artistname-Filter für '{part1}'"
        )
        time.sleep(1)
        score_f, mb_artist_f, mb_title_f, mbid_f, album_f, album_date_f, first_release_f, duration_ms_f = _musicbrainz_query_title_only(
            p1_cleaned, artist_hints=[part1, part2]
        )

        if score_f >= MIN_SCORE:
            sim_f_p1 = mb_similarity(mb_artist_f, part1)
            sim_f_p2 = mb_similarity(mb_artist_f, part2)
            log_info(
                f"MB Fallback-Query: Score={score_f}, "
                f"MB-Artist='{mb_artist_f}', "
                f"sim_p1={sim_f_p1:.2f}, sim_p2={sim_f_p2:.2f}"
            )
            # MB-Artist ähnelt part2 → part2 ist Artist, part1 ist Title
            if sim_f_p2 >= THRESHOLD and sim_f_p2 > sim_f_p1:
                log_info(
                    f"MB Fallback: Artist='{part2}', Title='{part1}' "
                    f"(MB-Artist='{mb_artist_f}', sim_p2={sim_f_p2:.2f})"
                )
                return part2, part1, album_f, album_date_f, mbid_f, first_release_f, False, duration_ms_f
            # MB-Artist ähnelt part1 → part1 ist Artist, part2 ist Title
            if sim_f_p1 >= THRESHOLD and sim_f_p1 > sim_f_p2:
                log_info(
                    f"MB Fallback: Artist='{part1}', Title='{part2}' "
                    f"(MB-Artist='{mb_artist_f}', sim_p1={sim_f_p1:.2f})"
                )
                return part1, part2, album_f, album_date_f, mbid_f, first_release_f, False, duration_ms_f
            log_info(
                f"MB Fallback: Artist-Ähnlichkeit zu niedrig "
                f"(sim_p1={sim_f_p1:.2f}, sim_p2={sim_f_p2:.2f}), behalte ICY-Original"
            )
        else:
            log_info(f"MB Fallback-Query Score zu niedrig ({score_f}), behalte ICY-Original")
        return part1, part2, '', '', '', '', True, 0

    # Beide combined-Scores zu niedrig → uncertain
    if combined_1 < MIN_SCORE * THRESHOLD and combined_2 < MIN_SCORE * THRESHOLD:
        log_info(
            f"MusicBrainz: beide combined-Scores zu niedrig "
            f"({combined_1:.1f}/{combined_2:.1f}), behalte ICY-Original: "
            f"Artist='{part1}', Title='{part2}'"
        )
        return part1, part2, '', '', '', '', True, 0

    # Q1 gewinnt → part1=Title, part2=Artist
    if combined_1 >= combined_2:
        if sim_1_p2 >= THRESHOLD:
            log_info(
                f"MusicBrainz Q1 gewinnt: Artist='{part2}', Title='{part1}' "
                f"(MB-Artist='{mb_artist_1}', sim={sim_1_p2:.2f})"
            )
            return part2, part1, album_1, album_date_1, mbid_1, first_release_1, False, duration_ms_1
        # MB-Artist ähnelt eher part1 (Reihenfolge stimmt schon)
        sim_1_p1 = mb_similarity(mb_artist_1, part1)
        if sim_1_p1 >= THRESHOLD:
            log_info(
                f"MusicBrainz Q1 gewinnt (Artist=part1): Artist='{part1}', Title='{part2}' "
                f"(MB-Artist='{mb_artist_1}', sim={sim_1_p1:.2f})"
            )
            return part1, part2, album_1, album_date_1, mbid_1, first_release_1, False, duration_ms_1
        log_info(
            f"MusicBrainz Q1 gewinnt aber Artist passt nicht gut "
            f"(sim_p1={mb_similarity(mb_artist_1, part1):.2f}, sim_p2={sim_1_p2:.2f}), "
            f"behalte Original"
        )
        return part1, part2, '', '', '', '', True, 0

    # Q2 gewinnt → part2=Title, part1=Artist
    if sim_2_p1 >= THRESHOLD:
        log_info(
            f"MusicBrainz Q2 gewinnt: Artist='{part1}', Title='{part2}' "
            f"(MB-Artist='{mb_artist_2}', sim={sim_2_p1:.2f})"
        )
        return part1, part2, album_2, album_date_2, mbid_2, first_release_2, False, duration_ms_2
    # MB-Artist ähnelt eher part2
    sim_2_p2 = mb_similarity(mb_artist_2, part2)
    if sim_2_p2 >= THRESHOLD:
        log_info(
            f"MusicBrainz Q2 gewinnt (Artist=part2): Artist='{part2}', Title='{part1}' "
            f"(MB-Artist='{mb_artist_2}', sim={sim_2_p2:.2f})"
        )
        return part2, part1, album_2, album_date_2, mbid_2, first_release_2, False, duration_ms_2
    log_info(
        f"MusicBrainz Q2 gewinnt aber Artist passt nicht gut "
        f"(sim_p1={sim_2_p1:.2f}, sim_p2={mb_similarity(mb_artist_2, part2):.2f}), "
        f"behalte Original"
    )
    return part1, part2, '', '', '', '', True, 0


# ── Public API ───────────────────────────────────────────────────────────────

def identify_artist_title_via_musicbrainz(part1, part2):
    """
    PUBLIC API: Ermittelt welcher der beiden ICY-Parts der Artist ist, via MusicBrainz.

    Rückgabe: (artist, title, album, album_date, mbid, first_release, uncertain, duration_ms)
      uncertain=True  → kein verlässlicher Treffer, ICY-Standard behalten
      uncertain=False → Reihenfolge sicher bestimmt
    """
    return _identify_artist_title_via_musicbrainz(part1, part2)


def musicbrainz_query_recording(title_part, artist_part):
    """
    PUBLIC API: Recording-Query für direkten Aufruf (z.B. aus MusicPlayer-Fallback).

    Rückgabe: (score, mb_artist, mb_title, mb_mbid, mb_album, mb_album_date, first_release, duration_ms)
    """
    return _musicbrainz_query_recording(title_part, artist_part)


def musicbrainz_query_artist_info(mbid):
    """
    PUBLIC API: Holt Gründungsjahr, Bandmitglieder und Genre für eine Artist-MBID.

    Nutzt einen In-Memory-Cache damit pro Song-Wechsel beim selben Künstler
    kein erneuter API-Call erfolgt.

    Rückgabe: (band_formed, band_members, genre) - alle Strings, können leer sein
    """
    if not mbid:
        return '', '', ''

    if mbid in _artist_info_cache:
        log_debug(f"Artist-Info Cache-Treffer für MBID={mbid}")
        return _artist_info_cache[mbid]

    url = f"{MUSICBRAINZ_ARTIST_URL}{mbid}"
    params = {"inc": "artist-rels+genres", "fmt": "json"}

    try:
        data = _mb_client.get(url, params=params).json()

        # Genre: nach Vote-Count sortiert, Top-Genre verwenden
        genres = data.get("genres", [])
        if genres:
            top_genre = sorted(genres, key=lambda g: g.get("count", 0), reverse=True)[0]
            genre = top_genre.get("name", "")
        else:
            genre = ""

        # Gründungsjahr und Mitglieder nur für Bands (Groups)
        if data.get("type") != "Group":
            result = ('', '', genre)
            _artist_cache_set(mbid, result)
            return result

        # Gründungsjahr
        life_span = data.get("life-span", {})
        raw_begin = (life_span.get("begin") or "")[:4]
        band_formed = raw_begin if raw_begin.isdigit() else ''

        # Bandmitglieder: Relations mit type="member of band" und direction="backward"
        relations = data.get("relations", [])
        member_names = [
            rel["artist"]["name"]
            for rel in relations
            if (
                isinstance(rel, dict)
                and rel.get("type") == "member of band"
                and rel.get("direction") == "backward"
                and not rel.get("ended", False)
                and isinstance(rel.get("artist"), dict)
                and rel["artist"].get("name")
            )
        ]
        band_members = ", ".join(member_names)

        log_info(
            f"Artist-Info für MBID={mbid}: "
            f"BandFormed='{band_formed}', Members='{band_members}', Genre='{genre}'"
        )

        result = (band_formed, band_members, genre)
        _artist_cache_set(mbid, result)
        return result

    except Exception as e:
        log_warning(f"Fehler beim Artist-Info Lookup (MBID={mbid}): {e}")
        # Negativen Cache-Eintrag setzen um Wiederholungs-Requests zu vermeiden
        _artist_cache_set(mbid, ('', '', ''))
        return '', '', ''


__all__ = [
    'mb_similarity',
    'identify_artist_title_via_musicbrainz',
    'musicbrainz_query_recording',
    'musicbrainz_query_artist_info',
]
