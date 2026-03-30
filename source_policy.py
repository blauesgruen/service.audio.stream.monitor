from collections import deque


class SourceHealth:
    def __init__(self, window):
        self.window = int(window)
        self.valid = deque(maxlen=self.window)
        self.generic = deque(maxlen=self.window)
        self.changed = deque(maxlen=self.window)
        self.agree = deque(maxlen=self.window)
        self.lead_error = deque(maxlen=self.window)
        self.last_pair = ('', '')

    @staticmethod
    def _rate(values, default=0.0):
        if not values:
            return float(default)
        return float(sum(values)) / float(len(values))

    def valid_rate(self):
        return self._rate(self.valid, 0.0)

    def generic_rate(self):
        return self._rate(self.generic, 0.0)

    def churn_rate(self):
        return self._rate(self.changed, 0.0)

    def agreement_rate(self):
        return self._rate(self.agree, 0.5)

    def lead_error_rate(self):
        return self._rate(self.lead_error, 0.0)


class SourcePolicy:
    FAMILIES = ('musicplayer', 'api', 'icy')

    def __init__(self, window=40, switch_margin=0.12, single_confirm_polls=2):
        self.window = int(window)
        self.base_switch_margin = float(switch_margin)
        self.base_single_confirm_polls = int(single_confirm_polls)
        # Keep legacy names as aliases for existing call sites.
        self.switch_margin = self.base_switch_margin
        self.single_confirm_polls = self.base_single_confirm_polls

        self._learned_weights = {family: 1.0 for family in self.FAMILIES}
        self._state = {family: SourceHealth(window=self.window) for family in self.FAMILIES}

        self._profile_confidence = 0.0
        self._profile_preferred_family = ''
        self._profile_switch_margin = None
        self._profile_single_confirm_polls = None
        self._icy_structural_generic = False
        self._mp_absent = False
        self._mp_noise = False

        self._pending_source = ''
        self._pending_pair = ('', '')
        self._pending_count = 0
        self._last_observation = {}
        self._generic_keywords = []
        self._known_songs = frozenset()

    @staticmethod
    def _valid_pair(pair):
        return bool(pair and pair[0] and pair[1])

    @staticmethod
    def _contains_station(pair, station_name):
        if not SourcePolicy._valid_pair(pair):
            return False
        station_l = (station_name or '').strip().lower()
        if not station_l:
            return False
        a_l = str(pair[0] or '').strip().lower()
        t_l = str(pair[1] or '').strip().lower()
        return station_l in a_l or station_l in t_l

    def set_generic_keywords(self, keywords):
        """Setzt senderspezifische Keywords für die generische Pair-Erkennung."""
        self._generic_keywords = [
            str(k).strip().lower()
            for k in (keywords or [])
            if str(k).strip()
        ]

    def set_known_songs(self, songs):
        """Setzt den bekannten Song-Cache des Senders (frozenset von (artist, title)-Tuples)."""
        self._known_songs = frozenset(
            (str(a).strip().lower(), str(t).strip().lower())
            for a, t in (songs or [])
            if a and t
        )

    def _is_known_song(self, pair):
        """Prüft ob ein Pair im bestätigten Song-Cache des Senders liegt."""
        if not self._known_songs or not self._valid_pair(pair):
            return False
        return (str(pair[0]).strip().lower(), str(pair[1]).strip().lower()) in self._known_songs

    def _is_keyword_generic(self, pair):
        """Prüft, ob ein Pair einen bekannten generischen Sender-String enthält."""
        if not self._generic_keywords or not self._valid_pair(pair):
            return False
        text = f"{pair[0]} {pair[1]}".lower()
        return any(kw in text for kw in self._generic_keywords)

    def _is_generic_pair(self, pair, station_name):
        """Zentraler Generic-Check: bekannter Song schlägt alle Generic-Checks."""
        if self._is_known_song(pair):
            return False
        return self._contains_station(pair, station_name) or self._is_keyword_generic(pair)

    def _active_switch_margin(self):
        if self._profile_switch_margin is not None and self._profile_confidence >= 0.60:
            return float(self._profile_switch_margin)
        return float(self.base_switch_margin)

    def _active_confirm_polls(self):
        if self._profile_single_confirm_polls is not None and self._profile_confidence >= 0.60:
            return max(1, int(self._profile_single_confirm_polls))
        return max(1, int(self.base_single_confirm_polls))

    def _observe_pair(self, family, pair, station_name, last_winner_pair):
        st = self._state.get(family)
        if st is None:
            return
        is_valid = self._valid_pair(pair)
        is_generic = self._is_generic_pair(pair, station_name)
        has_changed = bool(is_valid and st.last_pair[0] and st.last_pair[1] and pair != st.last_pair)
        st.valid.append(1 if is_valid else 0)
        st.generic.append(1 if is_generic else 0)
        st.changed.append(1 if has_changed else 0)
        if is_valid and last_winner_pair and last_winner_pair[0] and last_winner_pair[1]:
            st.agree.append(1 if pair == last_winner_pair else 0)
        if is_valid:
            st.last_pair = pair

    def mark_lead_error(self, family):
        st = self._state.get(family)
        if st is not None:
            st.lead_error.append(1)

    def _base_score(self, family):
        st = self._state.get(family)
        if st is None:
            return -1.0
        valid = st.valid_rate()
        generic = st.generic_rate()
        churn = st.churn_rate()
        agree = st.agreement_rate()
        lead = st.lead_error_rate()
        empty = 1.0 - valid
        return (
            0.38 * valid
            + 0.34 * agree
            - 0.26 * generic
            - 0.20 * churn
            - 0.30 * lead
            - 0.20 * empty
        )

    def _score(self, family):
        base = self._base_score(family)
        weight = float(self._learned_weights.get(family, 1.0))
        return base * weight

    def set_learned_weights(self, weights):
        safe = {}
        for family in self.FAMILIES:
            value = 1.0
            try:
                value = float((weights or {}).get(family, 1.0))
            except Exception:
                value = 1.0
            if value < 0.7:
                value = 0.7
            if value > 1.3:
                value = 1.3
            safe[family] = value
        self._learned_weights = safe

    def apply_station_profile(self, profile):
        data = profile or {}
        confidence = 0.0
        try:
            confidence = float(data.get('confidence', 0.0))
        except Exception:
            confidence = 0.0
        self._profile_confidence = max(0.0, min(1.0, confidence))

        preferred = str(data.get('preferred_family', '') or '').strip().lower()
        self._profile_preferred_family = preferred if preferred in self.FAMILIES else ''

        switch_margin = data.get('switch_margin')
        if switch_margin is None:
            self._profile_switch_margin = None
        else:
            try:
                self._profile_switch_margin = float(switch_margin)
            except Exception:
                self._profile_switch_margin = None

        single_confirm = data.get('single_confirm_polls')
        if single_confirm is None:
            self._profile_single_confirm_polls = None
        else:
            try:
                self._profile_single_confirm_polls = max(1, int(single_confirm))
            except Exception:
                self._profile_single_confirm_polls = None

        self._icy_structural_generic = bool(data.get('icy_structural_generic', False))
        self._mp_absent = bool(data.get('mp_absent', False))
        self._mp_noise = bool(data.get('mp_noise', False))

        self.set_learned_weights(data.get('weights') or {})

    def clear_station_profile(self):
        self._profile_confidence = 0.0
        self._profile_preferred_family = ''
        self._profile_switch_margin = None
        self._profile_single_confirm_polls = None
        self._icy_structural_generic = False
        self._mp_absent = False
        self._mp_noise = False
        self.set_learned_weights({})
        self.set_generic_keywords([])
        self.set_known_songs(frozenset())

    def _mp_unusable(self):
        return bool(self._mp_absent or self._mp_noise)

    def _api_reliable_comparator_conflicts(self, api_pair, mp_pair, icy_pair):
        """
        Liefert die Anzahl valider Vergleichsquellen und wie viele davon API widersprechen.
        Quellen, die der Senderprofil-Analyse nach strukturell unbrauchbar sind, werden ignoriert.
        """
        comparator_count = 0
        conflict_count = 0

        mp_reliable_comparator = not self._mp_unusable()
        icy_reliable_comparator = not self._icy_structural_generic

        if mp_reliable_comparator and self._valid_pair(mp_pair):
            comparator_count += 1
            if mp_pair != api_pair:
                conflict_count += 1

        if icy_reliable_comparator and self._valid_pair(icy_pair):
            comparator_count += 1
            if icy_pair != api_pair:
                conflict_count += 1

        return comparator_count, conflict_count

    def _preferred_family(self, valid_pairs, last_winner_family):
        if not valid_pairs:
            return ''

        # MP hat Prioritaet, sobald valide Songdaten vorliegen.
        # Ausnahme: Senderprofil markiert MP als strukturell unbrauchbar (absent/noise).
        if (
            'musicplayer' in valid_pairs
            and self._valid_pair(valid_pairs.get('musicplayer'))
            and not self._mp_unusable()
        ):
            return 'musicplayer'

        ranked = sorted(
            ((family, self._score(family)) for family in valid_pairs.keys()),
            key=lambda item: item[1],
            reverse=True,
        )
        best_family, best_score = ranked[0]
        margin = self._active_switch_margin()

        if last_winner_family in valid_pairs:
            current_score = self._score(last_winner_family)
            if best_family != last_winner_family and (best_score - current_score) < margin:
                return last_winner_family

        # For medium/high confidence profiles, allow dominant-source tie-break.
        preferred = self._profile_preferred_family
        if self._profile_confidence >= 0.20 and preferred in valid_pairs:
            preferred_score = self._score(preferred)
            if best_family != preferred and (best_score - preferred_score) <= (margin * 0.80):
                return preferred

        return best_family

    def _confirm(self, family, pair, required):
        required_count = max(1, int(required))
        if family != self._pending_source or pair != self._pending_pair:
            self._pending_source = family
            self._pending_pair = pair
            self._pending_count = 1
        else:
            self._pending_count += 1
        return self._pending_count >= required_count

    def _reset_confirm(self):
        self._pending_source = ''
        self._pending_pair = ('', '')
        self._pending_count = 0

    @staticmethod
    def _source_family(source):
        s = str(source or '')
        if s.startswith('musicplayer'):
            return 'musicplayer'
        if s.startswith('api'):
            return 'api'
        if s.startswith('icy'):
            return 'icy'
        return ''

    def _classify_source_state(self, pair, station_name):
        if not self._valid_pair(pair):
            return 'empty'
        if self._is_generic_pair(pair, station_name):
            return 'generic'
        return 'song'

    def _build_observation(
        self,
        winner_family,
        last_winner_pair,
        pairs,
        station_name,
        preferred,
        changed,
        reason,
    ):
        sources = {}
        current_valid = bool(last_winner_pair and last_winner_pair[0] and last_winner_pair[1])
        for family in self.FAMILIES:
            pair = pairs.get(family, ('', ''))
            state = self._classify_source_state(pair, station_name)
            match_current = bool(state == 'song' and current_valid and pair == last_winner_pair)
            other_song = bool(state == 'song' and current_valid and pair != last_winner_pair)
            sources[family] = {
                'state': state,
                'match_current': match_current,
                'other_song': other_song,
            }
        return {
            'winner_family': winner_family,
            'preferred_family': preferred,
            'changed': bool(changed),
            'reason': reason or '',
            'sources': sources,
        }

    def decide_trigger(
        self,
        last_winner_source,
        last_winner_pair,
        current_mp_pair,
        current_api_pair,
        current_icy_pair,
        station_name,
        stream_title_changed,
        initial_source_pending,
        reasons,
    ):
        winner_family = self._source_family(last_winner_source)
        pairs = {
            'musicplayer': current_mp_pair if self._valid_pair(current_mp_pair) else ('', ''),
            'api': current_api_pair if self._valid_pair(current_api_pair) else ('', ''),
            'icy': current_icy_pair if self._valid_pair(current_icy_pair) else ('', ''),
        }
        for family, pair in pairs.items():
            self._observe_pair(family, pair, station_name, last_winner_pair)

        valid_pairs = {family: pair for family, pair in pairs.items() if self._valid_pair(pair)}
        preferred = self._preferred_family(valid_pairs, winner_family)
        default_reason = reasons.get(winner_family, reasons['title'])
        confirm_polls = self._active_confirm_polls()

        def _finish(changed, reason):
            final_reason = reason or default_reason
            self._last_observation = self._build_observation(
                winner_family=winner_family,
                last_winner_pair=last_winner_pair,
                pairs=pairs,
                station_name=station_name,
                preferred=preferred,
                changed=changed,
                reason=final_reason,
            )
            return changed, final_reason, preferred

        if not winner_family:
            self._reset_confirm()
            return _finish((stream_title_changed or initial_source_pending), reasons['title'])

        active_pair = pairs.get(winner_family, ('', ''))
        api_pair = pairs.get('api', ('', ''))
        icy_pair = pairs.get('icy', ('', ''))
        mp_pair = pairs.get('musicplayer', ('', ''))
        external_support_last = (
            (self._valid_pair(api_pair) and api_pair == last_winner_pair)
            or (self._valid_pair(icy_pair) and icy_pair == last_winner_pair)
        )

        # MP-Prioritaet: wenn MP einen neuen, validen Song meldet, wird dieser
        # bevorzugt uebernommen (sofern MP nicht generisch ist).
        if (
            winner_family != 'musicplayer'
            and self._valid_pair(mp_pair)
            and mp_pair != last_winner_pair
            and not self._is_generic_pair(mp_pair, station_name)
        ):
            required = confirm_polls if len(valid_pairs) == 1 else 1
            if self._confirm('musicplayer_priority', mp_pair, required):
                return _finish(True, reasons['musicplayer'])
            return _finish(False, reasons['musicplayer'])

        # Active source reports a real track change.
        if self._valid_pair(active_pair) and active_pair != last_winner_pair:
            if winner_family == 'api':
                comparator_count, conflict_count = self._api_reliable_comparator_conflicts(
                    active_pair,
                    mp_pair,
                    icy_pair
                )
                if comparator_count > 0 and conflict_count == comparator_count:
                    self.mark_lead_error('api')
                    self._reset_confirm()
                    return _finish(False, reasons['api'])

            if winner_family == 'musicplayer':
                mp_generic = self._is_generic_pair(active_pair, station_name)
                if mp_generic and external_support_last:
                    self._reset_confirm()
                    return _finish(False, reasons['musicplayer'])
                if mp_generic:
                    required = max(3, confirm_polls + 1)
                    if self._confirm('musicplayer_noise', active_pair, required):
                        return _finish(True, reasons['mp_invalid'])
                    return _finish(False, reasons['musicplayer'])
                required = 2 if external_support_last else (confirm_polls if len(valid_pairs) == 1 else 1)
                if self._confirm(winner_family, active_pair, required):
                    return _finish(True, reasons[winner_family])
                return _finish(False, reasons[winner_family])

            required = confirm_polls if len(valid_pairs) == 1 else 1
            if self._confirm(winner_family, active_pair, required):
                return _finish(True, reasons[winner_family])
            return _finish(False, reasons[winner_family])

        # API stale: API keeps old track while ICY has a valid new song.
        if (
            winner_family == 'api'
            and stream_title_changed
            and self._valid_pair(pairs['icy'])
            and pairs['icy'] != last_winner_pair
            and not self._is_generic_pair(pairs['icy'], station_name)
        ):
            api_is_stale = self._valid_pair(api_pair) and api_pair == last_winner_pair
            if (
                preferred == 'icy'
                or (preferred == '' and not self._valid_pair(api_pair))
                or api_is_stale
            ):
                if self._confirm('icy', pairs['icy'], 1):
                    return _finish(True, reasons['icy_stale'])

        if (
            winner_family == 'musicplayer'
            and last_winner_pair
            and last_winner_pair[0]
            and last_winner_pair[1]
            and not self._valid_pair(active_pair)
        ):
            if external_support_last:
                self._reset_confirm()
                return _finish(False, reasons['musicplayer'])
            required = max(3, confirm_polls + 1)
            if self._confirm('musicplayer_invalid', ('', ''), required):
                return _finish(True, reasons['mp_invalid'])
            return _finish(False, reasons['musicplayer'])

        # Active source has no data: move to preferred valid source.
        if not self._valid_pair(active_pair) and preferred:
            target_pair = valid_pairs.get(preferred, ('', ''))
            if self._valid_pair(target_pair) and target_pair != last_winner_pair:
                required = confirm_polls if len(valid_pairs) == 1 else 1
                if self._confirm(preferred, target_pair, required):
                    return _finish(True, reasons.get(preferred, reasons['title']))
                return _finish(False, reasons.get(preferred, reasons['title']))

        self._reset_confirm()
        return _finish(False, default_reason)

    def debug_scores(self):
        return {family: round(self._score(family), 3) for family in self.FAMILIES}

    def learning_scores(self):
        return {family: float(self._base_score(family)) for family in self.FAMILIES}

    def latest_observation(self):
        return dict(self._last_observation or {})
