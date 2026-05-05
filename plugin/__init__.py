"""Joulescope Agent Bridge — UI plugin for JS220.

Lets an external script read live voltage/current/power and toggle the
JS220 high-side switch over a localhost JSON-line socket WHILE the
Joulescope UI keeps the device open. The plugin runs inside the UI's
process, so it shares the existing USB claim instead of fighting it.

Wire protocol (one JSON object per line, request and response):

    {"cmd": "device"}                -> {"ok": true, "unique_id": "JS220-001234"}
    {"cmd": "voltage"}               -> {"ok": true, "value": 5.12, "unit": "V"}
    {"cmd": "current"}               -> {"ok": true, "value": 0.0123, "unit": "A"}
    {"cmd": "power"}                 -> {"ok": true, "value": 0.063, "unit": "W"}
    {"cmd": "stats_1s"}              -> {"ok": true, "n": 2, "voltage": ..., "current": ..., "power": ...}
    {"cmd": "power_set", "on": true} -> {"ok": true, "target_power": true}

Note: target_power is published on the device's settings topic, which cuts
the rail but does not visually update the toolbar power button. The toolbar
button writes to an app-level topic that is dispatched through a path we
can't easily mirror from a plugin.
"""

from __future__ import annotations

import collections
import json
import socket
import socketserver
import threading
import time
from typing import Any, Optional

from joulescope_ui import N_, pubsub_singleton, register
from joulescope_ui.styles import styled_widget

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtCore, QtWidgets


BIND_HOST = "127.0.0.1"
BIND_PORT = 9876
WINDOW_S = 1.0
DEVICE_PREFIX_MATCH = "JS220-"

# Subtopics relative to a device's registry root (registry/<unique_id>/...).
_STATS_SUBTOPIC = "events/statistics/!data"
_TARGET_POWER_SUBTOPIC = "settings/target_power"  # bool

# Capability lists the registry manager publishes; we try each in turn.
_CAPABILITY_TOPICS = [
    "registry_manager/capabilities/device.object/list",
    "registry_manager/capabilities/source/list",
    "registry_manager/capabilities/signal_stream_source/list",
    "registry_manager/capabilities/statistic_stream_source/list",
]


def _extract_mean(stats: dict, signal: str) -> Optional[float]:
    """Pull the mean value for a signal out of a statistics-event payload.
    Tries the modern 'avg' key and the legacy 'µ' key."""
    for key in ("avg", "µ"):
        try:
            return float(stats["signals"][signal][key]["value"])
        except (KeyError, TypeError, ValueError):
            pass
    return None


