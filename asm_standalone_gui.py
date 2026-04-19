#!/usr/bin/env python3
"""
Standalone GUI for Audio Stream Monitor (ASM) without Kodi runtime.

This tool reuses existing ASM modules by providing lightweight Kodi API stubs
(`xbmc`, `xbmcgui`, `xbmcaddon`) before importing `service.py`.

Features:
- Start/stop monitoring of an HTTP/HTTPS audio stream URL
- radio.de station search with selectable results
- live view of RadioMonitor/RadioDE window properties
- integrated log panel (captures xbmc.log output via stub)

Note:
- This mirrors ASM runtime logic as closely as possible outside Kodi,
  but cannot fully emulate Kodi's internal player behavior.
"""

import argparse
import os
import queue
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


# -----------------------------
# Kodi stub installation
# -----------------------------


class _InMemoryWindow:
    def __init__(self) -> None:
        self._props: Dict[str, str] = {}
        self._lock = threading.RLock()

    def getProperty(self, key: str) -> str:
        with self._lock:
            return self._props.get(str(key), "")

    def setProperty(self, key: str, value: Any) -> None:
        with self._lock:
            self._props[str(key)] = str(value)

    def clearProperty(self, key: str) -> None:
        with self._lock:
            self._props.pop(str(key), None)

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._props)


def install_kodi_stubs(log_sink: "queue.Queue[str]") -> None:
    import types

    if (
        "xbmc" in sys.modules
        and "xbmcgui" in sys.modules
        and "xbmcaddon" in sys.modules
        and "xbmcvfs" in sys.modules
    ):
        return

    windows: Dict[int, _InMemoryWindow] = {10000: _InMemoryWindow()}

    def get_window(win_id: int) -> _InMemoryWindow:
        key = int(win_id)
        if key not in windows:
            windows[key] = _InMemoryWindow()
        return windows[key]

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3

    _log_level_map = {
        xbmc.LOGDEBUG: "DEBUG",
        xbmc.LOGINFO: "INFO",
        xbmc.LOGWARNING: "WARN",
        xbmc.LOGERROR: "ERROR",
    }

    class Monitor:
        def __init__(self) -> None:
            self._abort = False

        def abortRequested(self) -> bool:
            return self._abort

        def waitForAbort(self, timeout: float) -> bool:
            # Sleep in small chunks so caller behavior is closer to Kodi monitor loop.
            end_ts = time.time() + float(timeout)
            while time.time() < end_ts:
                if self._abort:
                    return True
                time.sleep(0.05)
            return self._abort

    class _DummyMusicInfoTag:
        def getArtist(self) -> str:
            return ""

        def getTitle(self) -> str:
            return ""

    class _DummyPlayingItem:
        def getLabel(self) -> str:
            return ""

        def getMusicInfoTag(self) -> _DummyMusicInfoTag:
            return _DummyMusicInfoTag()

    class Player:
        def __init__(self) -> None:
            self._playing_file = ""
            self._playing_audio = False
            self._playing_video = False

        def getPlayingFile(self) -> str:
            return self._playing_file

        def isPlayingAudio(self) -> bool:
            return self._playing_audio

        def isPlayingVideo(self) -> bool:
            return self._playing_video

        def getMusicInfoTag(self) -> _DummyMusicInfoTag:
            return _DummyMusicInfoTag()

        def getPlayingItem(self) -> _DummyPlayingItem:
            return _DummyPlayingItem()

    def xbmc_log(message: str, level: int = 0) -> None:
        level_text = _log_level_map.get(level, "INFO")
        line = f"[{level_text}] {message}"
        try:
            log_sink.put_nowait(line)
        except Exception:
            pass
        # Keep stdout output for CLI self-test visibility.
        print(line)

    def getCondVisibility(expr: str) -> bool:
        text = str(expr or "")
        if text.startswith("System.HasAddon("):
            return False
        if text == "System.GetBool(debug.showloginfo)":
            return True
        return False

    def executeJSONRPC(_: str) -> str:
        return "{}"

    def getInfoLabel(_: str) -> str:
        return ""

    def executebuiltin(command: str) -> None:
        xbmc_log(f"executebuiltin called: {command}", xbmc.LOGINFO)

    xbmc.log = xbmc_log
    xbmc.getCondVisibility = getCondVisibility
    xbmc.executeJSONRPC = executeJSONRPC
    xbmc.getInfoLabel = getInfoLabel
    xbmc.executebuiltin = executebuiltin
    xbmc.sleep = lambda ms: time.sleep(max(0.0, float(ms) / 1000.0))
    xbmc.Monitor = Monitor
    xbmc.Player = Player

    xbmcgui = types.ModuleType("xbmcgui")

    class Window:
        def __init__(self, win_id: int) -> None:
            self._impl = get_window(win_id)

        def getProperty(self, key: str) -> str:
            return self._impl.getProperty(key)

        def setProperty(self, key: str, value: Any) -> None:
            self._impl.setProperty(key, value)

        def clearProperty(self, key: str) -> None:
            self._impl.clearProperty(key)

    xbmcgui.Window = Window

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcvfs = types.ModuleType("xbmcvfs")

    class Addon:
        def __init__(self, id: str = "service.audio.stream.monitor") -> None:
            self._id = id
            profile_dir = os.environ.get(
                "ASM_STANDALONE_PROFILE",
                os.path.join(tempfile.gettempdir(), "asm_standalone_profile"),
            )
            self._info = {
                "id": "service.audio.stream.monitor",
                "name": "Audio Stream Monitor",
                "version": "1.1.5",
                "profile": profile_dir,
            }
            self._settings = {
                "bullet_enabled": "false",
                "bullet_color": "green",
                "persist_data": "true",
                "qf_enabled": "false",
                "debug_logging": "true",
            }

        def getAddonInfo(self, key: str) -> str:
            return str(self._info.get(key, ""))

        def getSetting(self, key: str) -> str:
            return str(self._settings.get(key, ""))

        def setSetting(self, key: str, value: str) -> None:
            self._settings[str(key)] = str(value)

        def getLocalizedString(self, string_id: int) -> str:
            return str(string_id)

    xbmcaddon.Addon = Addon
    xbmcvfs.translatePath = lambda path: str(path or "")

    sys.modules["xbmc"] = xbmc
    sys.modules["xbmcgui"] = xbmcgui
    sys.modules["xbmcaddon"] = xbmcaddon
    sys.modules["xbmcvfs"] = xbmcvfs


