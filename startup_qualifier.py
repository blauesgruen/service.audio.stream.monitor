class StartupQualifier:
    """
    Tracks startup/source-qualification state independent from Kodi runtime APIs.
    """

    def __init__(self, has_non_generic_song_pair, get_station_profile_hints, api_only_stable_polls=3):
        self._has_non_generic_song_pair = has_non_generic_song_pair
        self._get_station_profile_hints = get_station_profile_hints
        self._api_only_stable_polls = max(1, int(api_only_stable_polls))
        self.reset_session()

    def reset_session(self):
        self._session_icy_song_seen = False
        self._session_api_stable_pair = ('', '')
        self._session_api_stable_polls = 0

    def update_session_characteristics(self, current_api_pair, current_icy_pair, station_name=''):
        if self._has_non_generic_song_pair(current_icy_pair, station_name):
            self._session_icy_song_seen = True

        if self._has_non_generic_song_pair(current_api_pair, station_name):
            api_pair = (current_api_pair[0], current_api_pair[1])
            if api_pair == self._session_api_stable_pair:
                self._session_api_stable_polls += 1
            else:
                self._session_api_stable_pair = api_pair
                self._session_api_stable_polls = 1
            return

        self._session_api_stable_pair = ('', '')
        self._session_api_stable_polls = 0

    def session_api_only_ready(self, current_mp_pair, current_api_pair, current_icy_pair, station_name=''):
        if self._has_non_generic_song_pair(current_mp_pair, station_name):
            return False
        if self._has_non_generic_song_pair(current_icy_pair, station_name):
            return False
        if self._session_icy_song_seen:
            return False
        if not self._has_non_generic_song_pair(current_api_pair, station_name):
            return False
        return self._session_api_stable_polls >= self._api_only_stable_polls

    def profile_api_only_ready(self, station_name, current_api_pair):
        hints = self._get_station_profile_hints(station_name)
        confidence = float(hints.get('confidence', 0.0) or 0.0)
        if confidence < 0.20:
            return False
        if not bool(hints.get('icy_structural_generic', False)):
            return False
        if not (bool(hints.get('mp_noise', False)) or bool(hints.get('mp_absent', False))):
            return False
        return self._has_non_generic_song_pair(current_api_pair, station_name)

    def should_bypass_initial_program_block(
        self,
        station_name,
        current_mp_pair,
        current_api_pair,
        current_icy_pair
    ):
        if self.profile_api_only_ready(station_name, current_api_pair):
            return True
        return self.session_api_only_ready(
            current_mp_pair,
            current_api_pair,
            current_icy_pair,
            station_name
        )

    def has_startup_source_consensus(self, current_mp_pair, current_api_pair, current_icy_pair, station_name=''):
        if self.profile_api_only_ready(station_name, current_api_pair):
            return True
        if self.session_api_only_ready(current_mp_pair, current_api_pair, current_icy_pair, station_name):
            return True

        pairs = []
        for pair in (current_mp_pair, current_api_pair, current_icy_pair):
            if self._has_non_generic_song_pair(pair, station_name):
                pairs.append((pair[0], pair[1]))
        if len(pairs) < 2:
            return False

        counts = {}
        for pair in pairs:
            counts[pair] = int(counts.get(pair, 0)) + 1
            if counts[pair] >= 2:
                return True
        return False
