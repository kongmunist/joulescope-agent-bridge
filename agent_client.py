"""Backwards-compat shim — the canonical module is `joulescope_agent_bridge`.

Existing scripts that do `from agent_client import read_voltage` keep working,
and `python agent_client.py` still runs the CLI.
"""

from joulescope_agent_bridge import (  # noqa: F401
    BridgeError,
    accumulator_delta,
    avg_1s,
    benchmark_accumulators,
    device,
    main,
    read_accumulators,
    read_current,
    read_power,
    read_voltage,
    set_power,
)

if __name__ == "__main__":
    main()
