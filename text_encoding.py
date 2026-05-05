"""
Shared text decoding and Mojibake repair helpers.
"""

from __future__ import annotations

import re
import unicodedata

MOJIBAKE_HINT_RE = re.compile(r"(?:Ã.|Â.|â..)")
CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?(?P<charset>[^;,'\"\s]+)", flags=re.IGNORECASE)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
COMMON_MOJIBAKE_REPLACEMENTS = {
    "â€¢": "•",
    "â€“": "–",
    "â€”": "—",
    "â€˜": "‘",
    "â€™": "’",
    "â€œ": "“",
    "â€": "”",
    "â€¦": "…",
}


def repair_mojibake_text(value, max_passes=3):
    text = str(value or "")
    if not text:
        return text
    repaired = text
    for _ in range(max(1, int(max_passes or 1))):
        if not MOJIBAKE_HINT_RE.search(repaired):
            break
        try:
            candidate = repaired.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if not candidate or candidate == repaired:
            break
        repaired = candidate
    return repaired or text


def normalize_text(value, collapse_whitespace=False):
    text = str(value or "")
    if not text:
        return ""
    text = repair_mojibake_text(text)
    for broken, fixed in COMMON_MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    text = text.replace("\xa0", " ")
    text = ZERO_WIDTH_RE.sub("", text)
    text = CONTROL_RE.sub(" ", text)
    text = unicodedata.normalize("NFC", text)
    if collapse_whitespace:
        text = " ".join(text.split())
    return text.strip()


def extract_charset_from_content_type(content_type):
    text = str(content_type or "").strip()
    if not text:
        return ""
    match = CHARSET_RE.search(text)
    if not match:
        return ""
    return match.group("charset").strip().strip("'\"").lower()


def decode_text_bytes(payload, content_type="", fallback_encodings=("utf-8", "cp1252", "latin-1")):
    data = payload or b""
    if not data:
        return ""

    tried = set()
    encodings = []
    declared_charset = extract_charset_from_content_type(content_type)
    if declared_charset:
        encodings.append(declared_charset)
    encodings.extend(fallback_encodings)

    decoded = ""
    for encoding in encodings:
        normalized = str(encoding or "").strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            decoded = data.decode(normalized)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        decoded = data.decode("utf-8", errors="replace")

    return normalize_text(decoded)
