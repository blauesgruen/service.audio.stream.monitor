"""
Central raw-source label handling for Kodi window properties.
"""
import json

from constants import PropertyNames as _P


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return str(value)


def snapshot_getters(obj):
    """
    Collects all callable no-arg getter methods from an object into a dict.
    Safe for Kodi tags across versions (unknown methods are skipped).
    """
    data = {}
    if obj is None:
        return data

    for name in dir(obj):
        if not name.startswith("get"):
            continue
        method = getattr(obj, name, None)
        if not callable(method):
            continue
        try:
            value = method()
        except TypeError:
            continue
        except Exception:
            continue
        if value is None or value == "":
            continue
        data[name] = value
    return data


class RawSourceLabels:
    ALL_KEYS = (
        _P.RAW_STREAM_URL,
        _P.RAW_PLUGIN_URL,
        _P.RAW_STREAM_HOST,
        _P.RAW_STREAM_QUERY,
        _P.RAW_STREAM_HEADERS,
        _P.RAW_ICY_METADATA,
        _P.RAW_ICY_STREAMTITLE,
        _P.RAW_ICY_PARSED,
        _P.RAW_PLAYING_ITEM,
        _P.RAW_JSONRPC_PLAYER,
        _P.RAW_LISTITEM,
        _P.RAW_PLAYER_ART,
        _P.RAW_RADIODE_WINDOW,
        _P.RAW_RDS,
        _P.RAW_API_RADIODE_DETAILS,
        _P.RAW_API_RADIODE_NOWPLAYING,
        _P.RAW_API_TUNEIN_JSON,
        _P.RAW_API_TUNEIN_TEXT,
        _P.RAW_API_LAST_CONTEXT,
        _P.RAW_API_LAST_PAYLOAD,
    )

    API_CONTEXT_MAP = {
        "radiode.details": _P.RAW_API_RADIODE_DETAILS,
        "radiode.now_playing.slug": _P.RAW_API_RADIODE_NOWPLAYING,
        "radiode.now_playing.search": _P.RAW_API_RADIODE_NOWPLAYING,
        "tunein.json": _P.RAW_API_TUNEIN_JSON,
        "tunein.text": _P.RAW_API_TUNEIN_TEXT,
    }

    def __init__(self, window, log_debug=None, max_text_len=12000):
        self.window = window
        self.log_debug = log_debug
        self.max_text_len = int(max_text_len)

    def _truncate(self, text, max_len=None):
        limit = int(max_len or self.max_text_len)
        value = _to_text(text)
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    def set_text(self, key, value, max_len=None):
        text = self._truncate(value, max_len=max_len)
        if text:
            self.window.setProperty(key, text)
        else:
            self.window.clearProperty(key)

    def set_json(self, key, payload, max_len=None):
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = _to_text(payload)
        self.set_text(key, text, max_len=max_len)

    def clear_all(self):
        for key in self.ALL_KEYS:
            self.window.clearProperty(key)

    def set_api_payload(self, context, payload):
        ctx = str(context or "").strip()
        self.set_text(_P.RAW_API_LAST_CONTEXT, ctx, max_len=256)
        self.set_json(_P.RAW_API_LAST_PAYLOAD, payload, max_len=12000)

        target = self.API_CONTEXT_MAP.get(ctx)
        if target:
            self.set_json(target, payload, max_len=12000)
        elif self.log_debug:
            self.log_debug(f"RAW API context ohne Mapping: {ctx}")
