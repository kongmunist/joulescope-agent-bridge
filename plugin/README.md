# Joulescope Agent Bridge

A Joulescope UI plugin that exposes a JS220's live measurements and the
target-power toggle on a localhost JSON-line socket. Lets external scripts
(automation, AI agents, dashboards) read voltage/current/power and cut/restore
DUT power **while the Joulescope UI keeps the device open** — no USB
contention.

## Why a plugin

The JS220 USB device only allows one host process to claim it. The Joulescope
UI is that process. A separate Python script trying to call `joulescope.scan()`
or `pyjoulescope_driver.open()` while the UI has the device open fails. This
plugin runs **inside** the UI process, so it shares the existing claim — it
subscribes to the UI's PubSub bus for live stats and publishes settings to
control the rail.

## Install

Copy or symlink this directory into the UI's plugin path:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/joulescope/plugins/agent_bridge` |
| Linux | `~/.config/joulescope/plugins/agent_bridge` |
| Windows | `%APPDATA%\joulescope\plugins\agent_bridge` |

Then:

1. Restart the Joulescope UI.
2. **File → Plugins** — enable **Agent Bridge**.
3. **Widgets → Agent Bridge** — drop the widget anywhere in the layout. The
   widget instantiates the plugin and shows a status line; closing it stops
   the socket.
4. Connect a JS220 in the UI as normal. The widget auto-attaches; if it
   doesn't, click **Refresh** and then **Attach**.

## Wire protocol

Newline-delimited JSON, one request and one response per line, on
`127.0.0.1:9876`:

| Request | Response |
|---|---|
| `{"cmd":"device"}` | `{"ok":true,"unique_id":"JS220-XXXXXX"}` |
| `{"cmd":"voltage"}` | `{"ok":true,"value":5.12,"unit":"V"}` |
| `{"cmd":"current"}` | `{"ok":true,"value":0.0123,"unit":"A"}` |
| `{"cmd":"power"}` | `{"ok":true,"value":0.063,"unit":"W"}` |
| `{"cmd":"stats_1s"}` | `{"ok":true,"n":2,"voltage":...,"current":...,"power":...}` |
| `{"cmd":"power_set","on":true\|false}` | `{"ok":true,"target_power":true\|false}` |

`stats_1s` averages the rolling 1 s window. The JS220's default UI statistics
rate is 1–2 Hz, so expect `n` ≈ 1–2; raise it via the JS220's
`statistics_frequency` setting if you need finer resolution.

A reference Python client lives at `agent_client.py` in the parent directory.

## Notes and caveats

- **JS220 only.** The plugin matches device unique-ids beginning with
  `JS220-`. Adapting it for the JS110 means changing one constant
  (`DEVICE_PREFIX_MATCH`) and verifying the legacy device's stats payload
  shape.
- **Toolbar power button does not flip.** `power_set` writes to the device's
  `settings/target_power` topic, which actually cuts the rail. The UI's
  toolbar power button writes to a separate app-level topic that is dispatched
  through a path the plugin can't easily mirror. The button stays whatever it
  was last clicked to; the rail is whatever this plugin set it to. If they
  drift, click the toolbar button to resync.
- **Loopback only.** The socket binds to `127.0.0.1`. Treat as
  trusted-local-user — the plugin runs with the UI's permissions.
- **Multiple JS220s.** The widget's combo box lists every JS220 it discovers.
  Only the currently-attached one is exposed via the socket.