# -----------------------------
# Runtime wrapper
# -----------------------------


@dataclass
class RadioDeResult:
    name: str
    station_id: str
    stream_url: str
    logo: str
    api_rank: int = 0


class ASMRuntime:
    def __init__(self) -> None:
        # Import only after stubs are installed.
        import service  # noqa: WPS433
        from constants import PropertyNames as P  # noqa: WPS433

        self.service = service
        self.P = P
        self.monitor = service.RadioMonitor()
        self.window = service.WINDOW
        self._lock = threading.RLock()
        self._desired_stream_url = ""
        self._last_restart_ts = 0.0
        self._last_activity_ts = time.time()
        self._last_observed_state: Tuple[str, str, str, str] = ("", "", "", "")
        self._idle_restart_s = 10.0
        self._supervisor_stop = threading.Event()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="asm-standalone-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def _supervisor_loop(self) -> None:
        """
        Keeps monitoring alive similarly to Kodi's run()/check_playing loop.
        If the worker thread ends unexpectedly while a stream is still active,
        restart monitoring after a short debounce.
        """
        while not self._supervisor_stop.is_set():
            self._supervisor_stop.wait(2.0)
            if self._supervisor_stop.is_set():
                break

            with self._lock:
                stream_url = str(self._desired_stream_url or "").strip()
                if not stream_url:
                    continue

                worker = getattr(self.monitor, "metadata_thread", None)
                worker_alive = bool(worker and worker.is_alive())

                snap = self.property_snapshot()
                observed_state = (
                    str(snap.get(self.P.STREAM_TTL, "") or ""),
                    str(snap.get(self.P.ARTIST, "") or ""),
                    str(snap.get(self.P.TITLE, "") or ""),
                    str(snap.get(self.P.API_NOW, "") or ""),
                )
                if observed_state != self._last_observed_state:
                    self._last_observed_state = observed_state
                    self._last_activity_ts = time.time()

                if worker_alive:
                    # Recovery for streams that stay in empty-StreamTitle/no-song state:
                    # mimic manual re-start behavior automatically.
                    stream_ttl = (observed_state[0] or "").strip()
                    artist = (observed_state[1] or "").strip()
                    title = (observed_state[2] or "").strip()
                    idle_s = max(0.0, time.time() - float(self._last_activity_ts or 0.0))
                    if (
                        idle_s >= float(self._idle_restart_s)
                        and not stream_ttl
                        and not (artist and title)
                        and (time.time() - float(self._last_restart_ts or 0.0)) >= float(self._idle_restart_s)
                    ):
                        self.monitor.current_url = stream_url
                        self.monitor.is_playing = True
                        self.window.setProperty(self.P.PLAYING, "true")
                        self.monitor.start_metadata_monitoring(stream_url)
                        now_ts = time.time()
                        self._last_restart_ts = now_ts
                        self._last_activity_ts = now_ts
                    continue

                now_ts = time.time()
                if (now_ts - float(self._last_restart_ts or 0.0)) < 3.0:
                    continue

                self.monitor.current_url = stream_url
                self.monitor.is_playing = True
                self.window.setProperty(self.P.PLAYING, "true")
                self.monitor.start_metadata_monitoring(stream_url)
                self._last_restart_ts = now_ts

    def start_stream(self, url: str, station_name: str = "", station_id: str = "") -> None:
        stream_url = str(url or "").strip()
        if not stream_url:
            raise ValueError("Stream-URL darf nicht leer sein")
        if not stream_url.lower().startswith(("http://", "https://")):
            raise ValueError("Stream-URL muss mit http:// oder https:// beginnen")

        with self._lock:
            self.stop_stream(clear=False)
            self._desired_stream_url = stream_url
            self._last_restart_ts = time.time()
            self._last_activity_ts = self._last_restart_ts
            self._last_observed_state = ("", "", "", "")

            self.monitor.current_url = stream_url
            self.monitor.is_playing = True
            self.monitor.stop_thread = False
            if station_name:
                self.window.setProperty(self.P.STATION, station_name)
            self.window.setProperty(self.P.PLAYING, "true")

            # Optional radio.de hinting for API path (without source proof spoofing).
            if station_id:
                self.monitor.station_id = station_id
                self.monitor.plugin_slug = station_id
                self.monitor._set_api_source(self.monitor.API_SOURCE_RADIODE)

            self.monitor.start_metadata_monitoring(stream_url)

    def stop_stream(self, clear: bool = True) -> None:
        with self._lock:
            self._desired_stream_url = ""
            try:
                self.monitor.stop_metadata_monitoring()
            except Exception:
                pass
            self.monitor.is_playing = False
            self.monitor.current_url = None
            if clear:
                self.monitor.clear_properties()

    def shutdown(self) -> None:
        self._supervisor_stop.set()
        self.stop_stream(clear=True)
        try:
            self._supervisor_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.monitor._flush_station_profiles()
        except Exception:
            pass

    def property_snapshot(self) -> Dict[str, str]:
        return self.window._impl.snapshot()  # type: ignore[attr-defined]


