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
        self.switch_margin = float(switch_margin)
        self.single_confirm_polls = int(single_confirm_polls)
        self._state = {
            family: SourceHealth(window=self.window)
            for family in self.FAMILIES
        }
        self._pending_source = ''
        self._pending_pair = ('', '')
        self._pending_count = 0

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

    def _observe_pair(self, family, pair, station_name, last_winner_pair):
        st = self._state.get(family)
        if st is None:
            return
        is_valid = self._valid_pair(pair)
        is_generic = self._contains_station(pair, station_name)
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

    def _score(self, family):
        st = self._state.get(family)
        if st is None:
            return -1.0
        valid = st.valid_rate()
        generic = st.generic_rate()
        churn = st.churn_rate()
        agree = st.agreement_rate()
        lead = st.lead_error_rate()
        empty = 1.0 - valid
        # Positive: valide + konsistent mit finalen Entscheidungen.
        # Negative: generische Sendertexte, Flattern, nachweislich vorauslaufende Wechsel.
        return (
            0.38 * valid
            + 0.34 * agree
            - 0.26 * generic
            - 0.20 * churn
            - 0.30 * lead
            - 0.20 * empty
        )

    def _preferred_family(self, valid_pairs, last_winner_family):
        if not valid_pairs:
            return ''
        ranked = sorted(
            ((family, self._score(family)) for family in valid_pairs.keys()),
            key=lambda item: item[1],
            reverse=True
        )
        best_family, best_score = ranked[0]
        if last_winner_family in valid_pairs:
            current_score = self._score(last_winner_family)
            if best_family != last_winner_family and (best_score - current_score) < self.switch_margin:
                return last_winner_family
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
        reasons
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

        if not winner_family:
            self._reset_confirm()
            return (stream_title_changed or initial_source_pending), reasons['title'], preferred

        # Aktive Quelle hat einen echten Wechsel gemeldet.
        active_pair = pairs.get(winner_family, ('', ''))
        if self._valid_pair(active_pair) and active_pair != last_winner_pair:
            if winner_family == 'api':
                mp_pair = pairs['musicplayer']
                icy_pair = pairs['icy']
                mp_conflict = self._valid_pair(mp_pair) and mp_pair != active_pair
                icy_conflict = self._valid_pair(icy_pair) and icy_pair != active_pair
                if mp_conflict and icy_conflict:
                    self.mark_lead_error('api')
                    self._reset_confirm()
                    return False, reasons['api'], preferred
            required = self.single_confirm_polls if len(valid_pairs) == 1 else 1
            if self._confirm(winner_family, active_pair, required):
                return True, reasons[winner_family], preferred
            return False, reasons[winner_family], preferred

        # API stale: API bleibt stehen, ICY zeigt neuen Song.
        if (
            winner_family == 'api'
            and stream_title_changed
            and self._valid_pair(pairs['icy'])
            and pairs['icy'] != last_winner_pair
            and not self._contains_station(pairs['icy'], station_name)
        ):
            if preferred == 'icy' or (preferred == '' and not self._valid_pair(pairs['api'])):
                if self._confirm('icy', pairs['icy'], 1):
                    return True, reasons['icy_stale'], preferred

        if (
            winner_family == 'musicplayer'
            and last_winner_pair
            and last_winner_pair[0]
            and last_winner_pair[1]
            and not self._valid_pair(active_pair)
        ):
            self._reset_confirm()
            return True, reasons['mp_invalid'], preferred

        # Aktive Quelle liefert nichts mehr: zur besten verfÃƒÂ¼gbaren Quelle wechseln.
        if not self._valid_pair(active_pair) and preferred:
            target_pair = valid_pairs.get(preferred, ('', ''))
            if self._valid_pair(target_pair) and target_pair != last_winner_pair:
                required = self.single_confirm_polls if len(valid_pairs) == 1 else 1
                if self._confirm(preferred, target_pair, required):
                    return True, reasons.get(preferred, reasons['title']), preferred
                return False, reasons.get(preferred, reasons['title']), preferred

        self._reset_confirm()
        return False, reasons.get(winner_family, reasons['title']), preferred

    def debug_scores(self):
        return {
            family: round(self._score(family), 3)
            for family in self.FAMILIES
        }


