# Control API & MCP Server

The simulator can be controlled by a Claude session (or any script). There are two layers:

```
Claude ──MCP (stdio)──> mcp_server.py ──HTTP/JSON──> main.py control API ──> simulated devices
                                                              ▲
                              SmartFox firmware ──Modbus TCP──┘
```

- **Control API**: built into `main.py`, `http://127.0.0.1:8020` (localhost only, constant
  `CONTROL_API_PORT`). It uses only the stdlib.
- **MCP server**: `mcp_server.py`, a thin wrapper that turns the REST API (`/api/...`) into tools for
  Claude. Needs `pip install -r requirements-mcp.txt`. The Windows build doesn't need it.

Every API call runs on the Qt main thread (`MainWindow.run_in_gui`) and goes through the GUI widgets, so
the window always shows what the API set.

The older script API (`GET /state`, `POST /surplus`, `POST /station/<id>`, ...) still works, see the
docstring of `ControlApi` in `main.py` and the README. New code should use `/api/...`.

## Setup

```bash
venv/bin/pip install -r requirements-mcp.txt
python main.py                        # simulator must be running
claude mcp add prio-list-simulator -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

If the API runs on a different port: `claude mcp add prio-list-simulator -e SIM_API_URL=http://127.0.0.1:9000 -- ...`

## MCP tools

| Tool | Parameters | What it does |
|------|------------|--------------|
| `get_state` | none | Full snapshot: grid, meter, stations, heaters, batteries, polling, community, Smartfox |
| `sample_state` | `duration_s`=10 (max 120), `interval_s`=1 | Time series of grid total, stations, heaters, batteries, for watching how the firmware regulates |
| `add_stations` | `count`=1 | Add stations (IDs continue after the existing ones, unit-ID = station ID) |
| `clear_stations` | none | Remove all stations |
| `plug_in` | `station_id` | Car plugged in, status Connected |
| `plug_out` | `station_id` | Car unplugged (also clears Error) |
| `set_station_error` | `station_id`, `error_code`? | Status Error, optionally with an error-code register value |
| `update_station` | `station_id`, `status_code`?, `max_power_kw`?, `amperage_a`?, `manual_amperage`?, `error_code`?, `vehicle_phases`? | Change station settings (`amperage_a` turns manual override on; `vehicle_phases` 1-3 limits the phases the car uses) |
| `update_heater` | `slot` (0-2), `nominal_power_w`?, `temperature_c`?, `max_temp_c`?, `auto_temperature`?, `force_max_temp`? | Change a debug heater (unit-ID 210 + slot) |
| `set_grid` | `surplus_w`?, `noise_enabled`?, `noise_amplitude_w`? | Base grid power (neg = PV surplus) and load noise |
| `reset_meter_energy` | `forward`=true, `reverse`=true | Reset the meter's Wh counters |
| `add_battery` | `soc`?, `capacity_kwh`?, `temperature_c`?, `max_charge_w`?, `max_discharge_w`?, `ramp_w_per_s`?, `delay_s`?, `read_interval_s`?, `phases`?, `local_ip`? | Add a Victron battery with optional settings |
| `clear_batteries` | none | Remove all batteries |
| `update_battery` | `battery_id` + same fields as `add_battery` | Change battery settings |
| `update_community` | `enabled`?, `others_w`?, `key`? | Energy community (EVG) feed to the Smartfox |
| `set_smartfox_ip` | `ip` | Smartfox under test |
| `configure_smartfox` | `device`="all" (`meter`/`battery`/`station`/`heater`), `id`?, `remove`=false | Write the device configuration to the Smartfox (**saves to its flash**) |
| `update_polling` | `enabled`?, `ip`?, `interval_s`?, `surplus_source`?, `heater_in_surplus`? | Use the real Smartfox (JSON API) as the surplus source |

`?` = optional; only the fields you pass get changed.

## HTTP endpoints