# -----------------------------
# radio.de search helper
# -----------------------------


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", re.IGNORECASE)


def _collect_urls(node: Any, out: List[str]) -> None:
    if isinstance(node, str):
        value = node.strip()
        if _URL_RE.match(value):
            out.append(value)
        return
    if isinstance(node, dict):
        for value in node.values():
            _collect_urls(value, out)
        return
    if isinstance(node, list):
        for value in node:
            _collect_urls(value, out)


def _url_score(url: str) -> int:
    text = url.lower()
    score = 0
    if _IMAGE_EXT_RE.search(text):
        return -100
    if "radio-assets.com" in text or "logo" in text or "/images/" in text:
        return -80
    if text.endswith((".mp3", ".aac", ".ogg", ".pls", ".m3u", ".m3u8")):
        score += 40
    if any(token in text for token in ("stream", "live", "audio", "listen")):
        score += 15
    if "https://" in text:
        score += 2
    return score


def _pick_best_stream_url(data: Dict[str, Any]) -> str:
    urls: List[str] = []
    _collect_urls(data, urls)
    if not urls:
        return ""
    uniq: List[str] = []
    seen = set()
    for item in urls:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    ranked = sorted(uniq, key=_url_score, reverse=True)
    best = ranked[0]
    return best if _url_score(best) > 0 else ""


