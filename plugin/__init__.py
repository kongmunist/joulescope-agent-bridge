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
    {"cmd": "accumulators"}          -> {"ok": true, "charge_c": ..., "energy_j": ..., "sample_time_s": ...}
    {"cmd": "power_set", "on": true} -> {"ok": true, "target_power": true}

Note: target_power is published on the device's settings topic, which cuts
the rail but does not visually update the toolbar power button. The toolbar
button writes to an app-level topic that is dispatched through a path we
can't easily mirror from a plugin.
"""

from __future__ import annotations

import collections
import json
import logging
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
_LOG = logging.getLogger(__name__)

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


def _extract_accumulator(stats: dict, name: str) -> Optional[float]:
    try:
        return float(stats["accumulators"][name]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _first_present(*values: Optional[float]) -> Optional[float]:
    for value in values:
        if value is not None:
            return value
    return None


def _extract_sample_time(stats: dict, fallback: float) -> tuple[float, str]:
    try:
        t_range = stats["time"]["range"]["value"]
        return float(t_range[1]), "stats.time.range.end"
    except (KeyError, IndexError, TypeError, ValueError):
        return fallback, "host.monotonic"


class _Bridge:
    """Holds the rolling buffer and the device topic prefix."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.device_prefix: Optional[str] = None
        self.unique_id: Optional[str] = None
        self.samples: collections.deque[tuple[float, float, float, float]] = collections.deque()
        self.last: Optional[tuple[float, float, float, float]] = None  # (t, v, i, p)
        self.last_accumulators: Optional[tuple[float, float, float, float, float, float, int, str]] = None
        # (sample_time_s, charge_c, energy_j, v, i, p, event_count, sample_time_source)
        self.last_stats: Optional[dict] = None
        self.event_count: int = 0

    def attach(self, unique_id: str) -> None:
        prefix = f"registry/{unique_id}"
        with self.lock:
            self.unique_id = unique_id
            self.device_prefix = prefix
            self.samples.clear()
            self.last = None
            self.last_accumulators = None
            self.last_stats = None
            self.event_count = 0
        try:
            pubsub_singleton.subscribe(f"{prefix}/{_STATS_SUBTOPIC}", self._on_stats, ["pub"])
        except Exception:
            pass

    def _on_stats(self, topic: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        v = _first_present(_extract_mean(value, "v"), _extract_mean(value, "voltage"))
        i = _first_present(_extract_mean(value, "i"), _extract_mean(value, "current"))
        p = _first_present(_extract_mean(value, "p"), _extract_mean(value, "power"))
        if v is None or i is None or p is None:
            return
        charge = _extract_accumulator(value, "charge")
        energy = _extract_accumulator(value, "energy")
        now = time.monotonic()
        sample_time_s, sample_time_source = _extract_sample_time(value, now)
        with self.lock:
            self.samples.append((now, v, i, p))
            self.last = (now, v, i, p)
            self.last_stats = value
            self.event_count += 1
            if charge is not None and energy is not None:
                self.last_accumulators = (
                    sample_time_s,
                    charge,
                    energy,
                    v,
                    i,
                    p,
                    self.event_count,
                    sample_time_source,
                )
            cutoff = now - WINDOW_S
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()

    def snapshot(self) -> Optional[tuple[float, float, float, float]]:
        with self.lock:
            return self.last

    def window(self) -> list[tuple[float, float, float, float]]:
        with self.lock:
            return list(self.samples)

    def accumulators(self) -> Optional[tuple[float, float, float, float, float, float, int, str]]:
        with self.lock:
            return self.last_accumulators

    def stats_raw(self) -> Optional[dict]:
        with self.lock:
            return self.last_stats

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
    if cmd == "accumulators":
        accum = _bridge.accumulators()
        if accum is None:
            return {"ok": False, "error": "no accumulator samples yet"}
        sample_time_s, charge_c, energy_j, v, i, p, event_count, sample_time_source = accum
        return {
            "ok": True,
            "sample_time_s": sample_time_s,
            "sample_time_source": sample_time_source,
            "charge_c": charge_c,
            "energy_j": energy_j,
            "voltage": v,
            "current": i,
            "power": p,
            "event_count": event_count,
        }
    if cmd == "stats_raw":
        stats = _bridge.stats_raw()
        if stats is None:
            return {"ok": False, "error": "no samples yet"}
        return {"ok": True, "stats": stats}
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


_backend_started = False


def _activate_backend() -> None:
    """Start the socket backend when the plugin is loaded.

    Older versions required the status widget to be visible before the socket
    was bound. That made headless/SSH automation depend on a saved UI layout.
    """
    global _backend_started
    if _backend_started:
        return
    _backend_started = True

    for cap in _CAPABILITY_TOPICS:
        try:
            pubsub_singleton.subscribe(cap, _on_capability_list, ["pub", "retain"])
        except Exception:
            _LOG.exception("failed to subscribe to %s", cap)

    for uid in _discover_devices():
        if _bridge.device_prefix is None:
            _bridge.attach(uid)
            break

    try:
        _start_server()
    except OSError:
        _LOG.exception("failed to bind Agent Bridge on %s:%s", BIND_HOST, BIND_PORT)


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
        super().closeEvent(event)


_activate_backend()
