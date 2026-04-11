"""
Centralized pre-MusicBrainz plausibility policy.

Shared checks that run before MB lookups.
"""

import re

from constants import (
    NUMERIC_ID_PATTERN as _NUMERIC_ID_RE,
    STATION_NAME_MATCH_MIN_LEN as _STATION_NAME_MATCH_MIN_LEN,
)
from metadata import is_song_pair as _is_song_pair, is_generic_song_pair as _is_generic_song_pair


NON_SONG_TEXT_KEYWORDS = (
    "anruf",
    "hotline",
    "verkehr",
    "studio",
    "nachrichten",
)
PHONE_BLOCK_PATTERN = re.compile(r"\b(?:0\d{2,4}[\s\-]?\d{2,}[\s\-]?\d{1,})\b")
NUMERIC_PAIR_PART_PATTERN = re.compile(r"^\d{3,}$")


def normalize_station_compare_text(text):
    value = str(text or "").strip().lower()
    if not value:
        return ""
    value = value.replace("&", " and ")
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def build_station_hints(raw_values):
    hints = []
    seen = set()
    for candidate in list(raw_values or []):
        val = str(candidate or "").strip()
        if not val:
            continue
        variants = [val]
        if "-" in val or "_" in val:
            variants.append(val.replace("-", " ").replace("_", " "))
        for raw in variants:
            normalized = normalize_station_compare_text(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            hints.append(normalized)
    return hints


def is_station_name_match_pair(pair, station_hints, min_len=_STATION_NAME_MATCH_MIN_LEN):
    if not _is_song_pair(pair):
        return False
    pair_text = normalize_station_compare_text(f"{pair[0]} {pair[1]}")
    if not pair_text:
        return False
    for station_hint in list(station_hints or []):
        hint = str(station_hint or "").strip().lower()
        if len(hint) < int(min_len):
            continue
        if hint in pair_text:
            return True
    return False


def is_obvious_non_song_text(text, extra_keywords=()):
    value = str(text or "").strip().lower()
    if not value:
        return False
    all_tokens = set(NON_SONG_TEXT_KEYWORDS)
    for token in list(extra_keywords or []):
        t = str(token or "").strip().lower()
        if t:
            all_tokens.add(t)
    if any(token in value for token in all_tokens):
        return True
    return bool(PHONE_BLOCK_PATTERN.search(value))


def normalize_candidate_pair(artist, title, invalid_values=()):
    a = str(artist or "").strip()
    t = str(title or "").strip()
    if not a or not t:
        return "", ""
    invalid = set(str(v) for v in list(invalid_values or []))
    if a in invalid or t in invalid:
        return "", ""
    if NUMERIC_PAIR_PART_PATTERN.match(a) and NUMERIC_PAIR_PART_PATTERN.match(t):
        return "", ""
    if _NUMERIC_ID_RE.match(a) or _NUMERIC_ID_RE.match(t):
        return "", ""
    return a, t


def looks_like_numeric_id_pair(pair):
    if not _is_song_pair(pair):
        return False
    left = str(pair[0] or "").strip()
    right = str(pair[1] or "").strip()
    return bool(NUMERIC_PAIR_PART_PATTERN.match(left) and NUMERIC_PAIR_PART_PATTERN.match(right))


def sanitize_pair_for_pre_mb(
    pair,
    station_name="",
    invalid_values=(),
    extra_keywords=(),
    station_hints=(),
    station_match_min_len=_STATION_NAME_MATCH_MIN_LEN,
    reject_generic=True,
    reject_station_match=False,
    reject_obvious_text=False,
):
    if not pair:
        return "", ""
    normalized = normalize_candidate_pair(pair[0], pair[1], invalid_values=invalid_values)
    if not normalized[0] or not normalized[1]:
        return "", ""

    if looks_like_numeric_id_pair(normalized):
        return "", ""
    if reject_generic and _is_generic_song_pair(normalized, station_name, extra_keywords):
        return "", ""
    if reject_station_match and is_station_name_match_pair(
        normalized,
        station_hints=station_hints,
        min_len=station_match_min_len,
    ):
        return "", ""
    if reject_obvious_text and is_obvious_non_song_text(
        f"{normalized[0]} - {normalized[1]}",
        extra_keywords=extra_keywords,
    ):
        return "", ""
    return normalized


def is_pre_mb_plausible_pair(
    pair,
    station_name="",
    invalid_values=(),
    extra_keywords=(),
    station_hints=(),
    station_match_min_len=_STATION_NAME_MATCH_MIN_LEN,
    reject_generic=True,
    reject_station_match=False,
    reject_obvious_text=False,
):
    sanitized = sanitize_pair_for_pre_mb(
        pair,
        station_name=station_name,
        invalid_values=invalid_values,
        extra_keywords=extra_keywords,
        station_hints=station_hints,
        station_match_min_len=station_match_min_len,
        reject_generic=reject_generic,
        reject_station_match=reject_station_match,
        reject_obvious_text=reject_obvious_text,
    )
    return bool(sanitized[0] and sanitized[1])
