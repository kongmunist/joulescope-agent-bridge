"""Client for the Joulescope Agent Bridge.

Tries the UI plugin's localhost socket first. If the UI isn't running,
falls back to claiming the JS220 directly via `pyjoulescope_driver`
(install with `pip install pyjoulescope_driver`). Self-recovers: when
the UI comes back, a watcher thread releases the direct claim so the
UI can attach again.

Public API (stable across both backends):

    device()            -> "JS220-XXXXXX"
    read_voltage()      -> float, V
    read_current()      -> float, A
    read_power()        -> float, W
    avg_1s()            -> {"n": int, "voltage": ..., "current": ..., "power": ...}
    read_accumulators() -> {"sample_time_s": ..., "charge_c": ..., "energy_j": ...}
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

__version__ = "0.1.0"

__all__ = [
    "BridgeError",
    "device",
    "read_voltage",
    "read_current",
    "read_power",
    "avg_1s",
    "read_accumulators",
    "read_stats_raw",
    "accumulator_delta",
    "benchmark_accumulators",
    "set_power",
    "__version__",
]

HOST = "127.0.0.1"
PORT = 9876
TIMEOUT_S = 2.0
BRIDGE_PROBE_S = 0.2
WATCHER_INTERVAL_S = 2.0
DIRECT_FIRST_SAMPLE_TIMEOUT_S = 3.0
DIRECT_STATS_HZ = 2.0


class BridgeError(RuntimeError):
    """Raised when no backend can satisfy the request."""


def _stats_mean(stats: dict, signal: str) -> float | None:
    """Pull the mean for a signal out of a `s/stats/value` payload."""
    for key in ("avg", "µ"):
        try:
            return float(stats["signals"][signal][key]["value"])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def _stats_accumulator(stats: dict, name: str) -> float | None:
    try:
        return float(stats["accumulators"][name]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _first_present(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _stats_sample_time(stats: dict, fallback: float) -> tuple[float, str]:
    try:
        t_range = stats["time"]["range"]["value"]
        return float(t_range[1]), "stats.time.range.end"
    except (KeyError, IndexError, TypeError, ValueError):
        return fallback, "host.monotonic"


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
        self._driver: Any = None
        self._path: str | None = None
        self._unique_id: str | None = None
        self._samples: collections.deque[tuple[float, float, float, float]] = collections.deque()
        self._last: tuple[float, float, float, float] | None = None
        self._last_accumulators: tuple[float, float, float, float, float, float, int, str] | None = None
        # (sample_time_s, charge_c, energy_j, v, i, p, event_count, sample_time_source)
        self._last_stats: dict | None = None
        self._event_count = 0
        self._closed = True  # gate on late callbacks
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def is_open(self) -> bool:
        with self._lock:
            return self._driver is not None

    def open(self) -> None:
        with self._lock:
            if self._driver is not None:
                return
            try:
                from pyjoulescope_driver import Driver
            except ImportError as e:
                raise BridgeError(
                    "direct fallback needs pyjoulescope_driver: "
                    "pip install pyjoulescope_driver"
                ) from e
            d = Driver()
            paths = [p for p in d.device_paths() if "/js220/" in p]
            if not paths:
                raise BridgeError("no JS220 found and Joulescope UI not running")
            path = paths[0]
            d.open(path)
            scnt = max(1, int(1_000_000 / DIRECT_STATS_HZ))
            try:
                d.publish(f"{path}/s/stats/scnt", scnt, timeout=0)
            except Exception:
                pass
            try:
                # Make sure rail is on; "auto" is the default powered state.
                d.publish(f"{path}/s/i/range/mode", "auto", timeout=0)
            except Exception:
                pass
            d.subscribe(f"{path}/s/stats/value", "pub", self._on_stats)
            try:
                # Enable the statistics stream — without this no callbacks fire.
                d.publish(f"{path}/s/stats/ctrl", 1, timeout=0)
            except Exception:
                pass
            self._driver = d
            self._path = path
            self._unique_id = self._extract_uid(path)
            self._samples.clear()
            self._last = None
            self._last_accumulators = None
            self._last_stats = None
            self._event_count = 0
            self._closed = False
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
        if self._watcher is not None and self._watcher.is_alive():
            self._watcher.join(timeout=0.5)
        with self._lock:
            d = self._driver
            path = self._path
            self._closed = True
            self._driver = None
            self._path = None
            self._unique_id = None
            self._samples.clear()
            self._last = None
            self._last_accumulators = None
            self._last_stats = None
            self._event_count = 0
        if d is None or path is None:
            return
        # Block on the unsubscribe so no late callbacks dispatch into our
        # half-torn-down state. Then close the device path. We deliberately
        # don't call d.finalize() — the C jsdrv lives until the process
        # exits and gets cleaned up by the OS.
        try:
            d.publish(f"{path}/s/stats/ctrl", 0, timeout=0)
        except Exception:
            pass
        try:
            d.unsubscribe(f"{path}/s/stats/value", self._on_stats, timeout=0.5)
        except Exception:
            pass
        try:
            d.close(path)
        except Exception:
            pass

    @staticmethod
    def _extract_uid(path: str) -> str:
        # path looks like "u/js220/005633"
        sn = path.rstrip("/").rsplit("/", 1)[-1]
        return f"JS220-{sn}"

    def _on_stats(self, topic: str, value: Any) -> None:
        # Defensive: callbacks can fire briefly after close() returns.
        if self._closed or not isinstance(value, dict):
            return
        v = _first_present(_stats_mean(value, "v"), _stats_mean(value, "voltage"))
        i = _first_present(_stats_mean(value, "i"), _stats_mean(value, "current"))
        p = _first_present(_stats_mean(value, "p"), _stats_mean(value, "power"))
        if v is None or i is None or p is None:
            return
        charge = _stats_accumulator(value, "charge")
        energy = _stats_accumulator(value, "energy")
        now = time.monotonic()
        sample_time_s, sample_time_source = _stats_sample_time(value, now)
        with self._lock:
            if self._closed:
                return
            self._samples.append((now, v, i, p))
            self._last = (now, v, i, p)
            self._last_stats = value
            self._event_count += 1
            if charge is not None and energy is not None:
                self._last_accumulators = (
                    sample_time_s,
                    charge,
                    energy,
                    v,
                    i,
                    p,
                    self._event_count,
                    sample_time_source,
                )
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
        if cmd == "accumulators":
            self.open()
            self._wait_for_sample()
            with self._lock:
                accum = self._last_accumulators
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
            self.open()
            self._wait_for_sample()
            with self._lock:
                stats = self._last_stats
            if stats is None:
                return {"ok": False, "error": "no samples yet"}
            return {"ok": True, "stats": stats}
        if cmd == "power_set":
            self.open()
            on = bool(payload["on"])
            with self._lock:
                d = self._driver
                path = self._path
            if d is None or path is None:
                return {"ok": False, "error": "device not open"}
            try:
                d.publish(f"{path}/s/i/range/mode", "auto" if on else "off", timeout=0)
            except Exception as e:
                return {"ok": False, "error": f"publish failed: {e}"}
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
        if (
            payload.get("cmd") == "accumulators"
            and "unknown cmd" in str(resp.get("error", ""))
        ):
            raise BridgeError(
                "running bridge plugin does not support accumulators; "
                "restart Joulescope UI or reload the Agent Bridge plugin"
            )
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


def read_accumulators() -> dict[str, Any]:
    r = _request({"cmd": "accumulators"})
    return {
        "sample_time_s": r["sample_time_s"],
        "sample_time_source": r.get("sample_time_source"),
        "charge_c": r["charge_c"],
        "energy_j": r["energy_j"],
        "voltage": r["voltage"],
        "current": r["current"],
        "power": r["power"],
        "event_count": r.get("event_count"),
    }


def read_stats_raw() -> dict[str, Any]:
    return _request({"cmd": "stats_raw"})["stats"]


def accumulator_delta(start: dict[str, Any], end: dict[str, Any]) -> dict[str, float]:
    elapsed_s = float(end["sample_time_s"]) - float(start["sample_time_s"])
    if elapsed_s <= 0:
        raise BridgeError(f"non-positive accumulator elapsed time: {elapsed_s}")
    charge_c = float(end["charge_c"]) - float(start["charge_c"])
    energy_j = float(end["energy_j"]) - float(start["energy_j"])
    return {
        "elapsed_s": elapsed_s,
        "charge_c": charge_c,
        "energy_j": energy_j,
        "current": charge_c / elapsed_s,
        "power": energy_j / elapsed_s,
    }


def benchmark_accumulators(duration_s: float = 60.0, poll_s: float = 1.0) -> dict[str, Any]:
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if poll_s <= 0:
        raise ValueError("poll_s must be > 0")

    start = read_accumulators()
    deadline = time.monotonic() + duration_s
    old_samples: list[dict[str, Any]] = []
    while True:
        old_samples.append(avg_1s())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_s, remaining))
    end = read_accumulators()

    old_n = len(old_samples)
    old = {
        "samples": old_n,
        "bridge_n_total": sum(int(s["n"]) for s in old_samples),
        "voltage": sum(float(s["voltage"]) for s in old_samples) / old_n,
        "current": sum(float(s["current"]) for s in old_samples) / old_n,
        "power": sum(float(s["power"]) for s in old_samples) / old_n,
    }
    accum = accumulator_delta(start, end)
    return {
        "duration_s": duration_s,
        "poll_s": poll_s,
        "old_avg_1s": old,
        "accumulator": accum,
        "start": start,
        "end": end,
        "diff": {
            "current": old["current"] - accum["current"],
            "power": old["power"] - accum["power"],
            "current_pct": (
                (old["current"] / accum["current"] - 1.0) * 100.0
                if accum["current"] else float("nan")
            ),
            "power_pct": (
                (old["power"] / accum["power"] - 1.0) * 100.0
                if accum["power"] else float("nan")
            ),
        },
    }


def set_power(on: bool) -> bool:
    return _request({"cmd": "power_set", "on": bool(on)})["target_power"]


def _cli_main() -> int:
    import sys

    if len(sys.argv) < 2:
        backend = "bridge" if _bridge_reachable() else "direct"
        avg = avg_1s()
        print(f"backend  : {backend}")
        print(f"device   : {device()}")
        print(f"voltage  : {read_voltage():.6f} V")
        print(f"current  : {read_current()*1e3:.6f} mA")
        print(f"power    : {read_power()*1e3:.6f} mW")
        print(f"avg V 1s : {avg['voltage']:.6f} V")
        print(f"avg I 1s : {avg['current']*1e3:.6f} mA")
        print(f"avg P 1s : {avg['power']*1e3:.6f} mW")
        print(f"samples  : {avg['n']}")
        try:
            accum = read_accumulators()
        except BridgeError as e:
            print(f"accum    : unavailable ({e})")
        else:
            print(f"charge   : {accum['charge_c']:.9g} C")
            print(f"energy   : {accum['energy_j']:.9g} J")
        return 0
    cmd = sys.argv[1]
    if cmd == "on":
        print(set_power(True))
        return 0
    if cmd == "off":
        print(set_power(False))
        return 0
    if cmd == "accum":
        accum = read_accumulators()
        print(json.dumps(accum, indent=2, sort_keys=True))
        return 0
    if cmd == "stats-raw":
        stats = read_stats_raw()
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0
    if cmd == "bench":
        duration_s = float(sys.argv[2]) if len(sys.argv) >= 3 else 60.0
        poll_s = float(sys.argv[3]) if len(sys.argv) >= 4 else 1.0
        result = benchmark_accumulators(duration_s, poll_s)
        old = result["old_avg_1s"]
        accum = result["accumulator"]
        diff = result["diff"]
        print(f"duration       : {result['duration_s']:.3f} s")
        print(f"accum elapsed  : {accum['elapsed_s']:.3f} s")
        print(f"old samples    : {old['samples']} polls, {old['bridge_n_total']} bridge stats")
        print(f"old avg current: {old['current'] * 1e6:.6f} uA")
        print(f"accum current  : {accum['current'] * 1e6:.6f} uA")
        print(f"diff current   : {diff['current'] * 1e6:.6f} uA ({diff['current_pct']:.3f}%)")
        print(f"old avg power  : {old['power'] * 1e6:.6f} uW")
        print(f"accum power    : {accum['power'] * 1e6:.6f} uW")
        print(f"diff power     : {diff['power'] * 1e6:.6f} uW ({diff['power_pct']:.3f}%)")
        print(f"charge delta   : {accum['charge_c']:.9g} C")
        print(f"energy delta   : {accum['energy_j']:.9g} J")
        return 0
    print(
        f"usage: {sys.argv[0]} [on|off|accum|stats-raw|bench [duration_s] [poll_s]]",
        file=sys.stderr,
    )
    return 2


def main() -> None:
    """Console-script entry point: runs the CLI and exits cleanly."""
    import os
    import sys
    try:
        rc = _cli_main()
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Explicit close before interpreter teardown — the C jsdrv thread can
    # be killed mid-syscall during module finalization. os._exit skips
    # finalization entirely.
    _direct.close()
    os._exit(rc)


if __name__ == "__main__":
    main()
