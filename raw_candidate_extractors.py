"""
Helper functions to extract artist/title pairs from raw Kodi payloads.
This module is Kodi-agnostic and intended for analysis enrichment.
"""
import json
from text_encoding import normalize_text


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return ""
    return normalize_text(value, collapse_whitespace=True)


def _split_pair(text):
    value = _as_text(text)
    if not value:
        return "", ""
    if " - " in value:
        left, right = value.split(" - ", 1)
        return left.strip(), right.strip()
    return "", value


def _load_json_object(raw_text):
    text = _as_text(raw_text)
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_listitem_pair(raw_text):
    payload = _load_json_object(raw_text)
    artist = _as_text(payload.get("artist"))
    title = _as_text(payload.get("title")) or _as_text(payload.get("label"))
    if artist and title:
        return artist, title
    split_artist, split_title = _split_pair(title)
    return artist or split_artist, split_title


def extract_playing_item_pair(raw_text):
    payload = _load_json_object(raw_text)
    artist = _as_text(payload.get("getArtist"))
    title = _as_text(payload.get("getTitle")) or _as_text(payload.get("getLabel"))
    if artist and title:
        return artist, title
    split_artist, split_title = _split_pair(title)
    return artist or split_artist, split_title


def extract_jsonrpc_pair(raw_text):
    payload = _load_json_object(raw_text)
    item = payload.get("item", {})
    if isinstance(item, dict) and "item" in item and isinstance(item.get("item"), dict):
        item = item.get("item", {})
    if not isinstance(item, dict):
        item = {}

    artist = _as_text(item.get("artist")) or _as_text(item.get("displayartist"))
    title = _as_text(item.get("title")) or _as_text(item.get("label"))
    if artist and title:
        return artist, title
    split_artist, split_title = _split_pair(title)
    return artist or split_artist, split_title
