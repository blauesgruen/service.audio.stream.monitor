import json
import os
import time
import hashlib
from datetime import datetime

from constants import (
    SOURCE_FAMILIES,
    SOURCE_STATS_FAMILIES,
    ICY_FORMAT_KEYS,
    SOURCE_POLICY_SINGLE_CONFIRM_POLLS,
    SONG_END_DETECTOR_ENABLED,
    SONG_END_HOLD_S,
    SONG_END_MIN_KEYWORD_HITS,
    SONG_END_MIN_NON_SONG_SOURCES,
    SONG_END_MIN_SONG_AGE_S,
    SONG_END_NEAR_TIMEOUT_S,
    SONG_END_REQUIRE_ADDITIONAL_SIGNAL,
    SONG_END_STALE_API_MIN_S,
    SOURCE_POLICY_SWITCH_MARGIN,
    STATION_PROFILE_ALPHA,
    STATION_PROFILE_CONFIDENCE_HIGH,
    STATION_PROFILE_CONFIDENCE_LOW,
    STATION_PROFILE_DIRNAME,
    STATION_PROFILE_FILENAME,
    SONG_DB_FILENAME,
    STATION_PROFILE_ICY_STRUCTURAL_GENERIC_THRESHOLD,
    STATION_PROFILE_MP_ABSENT_SONG_RATE_MAX,
    STATION_PROFILE_MP_NOISE_FLIP_RATE_MIN,
    STATION_PROFILE_MP_NOISE_RELIABLE_EMA_MAX,
    STATION_PROFILE_MIN_SESSION_S,
    STATION_PROFILE_MIN_STABLE_SESSIONS,
    SOURCE_GROUP_FAMILIES,
    SOURCE_GROUP_DB_MIN_SAMPLES,
    SOURCE_GROUP_DB_MIN_SHARE,
    SOURCE_GROUP_DB_SWAP_MIN_SAMPLES,
    SOURCE_GROUP_DB_SWAP_MIN_SHARE,
)
from song_db import SongDatabase
from metadata import is_song_pair as _valid_pair


FAMILIES = SOURCE_FAMILIES


def _clamp(value, low, high):
    return max(low, min(high, value))


def _safe_div(num, den):
    if not den:
        return 0.0
    return float(num) / float(den)


def _today_iso():
    return datetime.utcnow().strftime('%Y-%m-%d')


def _normalize_family(value):
    family = str(value or '').strip().lower()
    if family in FAMILIES:
        return family
    return ''


def _default_song_end_policy():
    return {
        'enabled': bool(SONG_END_DETECTOR_ENABLED),
        'min_song_age_s': float(SONG_END_MIN_SONG_AGE_S),
        'hold_s': float(SONG_END_HOLD_S),
        'min_keyword_hits': int(SONG_END_MIN_KEYWORD_HITS),
        'min_non_song_sources': int(SONG_END_MIN_NON_SONG_SOURCES),
        'require_additional_signal': bool(SONG_END_REQUIRE_ADDITIONAL_SIGNAL),
        'stale_api_min_s': float(SONG_END_STALE_API_MIN_S),
        'near_timeout_s': float(SONG_END_NEAR_TIMEOUT_S),
    }