def _normalize_search_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"\\s+", " ", value)
    return value


def _compact_search_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").strip().lower())


def _search_match_score(item: RadioDeResult, query: str) -> int:
    q_norm = _normalize_search_text(query)
    q_compact = _compact_search_text(query)
    name_norm = _normalize_search_text(item.name)
    name_compact = _compact_search_text(item.name)
    sid_compact = _compact_search_text(item.station_id)
    score = 0

    if q_norm and name_norm == q_norm:
        score += 2000
    if q_compact and (name_compact == q_compact or sid_compact == q_compact):
        score += 1800
    if q_norm and name_norm.startswith(q_norm):
        score += 900
    if q_norm and q_norm in name_norm:
        score += 400

    q_tokens = [tok for tok in q_norm.split(" ") if tok]
    if q_tokens:
        token_hits = sum(1 for tok in q_tokens if tok in name_norm)
        score += token_hits * 120
        if token_hits == len(q_tokens):
            score += 350

    # Slight preference for better API rank when score ties.
    score += max(0, 200 - int(item.api_rank))
    return score


def resolve_radiode_details(station_id: str) -> Tuple[str, str]:
    from api_client import APIClient  # noqa: WPS433
    from constants import DEFAULT_HTTP_HEADERS, RADIODE_DETAILS_API_URL  # noqa: WPS433

    sid = str(station_id or "").strip()
    if not sid:
        return "", ""

    api = APIClient(headers=DEFAULT_HTTP_HEADERS)
    try:
        det = api.get(RADIODE_DETAILS_API_URL, params={"stationIds": sid}, timeout=8)
        payload = det.json() if det is not None else []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            first = payload[0]
            stream_url = _pick_best_stream_url(first)
            logo = str(first.get("logo300x300") or first.get("logo100x100") or "").strip()
            return stream_url, logo
    except Exception:
        return "", ""
    finally:
        api.close()
    return "", ""


def search_radiode(query: str) -> List[RadioDeResult]:
    from api_client import APIClient  # noqa: WPS433
    from constants import DEFAULT_HTTP_HEADERS, RADIODE_SEARCH_API_URL  # noqa: WPS433

    text = str(query or "").strip()
    if len(text) < 2:
        return []

    queries = [text]
    compact = _compact_search_text(text)
    if compact and compact != _compact_search_text(text.replace(" ", "")):
        queries.append(compact)
    elif " " in text and compact:
        queries.append(compact)

    api = APIClient(headers=DEFAULT_HTTP_HEADERS)
    results: List[RadioDeResult] = []
    try:
        for q in queries:
            response = api.get(RADIODE_SEARCH_API_URL, params={"query": q, "count": 120}, timeout=8)
            payload = response.json() if response is not None else {}
            playables = payload.get("playables") if isinstance(payload, dict) else []
            if not isinstance(playables, list):
                playables = []

            for idx, row in enumerate(playables):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                station_id = str(row.get("id") or row.get("stationId") or "").strip()
                logo = str(row.get("logo300x300") or row.get("logo100x100") or "").strip()
                stream_url = _pick_best_stream_url(row)

                if not name:
                    continue
                results.append(
                    RadioDeResult(
                        name=name,
                        station_id=station_id,
                        stream_url=stream_url,
                        logo=logo,
                        api_rank=idx,
                    )
                )
    finally:
        api.close()

    # Remove duplicates by station id/name while keeping best scored candidate.
    best_by_key: Dict[Tuple[str, str], Tuple[int, RadioDeResult]] = {}
    for item in results:
        key = (item.station_id.lower(), item.name.lower())
        score = _search_match_score(item, text)
        previous = best_by_key.get(key)
        if previous is None or score > previous[0]:
            best_by_key[key] = (score, item)

    ranked = sorted(
        best_by_key.values(),
        key=lambda pair: (-pair[0], pair[1].api_rank, pair[1].name.lower()),
    )
    return [item for _, item in ranked]


# -----------------------------
# Tkinter GUI
# -----------------------------


