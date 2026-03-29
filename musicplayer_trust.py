class MusicPlayerTrust:
    """
    Encapsulates trust state and trust transitions for MusicPlayer metadata.
    """

    def __init__(self, max_mismatches=2, log_info=None, log_debug=None, log_warning=None):
        self.max_mismatches = max(1, int(max_mismatches))
        self._log_info = log_info
        self._log_debug = log_debug
        self._log_warning = log_warning
        self._trusted = False
        self._mismatch_count = 0
        self._trust_generation = 0

    def reset(self, generation, reason=''):
        was_trusted = bool(self._trusted)
        self._trusted = False
        self._mismatch_count = 0
        self._trust_generation = int(generation)
        if reason and was_trusted and callable(self._log_debug):
            self._log_debug(f"MusicPlayer-Trust zurueckgesetzt: {reason}")

    def is_trusted(self, generation):
        return bool(self._trusted and self._trust_generation == int(generation))

    def mark_trusted(self, generation, reason=''):
        was_trusted = self.is_trusted(generation)
        self._trusted = True
        self._trust_generation = int(generation)
        self._mismatch_count = 0
        if not was_trusted and callable(self._log_info):
            suffix = f" ({reason})" if reason else ""
            self._log_info(f"MusicPlayer als Songquelle verifiziert{suffix}")

    def reset_mismatch_if_trusted(self, generation):
        if self.is_trusted(generation):
            self._mismatch_count = 0

    def register_mismatch(self, generation, reason=''):
        if not self.is_trusted(generation):
            return
        self._mismatch_count += 1
        if callable(self._log_warning):
            self._log_warning(
                f"MusicPlayer-Widerspruch ({self._mismatch_count}/{self.max_mismatches})"
                f"{': ' + reason if reason else ''}"
            )
        if self._mismatch_count >= self.max_mismatches:
            self.reset(generation, 'zu viele Widersprueche')

    def update_after_decision(self, generation, decision_source, decision_pair, mp_pairs):
        if not mp_pairs:
            return
        source = str(decision_source or '')
        pair = decision_pair if decision_pair and decision_pair[0] and decision_pair[1] else None
        mp_set = set(mp_pairs)

        if source.startswith('musicplayer'):
            self.reset_mismatch_if_trusted(generation)
            return

        if (source.startswith('api') or source.startswith('icy')) and pair and pair in mp_set:
            self.mark_trusted(generation, f"Konsens mit {source}: '{pair[0]} - {pair[1]}'")