class StationProfileSession:
    """
    Runtime collector for one station playback session.
    """

    def __init__(self, station_key, station_name=''):
        self.station_key = station_key
        self.station_name = station_name
        self.started_ts = time.time()
        self.last_ts = self.started_ts
        self.polls = 0

        self.winner_counts = {family: 0 for family in FAMILIES}
        self.source_stats = {
            family: {
                'samples': 0,
                'song': 0,
                'generic': 0,
                'empty': 0,
                'match_current': 0,
                'other_song': 0,
            }
            for family in FAMILIES
        }

        self.icy_format_counts = {key: 0 for key in ICY_FORMAT_KEYS}
        self.api_present_samples = 0

        self.mp_song_samples = 0
        self.mp_match_samples = 0
        self.mp_other_song_samples = 0
        self._last_mp_state = ''
        self.mp_state_flips = 0
        self.mp_state_observations = 0
        self.icy_winner_direct = 0
        self.icy_winner_swapped = 0

        self.api_lag_sum = 0.0
        self.api_lag_count = 0
        self._pending_icy_pair = ('', '')
        self._pending_icy_poll = -1
        self._last_api_pair = ('', '')
        self._last_icy_pair = ('', '')

    def observe(self, observation, context):
        self.polls += 1
        self.last_ts = time.time()

        winner = _normalize_family((observation or {}).get('winner_family'))
        if winner:
            self.winner_counts[winner] += 1
        winner_source_detail = str((context or {}).get('winner_source_detail', '') or '').strip().lower()
        if winner_source_detail.startswith('icy_swapped'):
            self.icy_winner_swapped += 1
        elif winner_source_detail.startswith('icy'):
            self.icy_winner_direct += 1

        sources = (observation or {}).get('sources', {})
        api_has_data_now = False
        for family in FAMILIES:
            state = 'empty'
            src = sources.get(family, {}) if isinstance(sources, dict) else {}
            if isinstance(src, dict):
                state_value = str(src.get('state', 'empty') or 'empty').strip().lower()
                if state_value in ('song', 'generic', 'empty'):
                    state = state_value
            stats = self.source_stats[family]
            stats['samples'] += 1
            stats[state] += 1
            if src.get('match_current'):
                stats['match_current'] += 1
            if src.get('other_song'):
                stats['other_song'] += 1

            if family == 'api' and state != 'empty':
                api_has_data_now = True

            if family == 'musicplayer' and state == 'song':
                self.mp_song_samples += 1
                if src.get('match_current'):
                    self.mp_match_samples += 1
                if src.get('other_song'):
                    self.mp_other_song_samples += 1
            if family == 'musicplayer':
                self.mp_state_observations += 1
                if self._last_mp_state and state != self._last_mp_state:
                    self.mp_state_flips += 1
                self._last_mp_state = state

        if api_has_data_now:
            self.api_present_samples += 1

        format_hint = str((context or {}).get('icy_format', '') or '').strip().lower()
        if format_hint in self.icy_format_counts:
            self.icy_format_counts[format_hint] += 1

        self._observe_api_lag(context)

    def _observe_api_lag(self, context):
        context = context or {}
        stream_title_changed = bool(context.get('stream_title_changed'))
        icy_pair = context.get('current_icy_pair') or ('', '')
        api_pair = context.get('current_api_pair') or ('', '')
        icy_is_song = bool(context.get('icy_is_song'))

        if stream_title_changed and icy_is_song and _valid_pair(icy_pair):
            if icy_pair != self._last_icy_pair:
                self._pending_icy_pair = icy_pair
                self._pending_icy_poll = self.polls

        if _valid_pair(api_pair) and api_pair != self._last_api_pair:
            if self._pending_icy_poll >= 0 and api_pair == self._pending_icy_pair:
                lag = max(0, self.polls - self._pending_icy_poll)
                self.api_lag_sum += float(lag)
                self.api_lag_count += 1
                self._pending_icy_pair = ('', '')
                self._pending_icy_poll = -1

        if _valid_pair(icy_pair):
            self._last_icy_pair = icy_pair
        if _valid_pair(api_pair):
            self._last_api_pair = api_pair

    def duration_seconds(self):
        return max(0.0, float(self.last_ts - self.started_ts))

    def build_metrics(self):
        total_winners = sum(self.winner_counts.values())
        winner_shares = {
            family: _safe_div(self.winner_counts.get(family, 0), total_winners)
            for family in FAMILIES
        }
        dominant_source = max(FAMILIES, key=lambda fam: winner_shares.get(fam, 0.0)) if total_winners else ''

        icy_stats = self.source_stats['icy']
        icy_generic_rate = _safe_div(icy_stats.get('generic', 0), icy_stats.get('samples', 0))

        api_available = _safe_div(self.api_present_samples, self.polls) >= 0.20

        mp_match_rate = _safe_div(self.mp_match_samples, self.mp_song_samples)
        mp_other_rate = _safe_div(self.mp_other_song_samples, self.mp_song_samples)
        mp_song_rate = _safe_div(self.mp_song_samples, self.polls)
        mp_flip_rate = _safe_div(self.mp_state_flips, max(0, self.mp_state_observations - 1))
        mp_reliable = bool(
            self.mp_song_samples >= 3
            and mp_match_rate >= 0.65
            and mp_other_rate <= 0.25
        )

        total_formats = sum(self.icy_format_counts.values())
        format_shares = {
            key: _safe_div(self.icy_format_counts.get(key, 0), total_formats)
            for key in ICY_FORMAT_KEYS
        }
        icy_format = max(ICY_FORMAT_KEYS, key=lambda key: format_shares.get(key, 0.0)) if total_formats else 'unknown'

        api_lag_cycles = None
        if self.api_lag_count > 0:
            api_lag_cycles = self.api_lag_sum / float(self.api_lag_count)
        icy_winner_total = int(self.icy_winner_direct + self.icy_winner_swapped)
        icy_swapped_winner_share = _safe_div(self.icy_winner_swapped, icy_winner_total)
        icy_prefer_swapped = bool(icy_winner_total >= 3 and icy_swapped_winner_share >= 0.60)

        return {
            'duration_s': self.duration_seconds(),
            'polls': self.polls,
            'dominant_source': dominant_source,
            'winner_shares': winner_shares,
            'icy_generic_rate': icy_generic_rate,
            'api_available': api_available,
            'api_lag_cycles': api_lag_cycles,
            'mp_reliable': mp_reliable,
            'mp_song_rate': mp_song_rate,
            'mp_flip_rate': mp_flip_rate,
            'icy_swapped_winner_share': icy_swapped_winner_share,
            'icy_prefer_swapped': icy_prefer_swapped,
            'format_shares': format_shares,
            'icy_format': icy_format,
        }


