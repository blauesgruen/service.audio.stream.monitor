"""
Rule-based song-end detector.

Keywords are treated as hints only. A song end is triggered only when
multiple guards are satisfied (age, hold, no fresh song, optional extra signal).
"""
import re
from constants import GENERIC_STRING_MIN_LEN, GENERIC_STRING_MAX_DIGIT_SEQ
from metadata import is_song_pair as _is_song_pair


class SongEndDetector:
    def __init__(self):
        self.reset()

    def reset(self):
        self._active_song_key = ("", "")
        self._active_station = ""
        self._hold_since_ts = 0.0
        self._api_stale_since_ts = 0.0
        self._keyword_hits = 0
        self._last_signature = ""

    def on_song_started(self, song_key=("", ""), station_name=""):
        key = (
            str((song_key or ("", ""))[0] or "").strip(),
            str((song_key or ("", ""))[1] or "").strip(),
        )
        if key == self._active_song_key and str(station_name or "").strip().lower() == self._active_station:
            return
        self._active_song_key = key
        self._active_station = str(station_name or "").strip().lower()
        self._hold_since_ts = 0.0
        self._api_stale_since_ts = 0.0
        self._keyword_hits = 0
        self._last_signature = ""

    @staticmethod
    def _normalize_pair(pair):
        if not pair:
            return ("", "")
        return (str(pair[0] or "").strip(), str(pair[1] or "").strip())

    @staticmethod
    def _normalize_keywords(keywords):
        normalized = []
        for item in list(keywords or []):
            token = str(item or "").strip().lower()
            if token and token not in normalized:
                normalized.append(token)
        return normalized

    @staticmethod
    def _safe_float(value, default_value):
        try:
            return float(value)
        except Exception:
            return float(default_value)

    @staticmethod
    def _safe_int(value, default_value):
        try:
            return int(value)
        except Exception:
            return int(default_value)

    @staticmethod
    def _normalize_policy(policy):
        data = dict(policy or {})
        return {
            "enabled": bool(data.get("enabled", True)),
            "min_song_age_s": max(0.0, SongEndDetector._safe_float(data.get("min_song_age_s", 45.0), 45.0)),
            "hold_s": max(0.0, SongEndDetector._safe_float(data.get("hold_s", 8.0), 8.0)),
            "min_keyword_hits": max(1, SongEndDetector._safe_int(data.get("min_keyword_hits", 2), 2)),
            "min_non_song_sources": max(1, SongEndDetector._safe_int(data.get("min_non_song_sources", 2), 2)),
            "require_additional_signal": bool(data.get("require_additional_signal", True)),
            "stale_api_min_s": max(0.0, SongEndDetector._safe_float(data.get("stale_api_min_s", 12.0), 12.0)),
            "near_timeout_s": max(0.0, SongEndDetector._safe_float(data.get("near_timeout_s", 30.0), 30.0)),
            "generic_keywords": SongEndDetector._normalize_keywords(data.get("generic_keywords", [])),
        }

    @staticmethod
    def _is_generic_text(text, station_name, keywords):
        value = str(text or "").strip().lower()
        if not value:
            return False
        station_l = str(station_name or "").strip().lower()
        if station_l and station_l in value:
            return True
        for token in keywords:
            if token and token in value:
                return True
        return False

    @staticmethod
    def extract_candidate_keywords(texts, station_name, configured_keywords):
        station_l = str(station_name or "").strip().lower()
        configured = set(SongEndDetector._normalize_keywords(configured_keywords))
        digit_seq_pattern = re.compile(r"\d{" + str(int(GENERIC_STRING_MAX_DIGIT_SEQ) + 1) + r",}")
        seen = []
        for value in texts:
            normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
            if not normalized:
                continue
            if len(normalized) < GENERIC_STRING_MIN_LEN:
                continue
            if digit_seq_pattern.search(normalized):
                continue
            if station_l and station_l in normalized:
                continue
            if normalized in configured:
                continue
            if normalized not in seen:
                seen.append(normalized)
        return seen

    @staticmethod
    def _keyword_matches(texts, keywords):
        if not keywords:
            return []
        haystack = " | ".join(str(item or "").lower() for item in texts if item)
        if not haystack:
            return []
        matches = []
        for token in keywords:
            if token and token in haystack and token not in matches:
                matches.append(token)
        return matches

    @staticmethod
    def _signature(texts):
        joined = "|".join(sorted(str(v or "").strip().lower() for v in texts if v))
        if len(joined) > 2000:
            joined = joined[:2000]
        return joined

    def evaluate(
        self,
        now_ts,
        station_name,
        last_song_key,
        song_started_ts,
        song_timeout_s,
        source_pairs,
        source_texts,
        policy,
    ):
        cfg = self._normalize_policy(policy)
        if not cfg["enabled"]:
            return {"should_clear": False}

        song_key = self._normalize_pair(last_song_key)
        if not _is_song_pair(song_key):
            self.reset()
            return {"should_clear": False}

        if song_key != self._active_song_key or str(station_name or "").strip().lower() != self._active_station:
            self.on_song_started(song_key, station_name=station_name)

        song_age_s = 0.0
        if song_started_ts:
            song_age_s = max(0.0, float(now_ts) - float(song_started_ts))
        if song_age_s < cfg["min_song_age_s"]:
            self._hold_since_ts = 0.0
            self._keyword_hits = 0
            self._last_signature = ""
            return {"should_clear": False}

        pairs = {}
        for source_name, pair in dict(source_pairs or {}).items():
            pairs[str(source_name)] = self._normalize_pair(pair)
        texts = [str(v or "") for v in dict(source_texts or {}).values()]

        keywords = cfg["generic_keywords"]
        matched_keywords = self._keyword_matches(texts, keywords)

        states = {}
        fresh_song_count = 0
        generic_count = 0
        empty_count = 0
        api_same_song = False

        for source_name in ("api", "icy", "listitem", "playing_item", "jsonrpc"):
            pair = pairs.get(source_name, ("", ""))
            text_value = str((source_texts or {}).get(source_name, "") or "")
            state = "empty"
            if _is_song_pair(pair):
                if pair == song_key:
                    state = "same_song"
                elif self._is_generic_text(f"{pair[0]} - {pair[1]}", station_name, keywords):
                    state = "generic"
                else:
                    state = "fresh_song"
            elif self._is_generic_text(text_value, station_name, keywords):
                state = "generic"

            if state == "fresh_song":
                fresh_song_count += 1
            elif state == "generic":
                generic_count += 1
            else:
                empty_count += 1
            if source_name == "api" and state == "same_song":
                api_same_song = True
            states[source_name] = state

        # Nur Texte aus Quellen ohne Song-Inhalt als Kandidaten extrahieren
        non_song_texts = [
            str((source_texts or {}).get(name, "") or "")
            for name, state in states.items()
            if state in ("generic", "empty")
        ]
        candidate_keywords = self.extract_candidate_keywords(non_song_texts, station_name, keywords)

        if api_same_song:
            if self._api_stale_since_ts <= 0.0:
                self._api_stale_since_ts = float(now_ts)
        else:
            self._api_stale_since_ts = 0.0

        api_stale_s = 0.0
        api_stale = False
        if self._api_stale_since_ts > 0.0:
            api_stale_s = max(0.0, float(now_ts) - self._api_stale_since_ts)
            api_stale = api_stale_s >= cfg["stale_api_min_s"]

        non_song_count = generic_count + empty_count
        hold_active = bool(fresh_song_count == 0 and non_song_count >= cfg["min_non_song_sources"])
        if hold_active:
            if self._hold_since_ts <= 0.0:
                self._hold_since_ts = float(now_ts)
                self._keyword_hits = 0
                self._last_signature = ""
            signature = self._signature(texts)
            if signature and signature != self._last_signature:
                self._keyword_hits += len(matched_keywords)
                self._last_signature = signature
        else:
            self._hold_since_ts = 0.0
            self._keyword_hits = 0
            self._last_signature = ""

        hold_elapsed_s = 0.0
        if self._hold_since_ts > 0.0:
            hold_elapsed_s = max(0.0, float(now_ts) - self._hold_since_ts)

        near_timeout = False
        if song_timeout_s and song_timeout_s > 0:
            remaining = float(song_timeout_s) - float(song_age_s)
            near_timeout = remaining <= cfg["near_timeout_s"]

        additional_signal = bool(api_stale or near_timeout or generic_count > cfg["min_non_song_sources"])
        keyword_gate = self._keyword_hits >= cfg["min_keyword_hits"]
        hold_gate = hold_elapsed_s >= cfg["hold_s"]
        additional_gate = additional_signal or (not cfg["require_additional_signal"])

        should_clear = bool(
            hold_active
            and fresh_song_count == 0
            and hold_gate
            and keyword_gate
            and additional_gate
        )

        reason_parts = []
        if hold_gate:
            reason_parts.append(f"hold={hold_elapsed_s:.1f}s")
        if keyword_gate:
            reason_parts.append(f"kw_hits={self._keyword_hits}")
        if api_stale:
            reason_parts.append(f"api_stale={api_stale_s:.1f}s")
        if near_timeout:
            reason_parts.append("near_timeout")
        reason = ",".join(reason_parts) if reason_parts else ""

        return {
            "should_clear": should_clear,
            "reason": reason,
            "states": states,
            "matched_keywords": matched_keywords,
            "candidate_keywords": candidate_keywords,
            "keyword_hit_count": int(self._keyword_hits),
            "hold_elapsed_s": round(hold_elapsed_s, 3),
            "api_stale_s": round(api_stale_s, 3),
            "fresh_song_count": int(fresh_song_count),
            "non_song_count": int(non_song_count),
        }
