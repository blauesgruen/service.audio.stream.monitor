"""
Central analysis event buffering and persistence.
"""
import json
import os
import time
import uuid
from collections import deque


def new_trace_id():
    return uuid.uuid4().hex[:12]


class AnalysisEventStore:
    def __init__(
        self,
        base_dir,
        filename,
        max_events=1500,
        flush_interval_s=5.0,
        log_debug=None,
    ):
        self.base_dir = base_dir or ''
        self.filename = filename or 'analysis_events.jsonl'
        self.max_events = max(10, int(max_events))
        self.flush_interval_s = max(0.5, float(flush_interval_s))
        self.log_debug = log_debug
        self.events = deque(maxlen=self.max_events)
        self._dirty = False
        self._last_flush_ts = 0.0

        self.path = os.path.join(self.base_dir, self.filename) if self.base_dir else self.filename
        self._ensure_dir()
        self._load_existing()

    def _ensure_dir(self):
        if not self.base_dir:
            return
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except Exception as e:
            if self.log_debug:
                self.log_debug(f"AnalysisEventStore mkdir fehlgeschlagen: {e}")

    def _load_existing(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    self.events.append(obj)
        except Exception as e:
            if self.log_debug:
                self.log_debug(f"AnalysisEventStore load fehlgeschlagen: {e}")

    def add_event(self, event):
        if not isinstance(event, dict):
            return
        self.events.append(event)
        self._dirty = True
        now = time.time()
        if now - self._last_flush_ts >= self.flush_interval_s:
            self.flush()

    def flush(self):
        if not self._dirty:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                for event in self.events:
                    fh.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')))
                    fh.write('\n')
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_flush_ts = time.time()
        except Exception as e:
            if self.log_debug:
                self.log_debug(f"AnalysisEventStore flush fehlgeschlagen: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def close(self):
        self.flush()