def run_gui(log_q: "queue.Queue[str]") -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    runtime = ASMRuntime()

    root = tk.Tk()
    root.title("ASM Standalone GUI (ohne Kodi)")
    root.geometry("1200x760")

    top = ttk.Frame(root, padding=8)
    top.pack(fill=tk.X)

    stream_var = tk.StringVar()
    station_var = tk.StringVar()
    station_id_var = tk.StringVar()
    search_var = tk.StringVar()
    status_var = tk.StringVar(value="Bereit")

    ttk.Label(top, text="Stream URL:").grid(row=0, column=0, sticky="w")
    stream_entry = ttk.Entry(top, textvariable=stream_var, width=90)
    stream_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(6, 6))

    ttk.Label(top, text="Station (Hint):").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(top, textvariable=station_var, width=40).grid(row=1, column=1, sticky="w", padx=(6, 6), pady=(6, 0))

    ttk.Label(top, text="Station-ID/Slug:").grid(row=1, column=2, sticky="e", pady=(6, 0))
    ttk.Entry(top, textvariable=station_id_var, width=24).grid(row=1, column=3, sticky="w", padx=(6, 6), pady=(6, 0))

    def do_start() -> None:
        try:
            runtime.start_stream(
                url=stream_var.get(),
                station_name=station_var.get(),
                station_id=station_id_var.get(),
            )
            status_var.set("Monitoring laeuft")
        except Exception as exc:
            messagebox.showerror("Start fehlgeschlagen", str(exc))

    def do_stop() -> None:
        runtime.stop_stream(clear=True)
        status_var.set("Gestoppt")

    ttk.Button(top, text="Start", command=do_start).grid(row=0, column=6, padx=(6, 4))
    ttk.Button(top, text="Stop", command=do_stop).grid(row=0, column=7, padx=(0, 4))

    ttk.Separator(root, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)

    # radio.de search block
    search_frame = ttk.LabelFrame(root, text="radio.de Suche", padding=8)
    search_frame.pack(fill=tk.X, padx=8)

    ttk.Label(search_frame, text="Suchtext:").grid(row=0, column=0, sticky="w")
    ttk.Entry(search_frame, textvariable=search_var, width=42).grid(row=0, column=1, sticky="w", padx=(6, 6))

    search_results: List[RadioDeResult] = []

    tree = ttk.Treeview(
        search_frame,
        columns=("name", "id", "stream"),
        show="headings",
        height=7,
    )
    tree.heading("name", text="Station")
    tree.heading("id", text="ID/Slug")
    tree.heading("stream", text="Stream URL")
    tree.column("name", width=260)
    tree.column("id", width=160)
    tree.column("stream", width=700)
    tree.grid(row=1, column=0, columnspan=8, sticky="nsew", pady=(8, 0))

    search_frame.grid_columnconfigure(7, weight=1)

    def update_tree(rows: List[RadioDeResult]) -> None:
        nonlocal search_results
        search_results = rows
        for item in tree.get_children():
            tree.delete(item)
        for idx, row in enumerate(rows):
            stream_text = row.stream_url or "(kein Stream-URL Feld erkannt)"
            tree.insert("", "end", iid=str(idx), values=(row.name, row.station_id, stream_text))

    def _search_worker(query: str) -> None:
        try:
            rows = search_radiode(query)
            root.after(0, lambda: update_tree(rows))
            root.after(0, lambda: status_var.set(f"{len(rows)} radio.de Treffer"))
        except Exception as exc:
            root.after(0, lambda: messagebox.showerror("Suche fehlgeschlagen", str(exc)))

    def do_search() -> None:
        text = search_var.get().strip()
        if len(text) < 2:
            messagebox.showwarning("Hinweis", "Bitte mindestens 2 Zeichen eingeben.")
            return
        status_var.set("radio.de Suche laeuft ...")
        threading.Thread(target=_search_worker, args=(text,), daemon=True).start()

    ttk.Button(search_frame, text="Suchen", command=do_search).grid(row=0, column=2, padx=(0, 4))

    def apply_selection(_: Any = None) -> None:
        selected = tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        if idx < 0 or idx >= len(search_results):
            return
        row = search_results[idx]
        station_var.set(row.name)
        station_id_var.set(row.station_id)
        if row.stream_url:
            stream_var.set(row.stream_url)
            status_var.set(f"Station uebernommen: {row.name}")
            return

        if not row.station_id:
            status_var.set(f"Station uebernommen (ohne Stream-URL): {row.name}")
            return

        status_var.set(f"Lade Stream-Details fuer {row.name} ...")

        def _details_worker(sid: str, station_name: str) -> None:
            stream_url, _ = resolve_radiode_details(sid)

            def _apply_result() -> None:
                if stream_url:
                    stream_var.set(stream_url)
                    status_var.set(f"Station uebernommen: {station_name}")
                else:
                    status_var.set(f"Station uebernommen (kein Stream-URL im Detail): {station_name}")

            root.after(0, _apply_result)

        threading.Thread(
            target=_details_worker,
            args=(row.station_id, row.name),
            daemon=True,
        ).start()

    tree.bind("<<TreeviewSelect>>", apply_selection)
    ttk.Button(search_frame, text="Auswahl uebernehmen", command=apply_selection).grid(row=0, column=3)

    # middle split: properties + logs
    middle = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
    middle.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    prop_frame = ttk.LabelFrame(middle, text="Window Properties", padding=8)
    log_frame = ttk.LabelFrame(middle, text="ASM Log", padding=8)
    middle.add(prop_frame, weight=3)
    middle.add(log_frame, weight=2)

    filter_var = tk.StringVar(value=".Artist,.Title,.Station")
    ttk.Label(prop_frame, text="Filter:").pack(anchor="w")
    ttk.Entry(prop_frame, textvariable=filter_var).pack(fill=tk.X, pady=(0, 6))

    prop_tree = ttk.Treeview(prop_frame, columns=("key", "value"), show="headings")
    prop_tree.heading("key", text="Property")
    prop_tree.heading("value", text="Wert")
    prop_tree.column("key", width=320)
    prop_tree.column("value", width=520)
    prop_tree.pack(fill=tk.BOTH, expand=True)

    log_container = ttk.Frame(log_frame)
    log_container.pack(fill=tk.BOTH, expand=True)
    log_container.grid_rowconfigure(0, weight=1)
    log_container.grid_columnconfigure(0, weight=1)

    log_text = tk.Text(log_container, wrap="none", height=18)
    log_scroll_y = ttk.Scrollbar(log_container, orient="vertical", command=log_text.yview)
    log_scroll_x = ttk.Scrollbar(log_container, orient="horizontal", command=log_text.xview)
    log_text.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)

    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll_y.grid(row=0, column=1, sticky="ns")
    log_scroll_x.grid(row=1, column=0, sticky="ew")

    status_bar = ttk.Label(root, textvariable=status_var, anchor="w")
    status_bar.pack(fill=tk.X, padx=8, pady=(0, 8))

    def poll_updates() -> None:
        # Properties
        snap = runtime.property_snapshot()
        raw_filter = filter_var.get().strip()
        needles = [part.strip().lower() for part in raw_filter.split(",") if part.strip()]
        visible_items = []
        for key, value in snap.items():
            if not key:
                continue
            key_l = key.lower()
            if needles and not any(needle in key_l for needle in needles):
                continue
            visible_items.append((key, value))
        visible_items.sort(key=lambda it: it[0])

        for item in prop_tree.get_children():
            prop_tree.delete(item)
        for idx, (key, value) in enumerate(visible_items):
            prop_tree.insert("", "end", iid=str(idx), values=(key, value))

        # Logs
        got_log = False
        while True:
            try:
                line = log_q.get_nowait()
            except queue.Empty:
                break
            got_log = True
            log_text.insert("end", line + "\n")
        if got_log:
            log_text.see("end")

        root.after(500, poll_updates)

    def on_close() -> None:
        try:
            runtime.shutdown()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll_updates()
    root.mainloop()


def run_self_test(log_q: "queue.Queue[str]") -> int:
    runtime = ASMRuntime()
    try:
        snap = runtime.property_snapshot()
        print(f"Self-test OK: runtime initialized, {len(snap)} initial properties")
        return 0
    finally:
        runtime.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="ASM Standalone GUI without Kodi")
    parser.add_argument("--self-test", action="store_true", help="Initialize runtime without starting GUI")
    args = parser.parse_args()

    log_q: "queue.Queue[str]" = queue.Queue(maxsize=10000)
    install_kodi_stubs(log_q)

    if args.self_test:
        return run_self_test(log_q)

    try:
        run_gui(log_q)
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
