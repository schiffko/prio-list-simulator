"""
MCP server that lets a Claude session control a running simulator (main.py).

Thin wrapper around the simulator's control API (see API.md). Start main.py first,
then register this server with Claude Code:

    claude mcp add prio-list-simulator -- /path/to/venv/bin/python /path/to/mcp_server.py

Environment:
    SIM_API_URL   control API base URL (default http://127.0.0.1:8020)
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Literal

try:
    from mcp.server.mcpserver import MCPServer                 # mcp >= 2
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer        # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError

API_URL = os.environ.get("SIM_API_URL", "http://127.0.0.1:8020").rstrip("/")

mcp = MCPServer(
    "prio-list-simulator",
    instructions=(
        "Controls the EV charging station / Smartfox meter / Victron battery / debug heater Modbus simulator. "
        "The SmartFox firmware under test reads and writes the simulated devices over Modbus; "
        "these tools change the simulated world (cars, PV surplus, batteries, heaters, energy community) "
        "and observe how the "
        "firmware reacts. Grid power sign: + = import from grid, - = feed-in/surplus. "
        "The simulator updates once per second, so use sample_state to watch regulation over time."
    ),
)


def _call(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req  = urllib.request.Request(f"{API_URL}{path}", data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", str(e))
        except Exception:
            message = str(e)
        raise ToolError(f"Simulator API {e.code}: {message}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"Simulator not reachable at {API_URL} ({e.reason}). Is main.py running?") from None


def _patch(path: str, **fields):
    return _call("PATCH", path, {k: v for k, v in fields.items() if v is not None})


# ---- overview ----

@mcp.tool()
def get_state() -> dict:
    """Full simulator snapshot: grid/surplus, energy meter, stations, heaters, batteries, polling, community."""
    return _call("GET", "/api/state")


@mcp.tool()
def sample_state(duration_s: int = 10, interval_s: float = 1.0) -> list[dict]:
    """Watch the simulation over time (e.g. how the firmware regulates after a change).

    Returns compact samples: grid total, per-station power/current/set-current/status and
    per-battery setpoint/actual power/SoC. duration_s max 120.
    """
    duration_s = max(1, min(duration_s, 120))
    interval_s = max(0.5, interval_s)
    samples, t0 = [], time.monotonic()
    while True:
        s = _call("GET", "/api/state")
        samples.append({
            "t": round(time.monotonic() - t0, 1),
            "grid_total_w": s["grid"]["grid_total_w"],
            "stations": [
                {"id": st["id"], "status": st["status"], "power_kw": st["power_kw"],
                 "current_a": st["current_a"], "set_current_ma": st["set_current_ma"],
                 "phases": st["phases"], "vehicle_phases": st["vehicle_phases"]}
                for st in s["stations"]
            ],
            "heaters": [
                {"slot": h["slot"], "state": h["state"], "setpoint_w": h["setpoint_w"],
                 "power_w": h["power_w"], "temperature_c": h["temperature_c"]}
                for h in s["heaters"]
            ],
            "batteries": [
                {"id": b["id"], "setpoint_w": b["setpoint_w"], "actual_power_w": b["actual_power_w"], "soc": b["soc"]}
                for b in s["batteries"]
            ],
        })
        if time.monotonic() - t0 + interval_s > duration_s:
            return samples
        time.sleep(interval_s)


# ---- charging stations ----

@mcp.tool()
def add_stations(count: int = 1) -> list[dict]:
    """Add charging stations (IDs continue after the existing ones; Modbus unit-ID = station ID)."""
    return _call("POST", "/api/stations", {"count": count})


@mcp.tool()
def clear_stations() -> dict:
    """Remove all charging stations."""
    return _call("DELETE", "/api/stations")


@mcp.tool()
def plug_in(station_id: int) -> dict:
    """Plug a car into a station (status Connected; starts charging once the firmware sets a current)."""
    return _call("POST", f"/api/stations/{station_id}/plug_in", {})


@mcp.tool()
def plug_out(station_id: int) -> dict:
    """Unplug the car from a station (status Unplugged, also clears an error state)."""
    return _call("POST", f"/api/stations/{station_id}/plug_out", {})


@mcp.tool()
def set_station_error(station_id: int, error_code: int | None = None) -> dict:
    """Put a station into the Error state, optionally with an error code register value."""
    body = {} if error_code is None else {"error_code": error_code}
    return _call("POST", f"/api/stations/{station_id}/error", body)


@mcp.tool()
def update_station(
    station_id: int,
    status_code: int | None = None,
    max_power_kw: float | None = None,
    amperage_a: float | None = None,
    manual_amperage: bool | None = None,
    error_code: int | None = None,
    vehicle_phases: Literal[1, 2, 3] | None = None,
) -> dict:
    """Change station settings. Only given fields are changed.

    vehicle_phases: phases the car can charge with; the station delivers min(phases, vehicle_phases).

    status_code: 0 Unplugged, 1 Disconnected, 2 Connected, 3 Charging, 4 Error, 5 Unauthorized, 6 Stop.
    amperage_a: force the charging current (0-32 A); enables manual override so firmware writes are ignored.
    manual_amperage: false hands current control back to the firmware's set-current register.
    """
    return _patch(f"/api/stations/{station_id}", status_code=status_code, max_power_kw=max_power_kw,
                  amperage_a=amperage_a, manual_amperage=manual_amperage, error_code=error_code,
                  vehicle_phases=vehicle_phases)


# ---- grid / meter ----

@mcp.tool()
def set_grid(
    surplus_w: float | None = None,
    noise_enabled: bool | None = None,
    noise_amplitude_w: int | None = None,
) -> dict:
    """Set the base grid power and load noise.

    surplus_w: base household grid power in W before chargers/batteries (negative = PV surplus
    feeding into the grid, positive = import). The firmware sees base + chargers + batteries + noise.
    """
    return _patch("/api/grid", surplus_w=surplus_w, noise_enabled=noise_enabled,
                  noise_amplitude_w=noise_amplitude_w)


@mcp.tool()
def reset_meter_energy(forward: bool = True, reverse: bool = True) -> dict:
    """Reset the energy meter's forward and/or reverse Wh counters."""
    return _call("POST", "/api/meter/reset", {"forward": forward, "reverse": reverse})


