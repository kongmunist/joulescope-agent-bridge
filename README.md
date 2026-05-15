# joulescope-agent-bridge

A Joulescope UI plugin + Python client that lets external scripts (and AI
agents) read live JS220 voltage/current/power and toggle the high-side power
rail **while the Joulescope UI keeps the device open**. No USB contention.

```
joulescope-agent-bridge/
├── plugin/                       ← copy or symlink into the UI's plugin path
│   ├── __init__.py               ← runs inside the UI; PubSub + JSON-line socket
│   ├── index.json
│   └── README.md                 ← install steps, wire protocol, caveats
├── joulescope_agent_bridge/      ← pip-installable Python client
│   ├── __init__.py
│   └── __main__.py
├── agent_client.py               ← backwards-compat shim
├── pyproject.toml
├── LICENSE
└── README.md
```

## Why a plugin

The JS220 USB device only allows one host process to claim it. The Joulescope
UI is that process. A separate Python script trying to call `joulescope.scan()`
or `pyjoulescope_driver.open()` while the UI has the device open fails with a
device-claim error. This plugin runs **inside** the UI process, so it shares
the existing claim — it subscribes to the UI's PubSub bus for live stats and
publishes settings to control the rail. It then exposes those primitives over
a localhost socket so any external process can drive them.

## Install

### Plugin (required for the bridge backend)

Symlink (or copy) `plugin/` into the UI's plugin directory:

```sh
# macOS
ln -s "$PWD/plugin" "$HOME/Library/Application Support/joulescope/plugins/agent_bridge"
# Linux
ln -s "$PWD/plugin" "$HOME/.config/joulescope/plugins/agent_bridge"
# Windows (PowerShell)
New-Item -ItemType SymbolicLink `
  -Path  "$env:APPDATA\joulescope\plugins\agent_bridge" `
  -Value "$PWD\plugin"
```

Then restart the Joulescope UI, **File → Plugins** → enable **Agent Bridge**,
and drop the **Agent Bridge** widget into your layout.

### Client (Python)

```sh
# bridge mode only (no extra deps; just stdlib sockets)
pip install joulescope-agent-bridge

# with direct-USB fallback for when the UI is closed
pip install joulescope-agent-bridge[direct]
```

The `[direct]` extra pulls in `pyjoulescope_driver` for the no-UI path. On
Homebrew Python you may need `--break-system-packages` or `--user`. Until
this is on PyPI, install from the checkout: `pip install -e .[direct]`.

After install you get:

- `import joulescope_agent_bridge as jab` — Python API
- `joulescope-bridge` — CLI command
- `python -m joulescope_agent_bridge` — equivalent to the CLI
- `agent_client.py` — backwards-compat shim still works (`python agent_client.py`)

## Quickstart

1. Install the plugin (above) and connect your JS220 in the UI.
2. From any terminal:
   ```sh
   joulescope-bridge            # prints device id, V, I, P, 1 s avg
   joulescope-bridge off        # cuts the rail
   joulescope-bridge on         # restores it
   ```

The plugin's own [README](plugin/README.md) has all install paths and the
full wire protocol.

## For AI agents / automation

If you (an AI coding agent or a CI script) need to monitor a JS220 from
Python, this is the contract. The client transparently uses two backends:

- **Bridge backend** (preferred): talks to the UI plugin's localhost socket.
  Works while the human keeps the Joulescope UI open — no USB contention.
- **Direct backend** (fallback): when the UI/plugin isn't running, the
  client opens the JS220 itself via `pyjoulescope_driver`. Install it with
  `pip install pyjoulescope_driver` — only needed if you expect to run
  without the UI.

The client tries the bridge first on every call, falls back to direct on
`ConnectionRefusedError`, and **self-recovers**: a watcher thread polls the
bridge every 2 s while direct is open, and closes the direct claim as soon
as the UI comes back so the human's UI can attach to the device again.

### Preconditions

- A JS220 is connected.
- For the bridge backend: Joulescope UI is running with the **Agent Bridge**
  plugin enabled and its widget visible. Widget shows `device: JS220-XXXXXX`
  and a non-zero event counter.
- For the direct backend: `pip install joulescope` available in the
  current Python environment.

### Python API (preferred)