All bodies are JSON objects. Unknown fields or values outside the GUI's range return `400`
with an `{"error": ...}` message.

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/api/state` | none | `{modbus_ports, sim_ip, grid, meter, stations[], heaters[], batteries[], polling, community, smartfox}` |
| GET | `/api/stations` | none | list of stations |
| POST | `/api/stations` | `{count: 1..16}` | the new stations |
| DELETE | `/api/stations` | none | `{ok}` |
| GET | `/api/stations/{id}` | none | station |
| PATCH | `/api/stations/{id}` | `status_code` (0-6), `max_power_kw` (1-22), `amperage_a` (0-32), `manual_amperage` (bool), `error_code` (0-65535), `vehicle_phases` (1-3) | station |
| POST | `/api/stations/{id}/plug_in` | none | station (`409` if in Error) |
| POST | `/api/stations/{id}/plug_out` | none | station |
| POST | `/api/stations/{id}/error` | `error_code`? | station |
| GET | `/api/heaters` | none | list of heaters |
| GET | `/api/heaters/{slot}` | none | heater |
| PATCH | `/api/heaters/{slot}` | `nominal_power_w` (0-30000), `temperature_c` (0-99), `max_temp_c` (20-99), `auto_temperature`, `force_max_temp` | heater |
| GET | `/api/grid` | none | grid |
| PATCH | `/api/grid` | `surplus_w` (-32768..32767), `noise_enabled`, `noise_amplitude_w` (0-10000) | grid (`409` for `surplus_w` while polling drives the surplus) |
| GET | `/api/meter` | none | meter |
| POST | `/api/meter/reset` | `forward`?, `reverse`? (default both) | meter |
| GET | `/api/batteries` | none | list of batteries |
| POST | `/api/batteries` | optional battery fields | the new battery |
| DELETE | `/api/batteries` | none | `{ok}` |
| GET | `/api/batteries/{id}` | none | battery |
| PATCH | `/api/batteries/{id}` | `soc` (0-100), `capacity_kwh` (0.1-200), `temperature_c` (-40..100), `max_charge_w` / `max_discharge_w` (0-30000), `ramp_w_per_s` (1-30000), `delay_s` (0-30), `read_interval_s` (0-60, 0 = off: power reads inside it get Modbus exception 6, so the firmware reads and writes the battery only that often), `phases` (1/3), `local_ip` | battery |
| GET | `/api/polling` | none | polling config + last polled values |
| PATCH | `/api/polling` | `enabled`, `ip`, `interval_s` (1-60), `surplus_source` (`null` / `detailsPowerValue_neg` / `toGridValue_kw`), `heater_in_surplus` | polling (`409` when changing ip/interval while enabled) |
| GET | `/api/community` | none | community |
| PATCH | `/api/community` | `enabled`, `others_w` (±100000), `key` (0-1) | community |
| GET | `/api/smartfox` | none | `{smartfox_ip, sim_ip}` |
| PATCH | `/api/smartfox` | `ip` | `{smartfox_ip, sim_ip}` |
| POST | `/api/configure` | `device` (`all`/`meter`/`battery`/`station`/`heater`), `id`?, `remove`? | `{ok, device, id}`, result in `config_status` of the device |

Quick test:

```bash
curl -s localhost:8020/api/state
curl -s -X POST localhost:8020/api/stations/1/plug_in
curl -s -X PATCH localhost:8020/api/grid -H 'Content-Type: application/json' -d '{"surplus_w": -5000}'
```

### Response objects

- **station**: `id, status_code, status, is_charging, power_kw, current_a, target_current_a,
  set_current_ma, phases, vehicle_phases, effective_phases, voltage_v, max_power_kw, manual_amperage,
  error_code, energy_kwh, session_duration_s, last_request{time,type}`
- **heater**: `slot, unit_id, nominal_power_w, setpoint_w, power_w, state (Ready/Heating/Max Temp),
  temperature_c, max_temp_c, auto_temperature, force_max_temp, energy_wh, last_request{time,type},
  config_status`
- **grid**: `surplus_input_w, surplus_effective_w (incl. polled heater/DAC offset), grid_total_w
  (what the meter reports, + = import), unit100_w (what Unit-ID 100 reports: surplus + chargers +
  heaters), charger_total_w, heater_total_w, battery_total_w, noise_enabled, noise_amplitude_w, noise_w`
- **meter**: `total_power_w, phase_powers_w[3], phase_currents_ma[3], energy_forward_wh,
  energy_reverse_wh, smartfox_slot, last_request{time,type}, config_status`
- **battery**: `id, local_ip, phases, capacity_kwh, soc, max_charge_w, max_discharge_w,
  ramp_w_per_s, delay_s, read_interval_s, temperature_c, setpoint_w, setpoint_regs[3], actual_power_w,
  last_request{time,type}`
- **community**: `enabled, others_w, key, own_delayed_w, community_power, my_surplus,
  expected_share_w, last_result`

## Not exposed yet (candidates to add)

- Removing a single station or battery (only "clear all" exists, same as the GUI)
- Station `voltage`
- Setting meter phase values directly (they are always derived from surplus + chargers + heaters + batteries)
- Modbus request log / history (only the last request per device is kept)
- Pausing the simulation or changing the 1 s tick rate
- Saving/loading a scenario (a whole state as JSON)

## Adding or removing a call

1. `main.py`: add or remove a row in `ControlApi.ROUTES` and the matching `api_*` method on
   `ControlApi`. GET handlers take only the path IDs; other methods also get `body`.
2. `mcp_server.py`: add or remove the `@mcp.tool()` function that calls it. The docstring is
   what Claude reads to decide when to use the tool.
3. Update this file.
