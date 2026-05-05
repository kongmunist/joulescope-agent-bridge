"""Client helpers for the Joulescope agent bridge plugin.

Talks to the localhost JSON-line socket served by the UI plugin in
`joulescope_agent/plugin/`. Use these from any Python process while the
Joulescope UI keeps the JS220 open.

    from agent_client import (
        read_voltage, read_current, read_power, avg_1s, set_power,
    )

    print(read_voltage(), "V")
    set_power(False)
    print(avg_1s())
"""

from __future__ import annotations

import json
import socket
from typing import Any

HOST = "127.0.0.1"
PORT = 9876
TIMEOUT_S = 2.0


class BridgeError(RuntimeError):
    """Raised when the bridge plugin returns an error or is unreachable."""


def _request(payload: dict) -> dict:
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT_S) as s:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        # Read until newline.
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
    resp = json.loads(line.decode("utf-8"))
    if not resp.get("ok"):
        raise BridgeError(resp.get("error", "unknown bridge error"))
    return resp


def device() -> str:
    return _request({"cmd": "device"})["unique_id"]


def read_voltage() -> float:
    """Latest mean voltage in volts."""
    return _request({"cmd": "voltage"})["value"]


def read_current() -> float:
    """Latest mean current in amps."""
    return _request({"cmd": "current"})["value"]


def read_power() -> float:
    """Latest mean power in watts."""
    return _request({"cmd": "power"})["value"]


def avg_1s() -> dict[str, Any]:
    """Average V/I/P over the rolling 1 s window. Includes sample count `n`."""
    r = _request({"cmd": "stats_1s"})
    return {"n": r["n"], "voltage": r["voltage"], "current": r["current"], "power": r["power"]}


def set_power(on: bool) -> bool:
    """Toggle the JS220 high-side switch (target_power). Returns the bool that was set."""
    return _request({"cmd": "power_set", "on": bool(on)})["target_power"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
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