class StationProfileStore:
    """
    Persistent station profile store with EMA learning and confidence tracking.
    Storage mode: one JSON file per station.
    """

    def __init__(
        self,
        storage_path,
        legacy_file_path=None,
        alpha=STATION_PROFILE_ALPHA,
        min_session_s=STATION_PROFILE_MIN_SESSION_S,
        min_stable_sessions=STATION_PROFILE_MIN_STABLE_SESSIONS,
    ):
        raw_path = str(storage_path or '').strip()
        if raw_path.lower().endswith('.json'):
            # Legacy constructor support: previously a single aggregate file path.
            base_dir = os.path.dirname(raw_path)
            self.profile_dir = os.path.join(base_dir, STATION_PROFILE_DIRNAME)
            self.legacy_file_path = str(legacy_file_path or raw_path)
        else:
            self.profile_dir = raw_path
            default_legacy = os.path.join(os.path.dirname(self.profile_dir), STATION_PROFILE_FILENAME)
            self.legacy_file_path = str(legacy_file_path or default_legacy)

        self.alpha = float(alpha)
        self.min_session_s = int(min_session_s)
        self.min_stable_sessions = int(min_stable_sessions)
        self._profiles = {}
        self._dirty_keys = set()
        self._last_flush_ts = 0.0
        db_path = os.path.join(os.path.dirname(self.profile_dir), SONG_DB_FILENAME)
        self._song_db = SongDatabase(db_path)
        self._prepare_storage_dir()
        self._migrate_legacy_aggregate()

    def _prepare_storage_dir(self):
        try:
            if self.profile_dir and not os.path.exists(self.profile_dir):
                os.makedirs(self.profile_dir, exist_ok=True)
        except Exception:
            pass

    def _station_file_path(self, station_key):
        if not self.profile_dir or not station_key:
            return ''
        key = str(station_key or '').strip().lower()
        slug = ''.join(ch if ch.isalnum() else '_' for ch in key).strip('_')
        if not slug:
            slug = 'station'
        slug = slug[:80]
        key_hash = hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
        file_name = f"{slug}__{key_hash}.json"
        return os.path.join(self.profile_dir, file_name)

    def _load_profile_from_file(self, station_key):
        file_path = self._station_file_path(station_key)
        if not file_path or not os.path.exists(file_path):
            return None
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                if isinstance(payload.get('profile'), dict):
                    return payload.get('profile')
                return payload
        except Exception:
            return None
        return None

    def _migrate_legacy_aggregate(self):
        try:
            if not self.legacy_file_path or not os.path.exists(self.legacy_file_path):
                return
            with open(self.legacy_file_path, 'r', encoding='utf-8') as f:
                payload = json.load(f)

            stations = {}
            if isinstance(payload, dict) and isinstance(payload.get('stations'), dict):
                stations = payload.get('stations') or {}
            if not stations:
                return

            for station_key, profile in stations.items():
                if not isinstance(profile, dict):
                    continue
                ensured = self._profile_defaults()
                for key, value in ensured.items():
                    if key in profile:
                        ensured[key] = profile[key]
                self._profiles[str(station_key)] = ensured
                self._dirty_keys.add(str(station_key))

            self.flush()
            migrated_path = f"{self.legacy_file_path}.migrated.bak"
            try:
                os.replace(self.legacy_file_path, migrated_path)
            except Exception:
                pass
        except Exception:
            pass

    def _profile_defaults(self):
        return {
            'dominant_source': '',
            'icy_format': 'unknown',
            'icy_generic_rate': 0.0,
            'api_available': False,
            'api_lag_cycles': 0.0,
            'mp_reliable': False,
            'mp_song_rate': 0.0,
            'mp_flip_rate': 0.0,
            'song_end_policy': _default_song_end_policy(),
            'icy_structural_generic': False,
            'mp_absent': False,
            'mp_noise': False,
            'confidence': 0.25,
            'sessions': 0,
            'sessions_above_threshold': 0,
            'last_seen': '',
            'last_change_detected': None,
            'profile_alpha': self.alpha,
            'source_share_ema': {family: 1.0 / 3.0 for family in FAMILIES},
            'icy_format_share_ema': {key: (1.0 if key == 'unknown' else 0.0) for key in ICY_FORMAT_KEYS},
            'icy_generic_rate_ema': 0.0,
            'api_available_ema': 0.0,
            'api_lag_cycles_ema': 0.0,
            'mp_reliable_ema': 0.0,
            'mp_song_rate_ema': 0.0,
            'mp_flip_rate_ema': 0.0,
            'icy_swapped_winner_share_ema': 0.0,
            'icy_prefer_swapped': False,
            '_pending_dominant_source': '',
            '_pending_dominant_count': 0,
            '_pending_icy_format': '',
            '_pending_icy_format_count': 0,
        }

    def _normalize_song_end_policy(self, policy):
        defaults = _default_song_end_policy()
        data = policy if isinstance(policy, dict) else {}
        normalized = dict(defaults)

        if 'enabled' in data:
            normalized['enabled'] = bool(data.get('enabled'))
        for key in ('min_song_age_s', 'hold_s', 'stale_api_min_s', 'near_timeout_s'):
            if key in data:
                try:
                    normalized[key] = max(0.0, float(data.get(key)))
                except Exception:
                    pass
        for key in ('min_keyword_hits', 'min_non_song_sources'):
            if key in data:
                try:
                    normalized[key] = max(1, int(data.get(key)))
                except Exception:
                    pass
        if 'require_additional_signal' in data:
            normalized['require_additional_signal'] = bool(data.get('require_additional_signal'))
        return normalized

    def _ensure_profile(self, station_key):
        key = str(station_key or '')
        if not key:
            return self._profile_defaults()

        profile = self._profiles.get(key)
        if not isinstance(profile, dict):
            profile = self._load_profile_from_file(key)
        if not isinstance(profile, dict):
            profile = self._profile_defaults()
            self._dirty_keys.add(key)
        defaults = self._profile_defaults()
        for field, value in defaults.items():
            if field not in profile:
                profile[field] = value
        profile['song_end_policy'] = self._normalize_song_end_policy(profile.get('song_end_policy'))
        self._profiles[str(station_key)] = profile
        return profile

    def start_session(self, station_key, station_name=''):
        if not station_key:
            return None
        return StationProfileSession(station_key=station_key, station_name=station_name)

    def finish_session(self, session):
        if session is None or not session.station_key:
            return None

        profile = self._ensure_profile(session.station_key)
        metrics = session.build_metrics()
        today = _today_iso()

        profile['sessions'] = int(profile.get('sessions', 0)) + 1
        profile['last_seen'] = today
        above_threshold = metrics['duration_s'] >= float(self.min_session_s)
        if above_threshold:
            profile['sessions_above_threshold'] = int(profile.get('sessions_above_threshold', 0)) + 1
            self._merge_ema(profile, metrics)
            self._derive_profile_fields(profile)
            self._song_db.promote_strings(str(session.station_key))
            changed = self._track_profile_changes(profile, today)
            self._update_confidence(profile, metrics, changed, above_threshold=True)
        else:
            self._update_confidence(profile, metrics, changed=False, above_threshold=False)

        self._mark_public(profile)
        self._dirty_keys.add(str(session.station_key))
        return profile

    def _merge_ema(self, profile, metrics):
        alpha = float(profile.get('profile_alpha', self.alpha) or self.alpha)

        source_ema = profile.get('source_share_ema', {})
        for family in FAMILIES:
            prev = float(source_ema.get(family, 1.0 / 3.0))
            sample = float(metrics['winner_shares'].get(family, 0.0))
            source_ema[family] = (1.0 - alpha) * prev + alpha * sample
        profile['source_share_ema'] = source_ema

        format_ema = profile.get('icy_format_share_ema', {})
        for key in ICY_FORMAT_KEYS:
            prev = float(format_ema.get(key, 0.0))
            sample = float(metrics['format_shares'].get(key, 0.0))
            format_ema[key] = (1.0 - alpha) * prev + alpha * sample
        profile['icy_format_share_ema'] = format_ema

        profile['icy_generic_rate_ema'] = (1.0 - alpha) * float(profile.get('icy_generic_rate_ema', 0.0)) + alpha * float(metrics['icy_generic_rate'])
        profile['api_available_ema'] = (1.0 - alpha) * float(profile.get('api_available_ema', 0.0)) + alpha * (1.0 if metrics['api_available'] else 0.0)
        profile['mp_reliable_ema'] = (1.0 - alpha) * float(profile.get('mp_reliable_ema', 0.0)) + alpha * (1.0 if metrics['mp_reliable'] else 0.0)
        profile['mp_song_rate_ema'] = (1.0 - alpha) * float(profile.get('mp_song_rate_ema', 0.0)) + alpha * float(metrics.get('mp_song_rate', 0.0))
        profile['mp_flip_rate_ema'] = (1.0 - alpha) * float(profile.get('mp_flip_rate_ema', 0.0)) + alpha * float(metrics.get('mp_flip_rate', 0.0))
        profile['icy_swapped_winner_share_ema'] = (
            (1.0 - alpha) * float(profile.get('icy_swapped_winner_share_ema', 0.0))
            + alpha * float(metrics.get('icy_swapped_winner_share', 0.0))
        )

        if metrics['api_lag_cycles'] is not None:
            profile['api_lag_cycles_ema'] = (1.0 - alpha) * float(profile.get('api_lag_cycles_ema', 0.0)) + alpha * float(metrics['api_lag_cycles'])

    def _derive_profile_fields(self, profile):
        source_ema = profile.get('source_share_ema', {})
        dominant = max(FAMILIES, key=lambda fam: float(source_ema.get(fam, 0.0)))
        profile['_derived_dominant_source'] = dominant

        format_ema = profile.get('icy_format_share_ema', {})
        icy_format = max(ICY_FORMAT_KEYS, key=lambda key: float(format_ema.get(key, 0.0)))
        if float(format_ema.get(icy_format, 0.0)) < 0.35:
            icy_format = 'unknown'
        profile['_derived_icy_format'] = icy_format

    def _track_profile_changes(self, profile, today):
        changed = False
        changed |= self._track_field_change(profile, 'dominant_source', profile.get('_derived_dominant_source', ''), today)
        changed |= self._track_field_change(profile, 'icy_format', profile.get('_derived_icy_format', 'unknown'), today)
        return changed

    def _track_field_change(self, profile, field_name, candidate, today):
        current = str(profile.get(field_name, '') or '')
        candidate = str(candidate or '')
        if not candidate:
            return False
        if not current:
            profile[field_name] = candidate
            return False
        if candidate == current:
            profile[f'_pending_{field_name}'] = ''
            profile[f'_pending_{field_name}_count'] = 0
            return False

        pending_key = f'_pending_{field_name}'
        count_key = f'_pending_{field_name}_count'
        if profile.get(pending_key) == candidate:
            profile[count_key] = int(profile.get(count_key, 0)) + 1
        else:
            profile[pending_key] = candidate
            profile[count_key] = 1

        if int(profile.get(count_key, 0)) >= 2:
            profile[field_name] = candidate
            profile[pending_key] = ''
            profile[count_key] = 0
            profile['last_change_detected'] = today
            return True
        return False

    def _update_confidence(self, profile, metrics, changed, above_threshold):
        confidence = float(profile.get('confidence', 0.25))

        if not above_threshold:
            confidence = max(0.10, confidence - 0.01)
        else:
            delta = 0.0
            dominant = str(profile.get('dominant_source', '') or '')
            session_dominant = str(metrics.get('dominant_source', '') or '')
            if dominant and session_dominant:
                delta += 0.08 if dominant == session_dominant else -0.10

            expected_generic = float(profile.get('icy_generic_rate_ema', metrics.get('icy_generic_rate', 0.0)))
            if abs(float(metrics.get('icy_generic_rate', 0.0)) - expected_generic) <= 0.20:
                delta += 0.02
            else:
                delta -= 0.03

            expected_mp_reliable = float(profile.get('mp_reliable_ema', 0.0)) >= 0.60
            if bool(metrics.get('mp_reliable')) == expected_mp_reliable:
                delta += 0.03
            else:
                delta -= 0.04

            if changed:
                delta -= 0.12

            confidence = _clamp(confidence + delta, 0.0, 1.0)

        if int(profile.get('sessions_above_threshold', 0)) < int(self.min_stable_sessions):
            confidence = min(confidence, 0.45)

        profile['confidence'] = round(confidence, 3)

    def _mark_public(self, profile):
        profile['icy_generic_rate'] = round(float(profile.get('icy_generic_rate_ema', 0.0)), 3)
        profile['api_available'] = bool(float(profile.get('api_available_ema', 0.0)) >= 0.35)
        profile['mp_reliable'] = bool(float(profile.get('mp_reliable_ema', 0.0)) >= 0.60)
        profile['mp_song_rate'] = round(float(profile.get('mp_song_rate_ema', 0.0)), 3)
        profile['mp_flip_rate'] = round(float(profile.get('mp_flip_rate_ema', 0.0)), 3)
        profile['icy_prefer_swapped'] = bool(float(profile.get('icy_swapped_winner_share_ema', 0.0)) >= 0.60)

        api_lag = float(profile.get('api_lag_cycles_ema', 0.0))
        profile['api_lag_cycles'] = round(api_lag, 2) if api_lag > 0 else 0.0

        role_flags = self._derive_source_role_flags(profile)
        profile['icy_structural_generic'] = bool(role_flags.get('icy_structural_generic'))
        profile['mp_absent'] = bool(role_flags.get('mp_absent'))
        profile['mp_noise'] = bool(role_flags.get('mp_noise'))

        profile['profile_alpha'] = float(profile.get('profile_alpha', self.alpha))

    def _derive_source_role_flags(self, profile):
        icy_generic_rate = float(profile.get('icy_generic_rate_ema', 0.0))
        mp_song_rate = float(profile.get('mp_song_rate_ema', 0.0))
        mp_flip_rate = float(profile.get('mp_flip_rate_ema', 0.0))
        mp_reliable_ema = float(profile.get('mp_reliable_ema', 0.0))

        icy_structural_generic = bool(
            icy_generic_rate >= float(STATION_PROFILE_ICY_STRUCTURAL_GENERIC_THRESHOLD)
        )
        mp_absent = bool(
            mp_song_rate <= float(STATION_PROFILE_MP_ABSENT_SONG_RATE_MAX)
        )
        mp_noise = bool(
            (not mp_absent)
            and mp_flip_rate >= float(STATION_PROFILE_MP_NOISE_FLIP_RATE_MIN)
            and mp_reliable_ema <= float(STATION_PROFILE_MP_NOISE_RELIABLE_EMA_MAX)
        )
        return {
            'icy_structural_generic': icy_structural_generic,
            'mp_absent': mp_absent,
            'mp_noise': mp_noise,
        }

    def get_profile(self, station_key):
        if not station_key:
            return None
        key = str(station_key)
        profile = self._profiles.get(key)
        if not isinstance(profile, dict):
            profile = self._load_profile_from_file(key)
            if isinstance(profile, dict):
                self._profiles[key] = profile
        if not isinstance(profile, dict):
            return None
        defaults = self._profile_defaults()
        for field, value in defaults.items():
            if field not in profile:
                profile[field] = value
                self._dirty_keys.add(key)
        profile['song_end_policy'] = self._normalize_song_end_policy(profile.get('song_end_policy'))
        self._profiles[key] = profile
        return profile

    def get_song_end_policy(self, station_key):
        profile = self.get_profile(station_key)
        if not isinstance(profile, dict):
            return _default_song_end_policy()
        return self._normalize_song_end_policy(profile.get('song_end_policy'))

    def _build_weights(self, profile):
        confidence = float(profile.get('confidence', 0.0))
        if confidence < STATION_PROFILE_CONFIDENCE_LOW:
            return {family: 1.0 for family in FAMILIES}

        source_ema = profile.get('source_share_ema', {})
        icy_generic_rate = float(profile.get('icy_generic_rate_ema', 0.0))
        api_available = float(profile.get('api_available_ema', 0.0)) >= 0.35
        mp_reliable = float(profile.get('mp_reliable_ema', 0.0)) >= 0.60

        weights = {}
        for family in FAMILIES:
            share = float(source_ema.get(family, 1.0 / 3.0))
            weight = 1.0 + ((share - (1.0 / 3.0)) * 0.45)

            if family == 'musicplayer' and not mp_reliable:
                weight -= 0.15
            if family == 'api' and not api_available:
                weight -= 0.12
            if family == 'icy' and icy_generic_rate > 0.45:
                weight -= 0.10

            weights[family] = round(_clamp(weight, 0.70, 1.30), 3)
        return weights

    def get_policy_profile(self, station_key):
        profile = self.get_profile(station_key)
        if not isinstance(profile, dict):
            return {
                'confidence': 0.0,
                'preferred_family': '',
                'weights': {family: 1.0 for family in FAMILIES},
                'switch_margin': None,
                'single_confirm_polls': None,
                'mp_reliable': False,
                'icy_format': 'unknown',
                'icy_prefer_swapped': False,
                'icy_structural_generic': False,
                'mp_absent': False,
                'mp_noise': False,
            }

        confidence = float(profile.get('confidence', 0.0))
        preferred = ''
        if confidence >= STATION_PROFILE_CONFIDENCE_LOW:
            preferred = _normalize_family(profile.get('dominant_source'))

        switch_margin = None
        single_confirm_polls = None
        if confidence >= STATION_PROFILE_CONFIDENCE_HIGH:
            switch_margin = float(SOURCE_POLICY_SWITCH_MARGIN)
            if preferred:
                switch_margin += 0.08

            if preferred == 'musicplayer' and bool(profile.get('mp_reliable')):
                switch_margin += 0.05
            if preferred == 'api':
                lag = float(profile.get('api_lag_cycles', 0.0) or 0.0)
                switch_margin += min(0.06, lag * 0.02)
            if preferred == 'icy' and float(profile.get('icy_generic_rate', 0.0)) < 0.25:
                switch_margin += 0.04

            switch_margin = round(_clamp(switch_margin, 0.10, 0.35), 3)

            single_confirm_polls = int(SOURCE_POLICY_SINGLE_CONFIRM_POLLS)
            if preferred == 'api':
                lag = float(profile.get('api_lag_cycles', 0.0) or 0.0)
                if lag > 0:
                    single_confirm_polls = max(single_confirm_polls, min(5, int(round(lag))))

        role_flags = self._derive_source_role_flags(profile)
        prefer_swapped = {family: False for family in SOURCE_STATS_FAMILIES}
        prefer_swapped['icy'] = bool(profile.get('icy_prefer_swapped', False))

        result = {
            'confidence': round(confidence, 3),
            'preferred_family': preferred,
            'weights': self._build_weights(profile),
            'switch_margin': switch_margin,
            'single_confirm_polls': single_confirm_polls,
            'mp_reliable': bool(profile.get('mp_reliable', False)),
            'icy_format': str(profile.get('icy_format', 'unknown') or 'unknown'),
            'icy_prefer_swapped': bool(profile.get('icy_prefer_swapped', False)),
            'prefer_swapped': prefer_swapped,
            'icy_structural_generic': bool(role_flags.get('icy_structural_generic')),
            'mp_absent': bool(role_flags.get('mp_absent')),
            'mp_noise': bool(role_flags.get('mp_noise')),
        }
        return self._apply_source_group_db_hints(station_key, result)

    def _apply_source_group_db_hints(self, station_key, policy):
        data = dict(policy or {})
        data['source_group_db_hint'] = {'applied': False}
        data['icy_prefer_swapped_early'] = False
        prefer_swapped_early = {
            family: False for family in SOURCE_STATS_FAMILIES
        }
        try:
            family_stats = self._song_db.get_source_family_stats(station_key)
        except Exception:
            family_stats = {}
        if not isinstance(family_stats, dict) or not family_stats:
            data['prefer_swapped_early'] = prefer_swapped_early
            return data

        icy_wins = 0
        icy_swapped_wins = 0
        for family in SOURCE_STATS_FAMILIES:
            row = family_stats.get(family) or {}
            wins = int(row.get('wins', 0) or 0)
            swapped_wins = int(row.get('swapped_wins', 0) or 0)
            if family == 'icy':
                icy_wins = wins
                icy_swapped_wins = swapped_wins
            if wins >= int(SOURCE_GROUP_DB_SWAP_MIN_SAMPLES):
                swap_share = float(swapped_wins) / float(max(1, wins))
                if swap_share >= float(SOURCE_GROUP_DB_SWAP_MIN_SHARE):
                    prefer_swapped_early[family] = True
        data['prefer_swapped_early'] = prefer_swapped_early
        data['icy_prefer_swapped_early'] = bool(prefer_swapped_early.get('icy', False))

        group_wins = {}
        total_wins = 0
        for family in SOURCE_GROUP_FAMILIES:
            wins = int((family_stats.get(family) or {}).get('wins', 0) or 0)
            if wins <= 0:
                continue
            group_wins[family] = wins
            total_wins += wins
        if total_wins < int(SOURCE_GROUP_DB_MIN_SAMPLES):
            return data

        top_family = max(group_wins.keys(), key=lambda fam: int(group_wins.get(fam, 0)))
        top_share = float(group_wins.get(top_family, 0)) / float(total_wins or 1)
        if top_share < float(SOURCE_GROUP_DB_MIN_SHARE):
            return data

        confidence = float(data.get('confidence', 0.0) or 0.0)
        preferred = str(data.get('preferred_family', '') or '')
        if confidence < float(STATION_PROFILE_CONFIDENCE_LOW) and preferred not in SOURCE_GROUP_FAMILIES:
            data['preferred_family'] = top_family
            weights = dict(data.get('weights') or {})
            for family in SOURCE_GROUP_FAMILIES:
                base_weight = float(weights.get(family, 1.0) or 1.0)
                if family == top_family:
                    base_weight += 0.10
                else:
                    base_weight -= 0.05
                weights[family] = round(_clamp(base_weight, 0.70, 1.30), 3)
            data['weights'] = weights
            data['source_group_db_hint'] = {
                'applied': True,
                'family': top_family,
                'share': round(top_share, 3),
                'samples': int(total_wins),
                'icy_swapped_share': round(float(icy_swapped_wins) / float(max(1, icy_wins)), 3) if icy_wins > 0 else 0.0,
            }
        return data

    def get_generic_keywords(self, station_key):
        return self._song_db.get_generic_strings(station_key)

    def record_keyword_candidates(self, station_key, candidates):
        self._song_db.record_string_candidates(station_key, candidates)

    def record_confirmed_song(self, station_key, artist, title):
        self._song_db.record_song(station_key, artist, title)

    def record_source_family_hit(self, station_key, source_family, swapped=False):
        return self._song_db.record_source_family_hit(station_key, source_family, swapped=swapped)

    def get_source_family_stats(self, station_key):
        return self._song_db.get_source_family_stats(station_key)

    def get_qf_degraded_state(self, station_key):
        profile = self.get_profile(station_key)
        if not isinstance(profile, dict):
            return {}
        return {
            'qf_degraded': bool(profile.get('qf_degraded', False)),
            'degraded_at_ts': int(profile.get('degraded_at_ts', 0) or 0),
            'degrade_reason': str(profile.get('degrade_reason', '') or ''),
        }

    def set_qf_degraded_state(self, station_key, degraded=False, reason=''):
        if not station_key:
            return False
        key = str(station_key or '')
        profile = self._ensure_profile(key)
        profile['qf_degraded'] = bool(degraded)
        profile['degraded_at_ts'] = int(time.time()) if degraded else 0
        profile['degrade_reason'] = str(reason or '') if degraded else ''
        self._dirty_keys.add(key)
        return True

    def get_known_songs(self, station_key):
        return self._song_db.get_known_songs(station_key)

    def record_verified_source(
        self,
        station_key,
        source_url,
        station_name='',
        source_kind='stream',
        verified_by='',
        confidence=1.0,
        meta=None,
    ):
        return self._song_db.record_verified_source(
            station_key=station_key,
            source_url=source_url,
            station_name=station_name,
            source_kind=source_kind,
            verified_by=verified_by,
            confidence=confidence,
            meta=meta,
        )

    def get_verified_source_by_url(self, source_url):
        return self._song_db.get_verified_source_by_url(source_url)

    def get_verified_sources_for_station(self, station_key='', station_name='', limit=50):
        return self._song_db.get_verified_sources_for_station(
            station_key=station_key,
            station_name=station_name,
            limit=limit,
        )

    def flush_if_due(self, min_interval_s=30.0):
        now_ts = time.time()
        if (now_ts - self._last_flush_ts) < float(min_interval_s):
            return
        self.flush()

    def flush(self):
        if not self._dirty_keys or not self.profile_dir:
            return
        try:
            self._prepare_storage_dir()
            for station_key in list(self._dirty_keys):
                profile = self._profiles.get(station_key)
                if not isinstance(profile, dict):
                    continue
                file_path = self._station_file_path(station_key)
                if not file_path:
                    continue
                clean = {k: v for k, v in profile.items() if k not in ('keyword_stats', 'generic_keywords', 'song_cache')}
                payload = {
                    'version': 2,
                    'station_key': station_key,
                    'profile': clean,
                }
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            self._dirty_keys.clear()
            self._last_flush_ts = time.time()
        except Exception:
            pass

    def close(self, flush=True):
        try:
            if flush:
                self.flush()
        except Exception:
            pass
        try:
            if self._song_db is not None:
                self._song_db.close()
        except Exception:
            pass