class _Bridge:
    """Holds the rolling buffer and the device topic prefix."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.device_prefix: Optional[str] = None
        self.unique_id: Optional[str] = None
        self.samples: collections.deque[tuple[float, float, float, float]] = collections.deque()
        self.last: Optional[tuple[float, float, float, float]] = None  # (t, v, i, p)
        self.event_count: int = 0

    def attach(self, unique_id: str) -> None:
        prefix = f"registry/{unique_id}"
        with self.lock:
            self.unique_id = unique_id
            self.device_prefix = prefix
            self.samples.clear()
            self.last = None
            self.event_count = 0
        try:
            pubsub_singleton.subscribe(f"{prefix}/{_STATS_SUBTOPIC}", self._on_stats, ["pub"])
        except Exception:
            pass

    def _on_stats(self, topic: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        v = _extract_mean(value, "v") or _extract_mean(value, "voltage")
        i = _extract_mean(value, "i") or _extract_mean(value, "current")
        p = _extract_mean(value, "p") or _extract_mean(value, "power")
        if v is None or i is None or p is None:
            return
        now = time.monotonic()
        with self.lock:
            self.samples.append((now, v, i, p))
            self.last = (now, v, i, p)
            self.event_count += 1
            cutoff = now - WINDOW_S
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()

    def snapshot(self) -> Optional[tuple[float, float, float, float]]:
        with self.lock:
            return self.last

    def window(self) -> list[tuple[float, float, float, float]]:
        with self.lock:
            return list(self.samples)

    def set_power(self, on: bool) -> bool:
        if self.device_prefix is None:
            raise RuntimeError("no device attached")
        value = bool(on)
        pubsub_singleton.publish(f"{self.device_prefix}/{_TARGET_POWER_SUBTOPIC}", value)
        return value


_bridge = _Bridge()


def _handle_request(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "device":
        return {"ok": True, "unique_id": _bridge.unique_id}
    if cmd in ("voltage", "current", "power"):
        snap = _bridge.snapshot()
        if snap is None:
            return {"ok": False, "error": "no samples yet"}
        _, v, i, p = snap
        if cmd == "voltage":
            return {"ok": True, "value": v, "unit": "V"}
        if cmd == "current":
            return {"ok": True, "value": i, "unit": "A"}
        return {"ok": True, "value": p, "unit": "W"}
    if cmd == "stats_1s":
        win = _bridge.window()
        if not win:
            return {"ok": False, "error": "no samples yet"}
        n = len(win)
        return {
            "ok": True,
            "n": n,
            "voltage": sum(s[1] for s in win) / n,
            "current": sum(s[2] for s in win) / n,
            "power": sum(s[3] for s in win) / n,
        }
    if cmd == "power_set":
        try:
            value = _bridge.set_power(bool(req["on"]))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "target_power": value}
    return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


class _LineHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                resp = {"ok": False, "error": f"bad json: {e}"}
            else:
                try:
                    resp = _handle_request(req)
                except Exception as e:  # last-resort guard
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
            self.wfile.flush()


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


_server: Optional[_ThreadedServer] = None
_server_thread: Optional[threading.Thread] = None


def _start_server() -> None:
    global _server, _server_thread
    if _server is not None:
        return
    _server = _ThreadedServer((BIND_HOST, BIND_PORT), _LineHandler)
    _server_thread = threading.Thread(
        target=_server.serve_forever,
        name="joulescope-agent-bridge",
        daemon=True,
    )
    _server_thread.start()


def _stop_server() -> None:
    global _server, _server_thread
    if _server is None:
        return
    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None


def _candidate_uids(value: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(value, (list, tuple)):
        return out
    for entry in value:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            uid = entry.get("unique_id") or entry.get("name") or entry.get("id")
            if uid:
                out.append(uid)
    return out


def _on_capability_list(topic: str, value: Any) -> None:
    """Auto-attach to the first matching device we see in any capability list."""
    if _bridge.device_prefix is not None:
        return
    for uid in _candidate_uids(value):
        if DEVICE_PREFIX_MATCH in uid:
            _bridge.attach(uid)
            return


def _discover_devices() -> list[str]:
    """Best-effort scan of pubsub for any registry/<DEVICE_PREFIX_MATCH>* nodes."""
    found: set[str] = set()
    for cap in _CAPABILITY_TOPICS:
        try:
            v = pubsub_singleton.query(cap)
        except Exception:
            continue
        for uid in _candidate_uids(v):
            if DEVICE_PREFIX_MATCH in uid:
                found.add(uid)
    # Fallback: walk the topic table directly. The attribute is private but
    # has been stable across recent pyjoulescope_ui versions.
    table = getattr(pubsub_singleton, "_topic_by_name", None)
    if isinstance(table, dict):
        for name in table.keys():
            if name.startswith(f"registry/{DEVICE_PREFIX_MATCH}"):
                parts = name.split("/", 2)
                if len(parts) >= 2:
                    found.add(parts[1])
    return sorted(found)


@register
@styled_widget(N_("Agent Bridge"))
class AgentBridgeWidget(QtWidgets.QWidget):
    """Status widget; instantiating it starts the JSON-line socket."""

    SETTINGS = {}
    CAPABILITIES = ["widget@"]

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        self._label = QtWidgets.QLabel("Agent Bridge: starting…", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        row = QtWidgets.QHBoxLayout()
        self._uid_combo = QtWidgets.QComboBox(self)
        self._uid_combo.setEditable(True)
        self._uid_combo.setMinimumWidth(200)
        row.addWidget(self._uid_combo, 1)
        self._refresh_btn = QtWidgets.QPushButton("Refresh", self)
        self._refresh_btn.clicked.connect(self._on_refresh)
        row.addWidget(self._refresh_btn)
        self._attach_btn = QtWidgets.QPushButton("Attach", self)
        self._attach_btn.clicked.connect(self._on_attach)
        row.addWidget(self._attach_btn)
        layout.addLayout(row)
        layout.addStretch(1)

        for cap in _CAPABILITY_TOPICS:
            try:
                pubsub_singleton.subscribe(cap, _on_capability_list, ["pub", "retain"])
            except Exception:
                pass
        self._on_refresh()

        try:
            _start_server()
        except OSError as e:
            self._label.setText(f"Agent Bridge: failed to bind {BIND_HOST}:{BIND_PORT}: {e}")
            return

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh_label)
        self._timer.start()
        self._refresh_label()

    def _on_refresh(self) -> None:
        candidates = _discover_devices()
        self._uid_combo.clear()
        self._uid_combo.addItems(candidates)
        if candidates and _bridge.device_prefix is None:
            _bridge.attach(candidates[0])
        self._refresh_label()

    def _on_attach(self) -> None:
        uid = self._uid_combo.currentText().strip()
        if uid:
            _bridge.attach(uid)
            self._refresh_label()

    def _refresh_label(self) -> None:
        with _bridge.lock:
            uid = _bridge.unique_id
            count = _bridge.event_count
            last = _bridge.last
        lines = [
            f"Agent Bridge — {BIND_HOST}:{BIND_PORT}",
            f"device: {uid or '(none — Refresh, then Attach)'}",
            f"events: {count}",
        ]
        if last:
            _, v, i, p = last
            lines.append(f"V={v:.4f}   I={i*1e3:.4f} mA   P={p*1e3:.4f} mW")
        self._label.setText("\n".join(lines))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        _stop_server()
        super().closeEvent(event)
