---
name: Prio-List Simulator — full overview and how to extend it
description: What main.py of the prio-list-simulator does (chargers, heaters, Victron batteries, slave meter, energy community, device polling, control API), how the SmartFox firmware interacts with it, the surplus flow, and how to extend it
type: project
---
## What this program is

`main.py` is a **PyQt6 desktop simulator** for testing the SmartFox6 firmware (`EnergyCtrl.c`, priority list / regulation). It replaces real hardware with a software simulation so the firmware can be tested over the network:

| Simulated device | Firmware driver | Unit-ID | Port |
|---|---|---|---|
| EV chargers | `car_cc_debug.c` | station ID (1..) | 5020 |
| Debug heaters (3) | `ht_debug.c` (`VB_ID_DEBUG`) | 210 + slot | 5020 |
| Victron batteries | `bs_victron.c` | 0 (system), 242 / 246 / 227 (VE / CC / Cerbo) | 502 or 5020 |
| Slave energy meter | `em_smartfox_slave.c` | 1, registers ≥ 40000 | 502 or 5020 |
| PV surplus register | `car_cc_debug.c` | 100 | 5020 |

One combined Modbus TCP server listens on **5020 and 502** (502 only if the OS allows it). The SmartFox is the Modbus **client**; the simulator is the server.

The project was merged from two simulators: the prio-list-simulator (chargers with `vehicle_phases`, heaters, energy community, "Konfigurieren" over the Smartfox JSON API) and the cc-simulator (multi-battery model with delay/ramp/SoC, device polling, control API + MCP).

---

## Modbus dispatch (`CombinedModbusTCPServer._dispatch`)

1. unit-ID 0 or one of `VICTRON_DEVICE_UNITS` → `_dispatch_battery`. The battery is picked by the **local IP** the client connected to (`_get_battery`): a battery with that `local_ip`, else the first one without `local_ip`. No battery → Modbus exception 0x0B.
2. start address ≥ 40000 → energy meter.
3. otherwise `_read_charger` / `_write_charger`: heater (unit 210-212) → unit 100 surplus register → station by unit-ID.

### Chargers
Registers 0-11 (see `MODBUS_REGISTERS.md`). Register 11 = `vehicle_phases`; power uses `effective_phases = min(phases, vehicle_phases)`.

### Battery (`BatterySimulator`)
bs_victron.c writes a setpoint (signed W, + = charge) to registers 37/40/41. The simulator delays it (`delay_s`), ramps (`ramp_w_per_s`), clamps to `max_charge_w`/`max_discharge_w`, and integrates the SoC from `capacity_kwh`. With `phases == 3` the firmware writes the same value into all three registers, so the setpoint is the **sum**. Reads: unit 0 → SoC (843), Cerbo power (842); device units → voltage (26), current (27), temperature (61), setpoint echo.

### Heater (`HeaterSimulator`)
Register map in `README.md`. Power follows the written setpoint with 500 W/s up to `nominal_power_w`; optional temperature simulation with thermostat hysteresis; "Force max. temperature" reports state 2.

---

## Power / surplus data flow

```
[surplus_spin / polled device] → _base_surplus (W)
        +
[polled heater + analog-out offset]  (consumptionControl1-3 + analogOutPower, if checkbox checked)
        ↓
  surplus_power  (effective W)
        +
  chargers + heaters (consumer_total_w)  + batteries (actual_power_w) + noise_w
        ↓
  meter.total_power_w / phase_powers_w / phase_currents_ma   → energy meter registers
```

- `meter_total_w()` = what the meter reports. `grid_power_w()` = surplus + chargers + heaters = what **Unit-ID 100** reports (also used by the energy community; batteries and noise are not in it).
- Heaters are spread evenly on 3 phases; chargers on `effective_phases`; a 3-phase battery on 3 phases, a 1-phase battery on L1.
- `update_all()` runs every second: batteries (+ noise) → stations → heaters → meter energy → `_update_poll_display()` → `update_surplus_display()` → widgets → community tick.

---

## Two clients of the Smartfox JSON API (both use the **Smartfox-IP** field)

- `SmartfoxApi` + `SmartfoxConfigBar`: **writes** device configuration (`POST /api/devices/config/<type>/<slot>`) — buttons "Konfigurieren"/"Entfernen"/"Alle konfigurieren". Saves to the flash of the Smartfox! Battery N uses Smartfox battery slot N-1 (max 3), with the battery's `local_ip` if set.
- `DevicePoller` (tab "Device Polling"): **reads** `/api/liveview/data` and `/api/liveview/devices` every N s. Field names stay the old `values.xml` ids (`detailsPowerValue`, `toGridValue`, `cc1Power`, `consumptionControl1Power`, `batt1Power`, ...). Can drive the surplus (`surplus_source_combo`) and add heater/analog-out power to it. `poller.get()` → `(values, last_poll_time, last_error)`.
- `CommunityWidget`: POSTs `/api/debug/community/...` (debug-build firmware only).

### Adding a polled variable
1. Add the id to `DevicePoller.NUMERIC_FIELDS` / `STRING_FIELDS`.
2. Fill it in `DevicePoller._parse` from `data["meas"]` or `devices[<API_TYPE_*>]`.
3. Add a `("id", "Label")` row to `_display_fields` in `MainWindow.setup_ui()`.
4. If it drives the simulation, use it in `_update_poll_display()` (additive offsets: follow `_heater_offset_w`; they must be added last to `surplus_power`).

---

## Control API (see `API.md`)

`ControlApi` (port 8020) runs requests in the GUI thread via `MainWindow.run_in_gui`. `/api/...` is the REST API used by `mcp_server.py`; the old script routes (`/state`, `/station/<id>`, `POST /battery`, ...) are kept. To add a call: row in `ControlApi.ROUTES` + `api_*` method + tool in `mcp_server.py` + `API.md`. Validate with `_require` / `_reject_unknown`; change values through the widgets so the GUI stays in sync.

---

## Key classes

`ChargingStationSimulator`, `HeaterSimulator`, `SmartfoxSlaveSimulator`, `BatterySimulator`, `DevicePoller`, `CombinedModbusTCPServer`/`CombinedServerThread`, `SmartfoxApi`, `SmartfoxConfigBar`, `StationWidget`, `MeterWidget`, `BatteryWidget`, `HeaterWidget`, `CommunityWidget`, `ControlApi`, `MainWindow`.

## Verify after editing

```bash
python3 -m py_compile main.py mcp_server.py
```
A headless smoke test works with `QT_QPA_PLATFORM=offscreen` (instantiate `MainWindow`, call the API on 8020, read Modbus on 5020).
