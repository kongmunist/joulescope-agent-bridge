"""Client for the Joulescope Agent Bridge.

Tries the UI plugin's localhost socket first. If the UI isn't running,
falls back to claiming the JS220 directly via the `joulescope` package
(install with `pip install joulescope`). Self-recovers: when the UI
comes back, a watcher thread releases the direct claim so the UI can
attach again.

Public API (stable across both backends):

    device()            -> "JS220-XXXXXX"
    read_voltage()      -> float, V
    read_current()      -> float, A
    read_power()        -> float, W
    avg_1s()            -> {"n": int, "voltage": ..., "current": ..., "power": ...}
    set_power(on: bool) -> bool

`BridgeError` is raised for backend errors (no device, no samples yet, etc.).
"""

from __future__ import annotations

import atexit
import collections
import json
import socket
import threading
import time
from typing import Any

HOST = "127.0.0.1"
PORT = 9876
TIMEOUT_S = 2.0
BRIDGE_PROBE_S = 0.2
WATCHER_INTERVAL_S = 2.0
DIRECT_FIRST_SAMPLE_TIMEOUT_S = 3.0


class BridgeError(RuntimeError):
    """Raised when no backend can satisfy the request."""


# ---------- Bridge backend (UI plugin's socket) --------------------------

def _bridge_request(payload: dict) -> dict:
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT_S) as s:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
    line = bytes(buf).split(b"\n", 1)[0]
    if not line:
        raise BridgeError("empty response from bridge")
    return json.loads(line.decode("utf-8"))


def _bridge_reachable() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=BRIDGE_PROBE_S):
            return True
    except OSError:
        return False


# ---------- Direct backend (joulescope package, used when UI is closed) ---

class _Direct:
    """Owns a `joulescope` Device when the UI plugin is unreachable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dev: Any = None
        self._unique_id: str | None = None
        self._samples: collections.deque[tuple[float, float, float, float]] = collections.deque()
        self._last: tuple[float, float, float, float] | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def is_open(self) -> bool:
        with self._lock:
            return self._dev is not None

    def open(self) -> None:
        with self._lock:
            if self._dev is not None:
                return
            try:
                from joulescope import scan
            except ImportError as e:
                raise BridgeError(
                    "direct fallback needs the 'joulescope' package: pip install joulescope"
                ) from e
            devices = scan(config="off")
            if not devices:
                raise BridgeError("no JS220 found and Joulescope UI not running")
            dev = devices[0]
            dev.open()
            dev.parameter_set("v_range", "15V")
            dev.parameter_set("i_range", "auto")
            dev.statistics_callback_register(self._on_stats, "sensor")
            dev.start()
            self._dev = dev
            self._unique_id = self._extract_uid(dev)
            self._samples.clear()
            self._last = None
        self._stop.clear()
        if self._watcher is None or not self._watcher.is_alive():
            self._watcher = threading.Thread(
                target=self._watch_for_bridge,
                name="joulescope-direct-watcher",
                daemon=True,
            )
            self._watcher.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            dev = self._dev
            self._dev = None
            self._unique_id = None
            self._samples.clear()
            self._last = None
        if dev is not None:
            for fn in (dev.stop, dev.close):
                try:
                    fn()
                except Exception:
                    pass

    @staticmethod
    def _extract_uid(dev: Any) -> str:
        for attr in ("device_serial_number", "serial_number"):
            v = getattr(dev, attr, None)
            if v:
                return f"JS220-{v}"
        s = str(dev)
        if "JS220-" in s:
            tail = s[s.index("JS220-"):]
            return tail.split()[0].rstrip(">").rstrip(",")
        return s

    def _on_stats(self, stats: dict) -> None:
        try:
            sig = stats["signals"]
            v = float(sig["voltage"]["µ"]["value"])
            i = float(sig["current"]["µ"]["value"])
            p = float(sig["power"]["µ"]["value"])
        except (KeyError, TypeError, ValueError):
            return
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, v, i, p))
            self._last = (now, v, i, p)
            cutoff = now - 1.0
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def _wait_for_sample(self) -> None:
        deadline = time.monotonic() + DIRECT_FIRST_SAMPLE_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                if self._last is not None:
                    return
            time.sleep(0.05)
        raise BridgeError(
            f"no statistics from JS220 within {DIRECT_FIRST_SAMPLE_TIMEOUT_S}s"
        )

    def _watch_for_bridge(self) -> None:
        # Poll the bridge socket; when it's up, release the device so the UI
        # plugin can take over. The next client call will route via the bridge.
        while not self._stop.wait(WATCHER_INTERVAL_S):
            if _bridge_reachable():
                self.close()
                return

    def request(self, payload: dict) -> dict:
        cmd = payload.get("cmd")
        if cmd == "device":
            self.open()
            return {"ok": True, "unique_id": self._unique_id}
        if cmd in ("voltage", "current", "power"):
            self.open()
            self._wait_for_sample()
            with self._lock:
                _, v, i, p = self._last  # type: ignore[misc]
            value = {"voltage": v, "current": i, "power": p}[cmd]
            unit = {"voltage": "V", "current": "A", "power": "W"}[cmd]
            return {"ok": True, "value": value, "unit": unit}
        if cmd == "stats_1s":
            self.open()
            self._wait_for_sample()
            with self._lock:
                win = list(self._samples)
            n = len(win)
            return {
                "ok": True,
                "n": n,
                "voltage": sum(s[1] for s in win) / n,
                "current": sum(s[2] for s in win) / n,
                "power": sum(s[3] for s in win) / n,
            }
        if cmd == "power_set":
            self.open()
            on = bool(payload["on"])
            with self._lock:
                self._dev.parameter_set("i_range", "auto" if on else "off")
            return {"ok": True, "target_power": on}
        return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


_direct = _Direct()
atexit.register(_direct.close)


# ---------- Unified entry point ------------------------------------------

def _request(payload: dict) -> dict:
    try:
        resp = _bridge_request(payload)
        # Bridge succeeded; release the direct claim if we'd been holding it.
        if _direct.is_open():
            _direct.close()
    except (ConnectionRefusedError, socket.timeout):
        resp = _direct.request(payload)
    except OSError:
        # Any other socket error: also fall back if direct is/was already in use.
        resp = _direct.request(payload)
    if not resp.get("ok"):
        raise BridgeError(resp.get("error", "unknown error"))
    return resp


# ---------- Public API ---------------------------------------------------

def device() -> str:
    return _request({"cmd": "device"})["unique_id"]


def read_voltage() -> float:
    return _request({"cmd": "voltage"})["value"]


def read_current() -> float:
    return _request({"cmd": "current"})["value"]


def read_power() -> float:
    return _request({"cmd": "power"})["value"]


def avg_1s() -> dict[str, Any]:
    r = _request({"cmd": "stats_1s"})
    return {"n": r["n"], "voltage": r["voltage"], "current": r["current"], "power": r["power"]}


def set_power(on: bool) -> bool:
    return _request({"cmd": "power_set", "on": bool(on)})["target_power"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        backend = "bridge" if _bridge_reachable() else "direct"
        print(f"backend  : {backend}")
        print(f"device   : {device()}")
        print(f"voltage  : {read_voltage():.4f} V")
        print(f"current  : {read_current()*1e3:.4f} mA")
        print(f"power    : {read_power()*1e3:.4f} mW")
        print(f"avg 1s   : {avg_1s()}")
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "on":
        print(set_power(True))
    elif cmd == "off":
        print(set_power(False))
    else:
        print(f"usage: {sys.argv[0]} [on|off]", file=sys.stderr)
        sys.exit(2)