```python
from joulescope_agent_bridge import (
    device, read_voltage, read_current, read_power, avg_1s,
    read_accumulators, accumulator_delta, set_power,
    BridgeError,
)

device()           # -> "JS220-XXXXXX"
read_voltage()     # -> 3.7991  (volts)
read_current()     # -> 1.7e-06 (amps; signed, source-positive)
read_power()       # -> 6.4e-06 (watts)
avg_1s()           # -> {"n": 2, "voltage": 3.7991, "current": 1.6e-06, "power": 6.2e-06}
read_accumulators()
# -> {"sample_time_s": ..., "charge_c": ..., "energy_j": ..., "voltage": ..., "current": ..., "power": ...}
set_power(False)   # cut the DUT rail; returns False
set_power(True)    # restore the rail; returns True
```

`BridgeError` is raised when the bridge returns an error response — most
commonly `"no samples yet"` immediately after a device attaches. Catch it
when polling around device-state transitions. `ConnectionRefusedError`
(stdlib) is raised when the UI/plugin isn't running.

### Raw protocol (any language)

One JSON object per line, request and response, on `127.0.0.1:9876`:

```
-> {"cmd":"voltage"}
<- {"ok":true,"value":5.12,"unit":"V"}

-> {"cmd":"power_set","on":false}
<- {"ok":true,"target_power":false}

-> {"cmd":"accumulators"}
<- {"ok":true,"charge_c":0.00123,"energy_j":0.00492,"sample_time_s":12345.6,...}
```

Full table of commands in [`plugin/README.md`](plugin/README.md).

### Behavioral notes for agents

- **Latency.** The plugin samples at the UI's statistics rate (default 1 Hz
  on JS220, configurable in the JS220's settings widget up to 10 Hz). `read_*`
  returns the most recent sample; expect 0–1 s of staleness. After
  `set_power`, wait at least `1 / statistics_frequency` seconds before
  reading the new state, plus DUT settling time (typical 200–500 ms).
- **`avg_1s().n`.** At 1 Hz statistics this returns `n = 1` and is therefore
  not a meaningful average. Raise `statistics_frequency` to 2–10 Hz in the
  UI before relying on `avg_1s`.
- **Long-running averages.** Prefer `read_accumulators()` snapshots over
  repeated `avg_1s()` polling. Joulescope publishes cumulative charge and
  energy; take two snapshots and compute `delta_charge / elapsed` for average
  current and `delta_energy / elapsed` for average power. If the UI's
  accumulators are cleared during a run, discard that run.
- **Toolbar power button cosmetic.** `set_power` cuts/restores the rail
  (verifiable via `read_voltage` ≈ 0 V) but does not visually flip the UI's
  top-left toolbar power button. The button writes to a separate app-level
  topic the plugin doesn't mirror. If a human needs the button state to
  match, instruct them to click it once to resync.
- **Single device.** The plugin attaches to one JS220 at a time. If multiple
  are connected, a human picks via the widget's combo box. The client always
  talks to whichever is currently attached — call `device()` first if you
  care which physical instrument you're driving.
- **USB claim is one or the other.** Bridge backend never touches USB
  directly — the UI owns it. Direct backend owns USB itself, and while it's
  open the UI cannot attach. The watcher handles the handoff: bring the UI
  back up, and within ~2 s the client releases the device for the UI.
  During that window, the UI may briefly show "no device" — that's normal.
- **First direct-mode call is slow.** USB enumerate + waiting for the first
  statistics sample adds up to ~1–3 s. Subsequent calls reuse the open
  device and are fast.
- **Failure modes.** Bridge unreachable AND no JS220 connected → client
  raises `BridgeError("no JS220 found and Joulescope UI not running")`.
  Bridge unreachable AND `joulescope` package missing → `BridgeError` with
  a `pip install joulescope` hint.

### Recipe: measure quiescent current with the rail toggled

```python
import time
from joulescope_agent_bridge import set_power, avg_1s

set_power(True)
time.sleep(1.0)               # let DUT boot / settle
on = avg_1s()

set_power(False)
time.sleep(1.0)
off = avg_1s()

set_power(True)               # always restore
print("delta_I =", on["current"] - off["current"], "A")
```

### Recipe: compare 1 s polling against accumulators

```sh
joulescope-bridge bench 60 1
```

This samples the old `avg_1s()` path once per second for 60 seconds and
compares it against the Joulescope accumulator delta over the same interval.

## License

MIT.