# ---- batteries ----

@mcp.tool()
def add_battery(
    soc: float | None = None,
    capacity_kwh: float | None = None,
    temperature_c: float | None = None,
    max_charge_w: int | None = None,
    max_discharge_w: int | None = None,
    ramp_w_per_s: int | None = None,
    delay_s: int | None = None,
    read_interval_s: int | None = None,
    phases: Literal[1, 3] | None = None,
    local_ip: str | None = None,
) -> dict:
    """Add a Victron battery (answers on Modbus unit 0/242/246/227), optionally with initial settings."""
    body = {k: v for k, v in locals().items() if v is not None}
    return _call("POST", "/api/batteries", body)


@mcp.tool()
def clear_batteries() -> dict:
    """Remove all batteries."""
    return _call("DELETE", "/api/batteries")


@mcp.tool()
def update_battery(
    battery_id: int,
    soc: float | None = None,
    capacity_kwh: float | None = None,
    temperature_c: float | None = None,
    max_charge_w: int | None = None,
    max_discharge_w: int | None = None,
    ramp_w_per_s: int | None = None,
    delay_s: int | None = None,
    read_interval_s: int | None = None,
    phases: Literal[1, 3] | None = None,
    local_ip: str | None = None,
) -> dict:
    """Change battery settings. Only given fields are changed.

    read_interval_s (0-60, 0 = off): power reads inside it get a Modbus exception, so the firmware reads
    and writes the battery only that often (slow real device).
    phases must match the firmware's three-phase battery setting. local_ip: empty string = answer
    on any local IP (several batteries need one IP alias each).
    """
    fields = {k: v for k, v in locals().items() if k != "battery_id"}
    return _patch(f"/api/batteries/{battery_id}", **fields)


# ---- heaters (debug heater, ht_debug.c, unit-ID 210 + slot) ----

@mcp.tool()
def update_heater(
    slot: int,
    nominal_power_w: int | None = None,
    temperature_c: float | None = None,
    max_temp_c: float | None = None,
    auto_temperature: bool | None = None,
    force_max_temp: bool | None = None,
) -> dict:
    """Change a debug heater (slot 0-2, Modbus unit-ID 210 + slot). Only given fields are changed.

    The firmware writes the power setpoint; the heater follows it with 500 W/s up to nominal_power_w.
    auto_temperature: simulate the boiler temperature from the power. force_max_temp: report state 2
    (max. temperature) and switch off. The returned object shows state, setpoint_w and power_w.
    """
    return _patch(f"/api/heaters/{slot}", nominal_power_w=nominal_power_w, temperature_c=temperature_c,
                  max_temp_c=max_temp_c, auto_temperature=auto_temperature, force_max_temp=force_max_temp)


# ---- energy community ----

@mcp.tool()
def update_community(
    enabled: bool | None = None,
    others_w: float | None = None,
    key: float | None = None,
) -> dict:
    """Energy community (EVG) feed: while enabled the simulator POSTs the community values to the Smartfox
    every 10 s (needs a debug-build firmware).

    others_w: grid power of the other members (negative = feed-in). key: share 0..1. The result shows the
    expected share (expected_share_w = -key * others_w) the firmware should settle on.
    """
    return _patch("/api/community", enabled=enabled, others_w=others_w, key=key)


# ---- Smartfox under test ----

@mcp.tool()
def set_smartfox_ip(ip: str) -> dict:
    """Set the IP of the Smartfox under test (used by configure_smartfox, polling and the community feed)."""
    return _patch("/api/smartfox", ip=ip)


@mcp.tool()
def configure_smartfox(
    device: Literal["all", "meter", "battery", "station", "heater"] = "all",
    id: int | None = None,
    remove: bool = False,
) -> dict:
    """Write the device configuration to the Smartfox (same as the 'Konfigurieren' / 'Entfernen' buttons).

    WARNING: every call saves settings in the flash of the real Smartfox. id = station ID, heater slot or
    battery ID (not needed for "all" and "meter"). The result only says the request was started; read
    config_status in get_state for the outcome.
    """
    body = {"device": device, "remove": remove}
    if id is not None:
        body["id"] = id
    return _call("POST", "/api/configure", body)


# ---- device polling (real SmartFox JSON API as surplus source) ----

@mcp.tool()
def update_polling(
    enabled: bool | None = None,
    ip: str | None = None,
    interval_s: int | None = None,
    surplus_source: Literal["detailsPowerValue_neg", "toGridValue_kw", "manual"] | None = None,
    heater_in_surplus: bool | None = None,
) -> dict:
    """Configure polling of a real SmartFox device whose live grid power drives the simulated surplus.

    ip is the Smartfox-IP shared with configure_smartfox and the community feed.

    surplus_source "manual" means the polled values are only displayed, set_grid stays in control.
    """
    body = {k: v for k, v in locals().items() if v is not None}
    if body.get("surplus_source") == "manual":
        body["surplus_source"] = None
    return _call("PATCH", "/api/polling", body)


if __name__ == "__main__":
    mcp.run()
