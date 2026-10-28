"""
EV Charging Station + Smartfox Slave Energy Meter + Victron Battery + Heater Simulator

Charger simulation  (port 5020, unit-IDs = station IDs):
  Implements the register map expected by car_cc_debug.c

Heater simulation  (port 5020, unit-IDs 210-212 = heater slot 0-2):
  Implements the register map expected by ht_debug.c (VB_ID_DEBUG)

Battery simulation  (port 502 or 5020, unit-IDs 0 / 242 / 246 / 227):
  Victron VE / CC / Cerbo register map of bs_victron.c, several batteries via local IP

Device polling: reads the JSON API of the real Smartfox (/api/liveview/*) as surplus source
Control API: http://127.0.0.1:8020 (see API.md, mcp_server.py)

Energy meter simulation  (port 502 or 5020, unit-ID 1):
  Implements the register map read by em_smartfox_slave.c

  Register map (PDU 0-indexed = Modbus address - 1):
    40999  4 regs  int64   energy forward  (Wh)
    41003  4 regs  uint64  energy reverse  (Wh)
    41017  2 regs  int32   total power     (W)
    41019  6 regs  3×int32 phase powers    (W)
    41028  6 regs  3×int32 phase currents  (mA)
"""

import sys
import socket
import struct
import asyncio
import threading
import urllib.request
import urllib.error
import json
import queue
import random
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSpinBox, QGroupBox, QScrollArea,
    QGridLayout, QDoubleSpinBox, QComboBox, QCheckBox, QTabWidget, QLineEdit
)
from PyQt6.QtCore import QTimer, Qt


# ---------------------------------------------------------------------------
# Charger simulator (unchanged)
# ---------------------------------------------------------------------------

class ChargingStationSimulator:
    """Simulates a single EV charging station with Modbus TCP interface"""

    STATUS_UNPLUGGED    = 0
    STATUS_DISCONNECTED = 1
    STATUS_CONNECTED    = 2
    STATUS_CHARGING     = 3
    STATUS_ERROR        = 4
    STATUS_UNAUTHORIZED = 5
    STATUS_STOP         = 6

    STATUS_NAMES = {
        0: "Unplugged",
        1: "Disconnected",
        2: "Connected",
        3: "Charging",
        4: "Error",
        5: "Unauthorized",
        6: "Stop"
    }

    def __init__(self, station_id: int):
        self.station_id       = station_id
        self.is_charging      = False
        self.current_power    = 0.0
        self.max_power        = 22.0
        self.current_amperage = 0.0
        self.target_amperage  = 0.0
        self.max_amperage     = 32.0
        self.voltage          = 230.0
        self.phases           = 3
        self.vehicle_phases   = 3
        self.energy_delivered = 0.0
        self.session_duration = 0
        self.status_code      = 0
        self.error_code       = 0
        self.set_current_ma   = 0
        self.manual_amperage  = False
        self.registers        = [0] * 20
        self.last_request_time = "Never"
        self.last_request_type = "None"

    @property
    def status(self):
        return self.STATUS_NAMES.get(self.status_code, "Unknown")

    @property
    def effective_phases(self):
        return min(self.phases, self.vehicle_phases)

    def get_modbus_data(self) -> list:
        self.registers[0]  = self.status_code
        self.registers[1]  = int(self.current_power * 10)
        self.registers[2]  = int(self.voltage * 10)
        self.registers[3]  = int(self.current_amperage * 10)
        self.registers[4]  = int(self.energy_delivered * 100) & 0xFFFF
        self.registers[5]  = (int(self.energy_delivered * 100) >> 16) & 0xFFFF
        self.registers[6]  = int(self.max_power * 10)
        self.registers[7]  = self.error_code
        self.registers[8]  = self.set_current_ma & 0xFFFF
        self.registers[9]  = (self.set_current_ma >> 16) & 0xFFFF
        self.registers[10] = self.phases
        self.registers[11] = self.vehicle_phases
        return self.registers

    def write_register(self, address: int, value: int) -> bool:
        if address < 0 or address >= len(self.registers):
            return False
        if address == 8:
            self.set_current_ma = (self.set_current_ma & 0xFFFF0000) | (value & 0xFFFF)
        elif address == 9:
            self.set_current_ma = (self.set_current_ma & 0xFFFF) | ((value & 0xFFFF) << 16)
        elif address == 10:
            if value in [1, 3]:
                self.phases = value
                self.registers[10] = value
                return True
            return False
        else:
            self.registers[address] = value
        self._apply_set_current()
        return True

    def _apply_set_current(self):
        if self.manual_amperage:
            return
        amps = self.set_current_ma / 1000.0
        self.set_amperage(amps)
        self._auto_transition()

    def set_status(self, status_code: int):
        self.status_code = status_code
        if status_code in (self.STATUS_CHARGING, self.STATUS_CONNECTED):
            if self.set_current_ma > 0:
                self.is_charging     = True
                self.target_amperage = self.set_current_ma / 1000.0
            else:
                self.is_charging      = False
                self.target_amperage  = 0.0
                self.current_amperage = 0.0
                self.current_power    = 0.0
        else:
            self.is_charging      = False
            self.current_power    = 0.0
            self.current_amperage = 0.0
            self.target_amperage  = 0.0
            self.energy_delivered = 0.0

    def plug_in(self):  self.set_status(self.STATUS_CONNECTED)
    def plug_out(self): self.set_status(self.STATUS_UNPLUGGED)
    def set_error(self): self.set_status(self.STATUS_ERROR)

    def set_amperage(self, amps: float):
        self.target_amperage = min(amps, self.max_amperage)

    def _auto_transition(self):
        if self.status_code not in (self.STATUS_CONNECTED, self.STATUS_CHARGING):
            return
        if self.set_current_ma > 0 and not self.is_charging:
            self.is_charging     = True
            self.target_amperage = self.set_current_ma / 1000.0
            self.status_code     = self.STATUS_CHARGING
        elif self.set_current_ma == 0 and self.is_charging:
            self.is_charging      = False
            self.target_amperage  = 0.0
            self.current_amperage = 0.0
            self.current_power    = 0.0
            self.status_code      = self.STATUS_CONNECTED

    def update(self):
        if self.is_charging:
            self.session_duration += 1
            if self.current_amperage != self.target_amperage:
                diff = self.target_amperage - self.current_amperage
                step = 1.0 if diff > 0 else -1.0
                if abs(diff) <= abs(step):
                    self.current_amperage = self.target_amperage
                else:
                    self.current_amperage += step
            self.current_power     = self.current_amperage * self.voltage * self.effective_phases / 1000
            self.energy_delivered += (self.current_power / 3600)
            if self.current_amperage == 0.0 and self.target_amperage == 0.0:
                self.is_charging   = False
                self.current_power = 0.0
                self.status_code   = self.STATUS_CONNECTED


# ---------------------------------------------------------------------------
# Heater simulator (ht_debug.c, VB_ID_DEBUG)
# ---------------------------------------------------------------------------

HEATER_UNIT_ID_BASE = 210
HEATER_COUNT        = 3


class HeaterSimulator:
    """Debug heater read/written by ht_debug.c.

    Holding registers (0-based):
      0    state            0 = ready, 1 = heating, 2 = max. temperature
      1-2  measured power   W   uint32 high/low
      3-4  energy total     Wh  uint32 high/low
      5    boiler temp      0.1 degC
      8-9  power setpoint   W   uint32 high/low  (written by the Smartfox)
    """

    STATE_READY    = 0
    STATE_HEATING  = 1
    STATE_MAX_TEMP = 2

    STATE_NAMES = {0: "Ready", 1: "Heating", 2: "Max Temp"}

    RAMP_W_PER_S      = 500
    MAX_TEMP_HYST_C   = 5.0

    def __init__(self, slot: int):
        self.slot              = slot
        self.unit_id           = HEATER_UNIT_ID_BASE + slot
        self.nominal_power_w   = 3000
        self.setpoint_w        = 0
        self.power_w           = 0
        self.energy_wh         = 0.0
        self.temperature_c     = 45.0
        self.max_temp_c        = 65.0
        self.heat_rate_k_min   = 2.0     # temperature rise per minute at nominal power
        self.cool_rate_k_min   = 0.2     # standing loss per minute
        self.auto_temperature  = True
        self.force_max_temp    = False
        self.max_temp_reached  = False
        self.last_request_time = "Never"
        self.last_request_type = "None"

    @property
    def state(self) -> int:
        if self.force_max_temp or self.max_temp_reached:
            return self.STATE_MAX_TEMP
        return self.STATE_HEATING if self.power_w > 0 else self.STATE_READY

    def read_registers(self, start: int, count: int) -> list:
        power  = int(self.power_w) & 0xFFFFFFFF
        energy = int(self.energy_wh) & 0xFFFFFFFF
        sp     = int(self.setpoint_w) & 0xFFFFFFFF
        regs = {
            0: self.state,
            1: (power >> 16) & 0xFFFF,
            2: power & 0xFFFF,
            3: (energy >> 16) & 0xFFFF,
            4: energy & 0xFFFF,
            5: int(round(self.temperature_c * 10.0)) & 0xFFFF,
            8: (sp >> 16) & 0xFFFF,
            9: sp & 0xFFFF,
        }
        return [regs.get(start + i, 0) for i in range(count)]

    def write_register(self, address: int, value: int) -> bool:
        if address == 8:
            self.setpoint_w = ((value & 0xFFFF) << 16) | (self.setpoint_w & 0xFFFF)
            return True
        if address == 9:
            self.setpoint_w = (self.setpoint_w & 0xFFFF0000) | (value & 0xFFFF)
            return True
        return False

    def update(self):
        # Device-side thermostat: switches off at max_temp, back on below the hysteresis
        if self.temperature_c >= self.max_temp_c:
            self.max_temp_reached = True
        elif self.temperature_c <= self.max_temp_c - self.MAX_TEMP_HYST_C:
            self.max_temp_reached = False

        target = min(self.setpoint_w, self.nominal_power_w)
        if self.state == self.STATE_MAX_TEMP:
            target = 0

        diff = target - self.power_w
        if abs(diff) <= self.RAMP_W_PER_S:
            self.power_w = target
        else:
            self.power_w += self.RAMP_W_PER_S if diff > 0 else -self.RAMP_W_PER_S

        self.energy_wh += self.power_w / 3600.0

        if self.auto_temperature:
            heat = (self.power_w / self.nominal_power_w) * self.heat_rate_k_min / 60.0 if self.nominal_power_w > 0 else 0.0
            self.temperature_c += heat - self.cool_rate_k_min / 60.0
            self.temperature_c = max(10.0, min(95.0, self.temperature_c))


# ---------------------------------------------------------------------------
# Energy meter simulator (new)
# ---------------------------------------------------------------------------

REG_ENERGY_FOR   = 40999
REG_ENERGY_REV   = 41003
REG_POWER        = 41017
REG_POWER_PHASES = 41019
REG_CURRENT      = 41028


class SmartfoxSlaveSimulator:
    """Models a Smartfox energy meter read by em_smartfox_slave.c"""

    def __init__(self):
        self.total_power_w     = 0
        self.phase_powers_w    = [0, 0, 0]
        self.phase_currents_ma = [0, 0, 0]
        self.energy_forward_wh = 0.0
        self.energy_reverse_wh = 0.0
        self.last_request_time = "Never"
        self.last_request_type = "None"

    @staticmethod
    def _pack32(value: int, regs: dict, addr: int):
        v = value & 0xFFFFFFFF
        regs[addr]     = (v >> 16) & 0xFFFF
        regs[addr + 1] = v & 0xFFFF

    @staticmethod
    def _pack64(value: int, regs: dict, addr: int):
        v = int(value) & 0xFFFFFFFFFFFFFFFF
        regs[addr]     = (v >> 48) & 0xFFFF
        regs[addr + 1] = (v >> 32) & 0xFFFF
        regs[addr + 2] = (v >> 16) & 0xFFFF
        regs[addr + 3] = v & 0xFFFF

    def build_registers(self) -> dict:
        regs = {}
        self._pack32(self.total_power_w, regs, REG_POWER)
        for i in range(3):
            self._pack32(self.phase_powers_w[i],    regs, REG_POWER_PHASES + i * 2)
            self._pack32(self.phase_currents_ma[i], regs, REG_CURRENT      + i * 2)
        self._pack64(int(self.energy_forward_wh), regs, REG_ENERGY_FOR)
        self._pack64(int(self.energy_reverse_wh), regs, REG_ENERGY_REV)
        return regs

    def read_registers(self, start: int, count: int) -> list:
        regs = self.build_registers()
        return [regs.get(start + i, 0) for i in range(count)]

    def update(self):
        if self.total_power_w > 0:
            self.energy_forward_wh += self.total_power_w / 3600.0
        elif self.total_power_w < 0:
            self.energy_reverse_wh += abs(self.total_power_w) / 3600.0


# ---------------------------------------------------------------------------
# Battery simulator — register map of bs_victron.c (Victron VE / CC / Cerbo CTRL)
# ---------------------------------------------------------------------------

VICTRON_SYSTEM_UNIT   = 0                 # SoC reg 843, Cerbo power reg 842
VICTRON_DEVICE_UNITS  = (242, 246, 227)   # VE, CC, Cerbo: power 26/27, temp 61, setpoints 37/40/41
VICTRON_REG_SOC       = 843
VICTRON_REG_CERBO_P   = 842
VICTRON_REG_VOLTAGE   = 26
VICTRON_REG_CURRENT   = 27
VICTRON_REG_TEMP      = 61
VICTRON_REG_SETPOINTS = (37, 40, 41)      # L1, L2, L3


def _s16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


class BatterySimulator:
    """Battery with delayed, ramped response to the setpoint written by bs_victron.c (W, + = charge)."""

    VOLTAGE_V = 50.0

    def __init__(self, battery_id: int):
        self.battery_id        = battery_id
        self.local_ip          = ""        # empty = answers on any local IP
        self.capacity_kwh      = 10.0
        self.soc               = 50.0
        self.max_charge_w      = 5000
        self.max_discharge_w   = 5000
        self.ramp_w_per_s      = 2000
        self.delay_s           = 2
        self.phases            = 1         # must match the firmware's three-phase battery setting
        self.temperature_c     = 25.0
        self.setpoint_regs     = [0, 0, 0]
        self.actual_power_w    = 0.0
        self._pending          = []        # setpoints still inside the response delay
        self.last_request_time = "Never"
        self.last_request_type = "None"

    @property
    def setpoint_w(self) -> int:
        # bs_victron.c writes the same value to every phase register, so 3-phase delivers the sum
        if self.phases == 3:
            return sum(self.setpoint_regs)
        return self.setpoint_regs[0]

    def read_registers(self, unit_id: int, start: int, count: int) -> list:
        if unit_id == VICTRON_SYSTEM_UNIT:
            regs = {
                VICTRON_REG_SOC:     int(self.soc),
                VICTRON_REG_CERBO_P: int(self.actual_power_w),
            }
        else:
            regs = {
                VICTRON_REG_VOLTAGE: int(self.VOLTAGE_V * 100),
                VICTRON_REG_CURRENT: int(round(self.actual_power_w / self.VOLTAGE_V * 10)),
                VICTRON_REG_TEMP:    int(self.temperature_c * 10),
            }
            for reg, val in zip(VICTRON_REG_SETPOINTS, self.setpoint_regs):
                regs[reg] = val
        return [regs.get(start + i, 0) & 0xFFFF for i in range(count)]

    def write_registers(self, start: int, values: list):
        for i, value in enumerate(values):
            reg = start + i
            if reg in VICTRON_REG_SETPOINTS:
                self.setpoint_regs[VICTRON_REG_SETPOINTS.index(reg)] = _s16(value)

    def update(self):
        self._pending.append(self.setpoint_w)
        while len(self._pending) > self.delay_s + 1:   # delay was lowered in the UI
            self._pending.pop(0)
        target =self._pending.pop(0) if len(self._pending) > self.delay_s else self.actual_power_w

        target = max(-self.max_discharge_w, min(self.max_charge_w, target))
        if (self.soc >= 100.0 and target > 0) or (self.soc <= 0.0 and target < 0):
            target = 0

        diff = target - self.actual_power_w
        step = min(abs(diff), self.ramp_w_per_s)
        self.actual_power_w += step if diff > 0 else -step

        if self.capacity_kwh > 0:
            self.soc += self.actual_power_w / 3600.0 / (self.capacity_kwh * 1000.0) * 100.0
            self.soc = max(0.0, min(100.0, self.soc))


# ---------------------------------------------------------------------------
# Device poller — reads the JSON API (/api/liveview/data + /devices) of the real SmartFox
# ---------------------------------------------------------------------------


# Device-type keys of /api/liveview/devices (enum PRODUKT_TYPE in JsonBuilderConstants.h)
API_TYPE_HEATER  = "1"   # Verbrauchsregler, power in kW
API_TYPE_CHARGER = "2"   # Ladestation, power in W
API_TYPE_BATTERY = "5"   # Batterie, power in W (+ = charging)
API_TYPE_AOUT    = "9"   # Analogausgang, power in kW


class DevicePoller(threading.Thread):
    """Field names stay the old values.xml ids so the rest of the simulator is unchanged."""

    NUMERIC_FIELDS = {
        'detailsPowerValue',                              # total grid power W (pos = consuming)
        'toGridValue',                                    # grid power kW     (pos = feeding/surplus)
        'powerL1Value', 'powerL2Value', 'powerL3Value',  # phase powers W
        'ampereL1Value', 'ampereL2Value', 'ampereL3Value',# phase currents A
        'cc1Power', 'cc2Power', 'cc3Power', 'cc4Power', 'cc5Power',  # charger powers kW
        'consumptionControl1Power', 'consumptionControl2Power', 'consumptionControl3Power',
        'analogOutPower',
        'batt1Power', 'batt2Power', 'batt3Power',         # battery powers W (+ = charging)
    }
    STRING_FIELDS = {
        'hidCcSt1', 'hidCcSt2', 'hidCcSt3', 'hidCcSt4', 'hidCcSt5',
    }
    FIELDS = NUMERIC_FIELDS | STRING_FIELDS

    def __init__(self, ip: str, interval: int):
        super().__init__(daemon=True)
        self.ip       = ip
        self.interval = interval
        self._stop_evt = threading.Event()
        self._lock    = threading.Lock()
        self._values: dict = {}
        self.last_poll_time = "Never"
        self.last_error: str | None = None

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.is_set():
            self._poll()
            self._stop_evt.wait(timeout=self.interval)

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://{self.ip}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _poll(self):
        try:
            data    = self._get_json("/api/liveview/data")
            devices = self._get_json("/api/liveview/devices").get("devices", {})
            values  = self._parse(data, devices)
            with self._lock:
                self._values = values
                self.last_poll_time = datetime.now().strftime("%H:%M:%S")
                self.last_error = None
        except Exception as e:
            with self._lock:
                self.last_error = str(e)

    @staticmethod
    def _parse(data: dict, devices: dict) -> dict:
        values: dict = {}
        meas = data.get("meas", {})

        grid_w = meas.get("power", {}).get("W")
        if grid_w is not None:
            values["detailsPowerValue"] = float(grid_w)
            values["toGridValue"]       = -float(grid_w) / 1000.0
        for i in range(1, 4):
            phase = meas.get(f"l{i}", {})
            if "power" in phase:
                values[f"powerL{i}Value"] = float(phase["power"])
            if "current" in phase:
                values[f"ampereL{i}Value"] = float(phase["current"]) / 1000.0   # mA -> A

        for i, cc in enumerate(devices.get(API_TYPE_CHARGER, [])[:5], start=1):
            values[f"cc{i}Power"] = float(cc.get("power") or 0) / 1000.0
            values[f"hidCcSt{i}"] = str(cc.get("state", ""))
        for i, vb in enumerate(devices.get(API_TYPE_HEATER, [])[:3], start=1):
            values[f"consumptionControl{i}Power"] = float(vb.get("power") or 0)
        aout = devices.get(API_TYPE_AOUT, [])
        if aout:
            values["analogOutPower"] = float(aout[0].get("power") or 0)
        for i, bs in enumerate(devices.get(API_TYPE_BATTERY, [])[:3], start=1):
            values[f"batt{i}Power"] = float(bs.get("power") or 0)
        return values

    def get(self) -> tuple[dict, str, str | None]:
        with self._lock:
            return dict(self._values), self.last_poll_time, self.last_error


# ---------------------------------------------------------------------------
# Combined Modbus TCP server (port 5020)
# Dispatches by register address range:
#   start >= 40000  ->  energy meter (em_smartfox_slave.c register map)
#   start <  40000  ->  charger      (car_cc_debug.c register map)
# ---------------------------------------------------------------------------

MODBUS_PORTS = (5020, 502)
METER_REG_THRESHOLD = 40000


class CombinedModbusTCPServer:
    FC_READ_HOLDING   = 3
    FC_WRITE_SINGLE   = 6
    FC_WRITE_MULTIPLE = 16

    def __init__(self, main_window):
        self.main_window = main_window
        self._server     = None

    @property
    def meter(self) -> SmartfoxSlaveSimulator:
        return self.main_window.meter

    async def _handle_client(self, reader, writer):
        peer     = writer.get_extra_info('peername')
        local_ip = writer.get_extra_info('sockname')[0]
        print(f"[Modbus] Connected: {peer} -> {local_ip}")
        try:
            while True:
                header   = await reader.readexactly(7)
                trans_id = struct.unpack_from('>H', header, 0)[0]
                length   = struct.unpack_from('>H', header, 4)[0]
                unit_id  = header[6]
                pdu          = await reader.readexactly(length - 1)
                func_code    = pdu[0]
                response_pdu = self._dispatch(unit_id, func_code, pdu[1:], local_ip)
                resp_len = len(response_pdu) + 1
                writer.write(
                    struct.pack('>HHH', trans_id, 0, resp_len)
                    + bytes([unit_id]) + response_pdu
                )
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            print(f"[Modbus] Client error: {e}")
        finally:
            writer.close()
            print(f"[Modbus] Disconnected: {peer}")

    def _dispatch(self, unit_id, func_code, data, local_ip=""):
        try:
            start = struct.unpack_from('>H', data, 0)[0]

            if unit_id == VICTRON_SYSTEM_UNIT or unit_id in VICTRON_DEVICE_UNITS:
                return self._dispatch_battery(unit_id, func_code, start, data, local_ip)

            if func_code == self.FC_READ_HOLDING:
                count  = struct.unpack_from('>H', data, 2)[0]
                if start >= METER_REG_THRESHOLD:
                    values = self._read_meter(start, count)
                else:
                    values = self._read_charger(unit_id, start, count)
                payload = b''.join(struct.pack('>H', v & 0xFFFF) for v in values)
                return bytes([func_code, len(payload)]) + payload

            elif func_code == self.FC_WRITE_SINGLE:
                value = struct.unpack_from('>H', data, 2)[0]
                self._write_charger(unit_id, start, [value])
                return bytes([func_code]) + data[:4]

            elif func_code == self.FC_WRITE_MULTIPLE:
                count  = struct.unpack_from('>H', data, 2)[0]
                values = [struct.unpack_from('>H', data, 5 + i * 2)[0] for i in range(count)]
                self._write_charger(unit_id, start, values)
                return bytes([func_code]) + data[:4]

            else:
                return bytes([func_code | 0x80, 0x01])
        except Exception as e:
            print(f"[Modbus] Dispatch error unit {unit_id} fc {func_code}: {e}")
            return bytes([func_code | 0x80, 0x04])

    # ---- battery (bs_victron.c) ----

    def _get_battery(self, local_ip):
        batteries = self.main_window.batteries
        for b in batteries:
            if b.local_ip and b.local_ip == local_ip:
                return b
        for b in batteries:
            if not b.local_ip:
                return b
        return None

    def _dispatch_battery(self, unit_id, func_code, start, data, local_ip):
        battery = self._get_battery(local_ip)
        if battery is None:
            return bytes([func_code | 0x80, 0x0B])   # gateway target failed to respond
        now = datetime.now().strftime("%H:%M:%S")

        if func_code == self.FC_READ_HOLDING:
            count   = struct.unpack_from('>H', data, 2)[0]
            values  = battery.read_registers(unit_id, start, count)
            battery.last_request_time = now
            battery.last_request_type = f"Unit {unit_id} read {start} qty {count}"
            print(f"[Batt {battery.battery_id} RX] Unit {unit_id}: Read HR {start} qty {count} -> {values}")
            payload = b''.join(struct.pack('>H', v) for v in values)
            return bytes([func_code, len(payload)]) + payload

        if func_code == self.FC_WRITE_SINGLE:
            values = [struct.unpack_from('>H', data, 2)[0]]
        elif func_code == self.FC_WRITE_MULTIPLE:
            count  = struct.unpack_from('>H', data, 2)[0]
            values = [struct.unpack_from('>H', data, 5 + i * 2)[0] for i in range(count)]
        else:
            return bytes([func_code | 0x80, 0x01])

        battery.write_registers(start, values)
        battery.last_request_time = now
        battery.last_request_type = f"Unit {unit_id} write {start} <- {[_s16(v) for v in values]}"
        print(f"[Batt {battery.battery_id} TX] Unit {unit_id}: Write HR {start} <- {[_s16(v) for v in values]}")
        return bytes([func_code]) + data[:4]

    # ---- meter reads ----

    def _read_meter(self, start, count):
        now = datetime.now().strftime("%H:%M:%S")
        self.meter.last_request_time = now
        self.meter.last_request_type = f"FC3 addr={start} qty={count}"
        result = self.meter.read_registers(start, count)
        print(f"[Meter RX] Read HR {start} qty {count} -> {result}")
        return result

    # ---- charger reads / writes ----

    def _get_heater(self, unit_id):
        for h in self.main_window.heaters:
            if h.unit_id == unit_id:
                return h
        return None

    def _get_station(self, unit_id):
        for s in self.main_window.stations:
            if s.station_id == unit_id:
                return s
        return None

    def _read_charger(self, unit_id, start, count):
        now = datetime.now().strftime("%H:%M:%S")
        heater = self._get_heater(unit_id)
        if heater is not None:
            heater.last_request_time = now
            heater.last_request_type = f"Read HR {start} (qty {count})"
            result = heater.read_registers(start, count)
            print(f"[Heater RX] Unit {unit_id}: Read HR {start} qty {count} -> {result}")
            return result
        if unit_id == 100:
            result_w = self.main_window.surplus_power + self.main_window.consumer_total_w()
            packed   = struct.pack('>f', result_w)
            high     = (packed[0] << 8) | packed[1]
            low      = (packed[2] << 8) | packed[3]
            reg_map  = {0: high, 1: low}
            result   = [reg_map.get(start + i, 0) for i in range(count)]
            print(f"[Charger RX] Unit 100: Read HR {start} qty {count} -> {result}")
            return result
        station = self._get_station(unit_id)
        if station is None:
            return [0] * count
        station.last_request_time = now
        station.last_request_type = f"Read HR {start} (qty {count})"
        registers = station.get_modbus_data()
        result = [registers[start + i] if (start + i) < len(registers) else 0 for i in range(count)]
        print(f"[Charger RX] Unit {unit_id}: Read HR {start} qty {count} -> {result}")
        return result

    def _write_charger(self, unit_id, start, values):
        now = datetime.now().strftime("%H:%M:%S")
        heater = self._get_heater(unit_id)
        if heater is not None:
            heater.last_request_time = now
            heater.last_request_type = f"Write HR {start} (qty {len(values)})"
            print(f"[Heater TX] Unit {unit_id}: Write HR {start} qty {len(values)} <- {values}")
            for i, value in enumerate(values):
                heater.write_register(start + i, value)
            return
        if unit_id == 100:
            if start == 0 and len(values) >= 2:
                packed = struct.pack('>HH', values[0] & 0xFFFF, values[1] & 0xFFFF)
                value  = struct.unpack('>f', packed)[0]
                self.main_window.surplus_power = float(value)
                print(f"[Charger TX] Unit 100: Write HR 0-1 <- {value:.1f} W")
            return
        station = self._get_station(unit_id)
        if station is None:
            return
        station.last_request_time = now
        station.last_request_type = f"Write HR {start} (qty {len(values)})"
        print(f"[Charger TX] Unit {unit_id}: Write HR {start} qty {len(values)} <- {values}")
        for i, value in enumerate(values):
            station.write_register(start + i, value)

    async def serve(self):
        # 5020: debug drivers (car_cc_debug.c, ht_debug.c, wp_debug.c)
        # 502:  drivers with a hard-coded port (em_smartfox_slave.c, bs_victron.c),
        #       so no port forwarding is needed. macOS and Windows allow binding
        #       0.0.0.0:502 without root; on Linux this needs root or
        #       CAP_NET_BIND_SERVICE, otherwise only 5020 is served.
        servers = []
        for port in MODBUS_PORTS:
            try:
                servers.append(await asyncio.start_server(self._handle_client, '0.0.0.0', port))
                print(f"[Modbus] Combined server listening on 0.0.0.0:{port}")
            except OSError as e:
                print(f"[Modbus] Port {port} not available: {e}")
        if not servers:
            raise RuntimeError("no Modbus TCP port available")
        await asyncio.gather(*(s.serve_forever() for s in servers))


class CombinedServerThread(threading.Thread):
    def __init__(self, main_window):
        super().__init__(daemon=True)
        self.main_window = main_window
        self.running     = False

    def run(self):
        self.running = True
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(CombinedModbusTCPServer(self.main_window).serve())
        except Exception as e:
            print(f"[Modbus] Server error: {e}")
        finally:
            self.running = False


# ---------------------------------------------------------------------------
# UI widgets
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Smartfox configuration over its JSON API (http://<smartfox>/api/, no auth)
# ---------------------------------------------------------------------------

# Firmware product ids (schnittstelle.id) and device type numbers of devices/config/<type>/<idx>
SF_TYPE_VB, SF_TYPE_CC, SF_TYPE_EM, SF_TYPE_BS = 1, 2, 3, 5
SF_CC_SLOTS = 5
SF_BS_SLOTS = 3
SF_PRODUCT_CC_DEBUG       = 24   # car_cc_debug.c: port 5020, unit id = schnittstelle.address
SF_PRODUCT_VB_DEBUG       = 10   # ht_debug.c: port 5020, unit id 210 + slot (fixed)
SF_PRODUCT_BS_VICTRON_VE  = 9    # bs_victron.c: port 502, unit id 242 (fixed)
SF_PRODUCT_EM_SLAVE_TCP   = 8    # em_smartfox_slave.c: port 502, unit id 1 (fixed)


class SmartfoxApi:
    """Configures the Smartfox under test. Every POST is saved to its flash - only on a click."""

    def __init__(self, smartfox_ip, sim_ip):
        self.smartfox_ip = smartfox_ip   # callables, read in the GUI thread
        self.sim_ip      = sim_ip

    def request(self, base_url, method, path, body=None, timeout=5):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None


class SmartfoxConfigBar(QWidget):
    """
    "Konfigurieren" / "Entfernen" for one simulated device. build() runs in the GUI thread and returns
    (path, device, checks): device is the partial {"device": ...} content, checks a list of
    (path, predicate(json) -> warning or None) run before the POST. The HTTP calls run in a thread;
    the result is shown by a timer (Qt widgets must not be touched from the thread).
    """

    def __init__(self, api: SmartfoxApi, build, parent=None):
        super().__init__(parent)
        self.api    = api
        self.build  = build
        self.status = "-"
        self.busy   = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.config_btn = QPushButton("Konfigurieren")
        self.config_btn.clicked.connect(self.configure)
        layout.addWidget(self.config_btn)
        self.remove_btn = QPushButton("Entfernen")
        self.remove_btn.clicked.connect(self.remove)
        layout.addWidget(self.remove_btn)
        self.status_label = QLabel("-")
        self.status_label.setStyleSheet("font-size: 10px; color: #444;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.status_label.setText(self.status))
        self.timer.start(300)

    def _start(self, remove):
        if self.busy:
            return
        built = self.build()
        if isinstance(built, str):   # not configurable (e.g. no free slot)
            self.status = built
            return
        path, device, checks = built
        if remove:
            device, checks = {"schnittstelle": {"id": 0}}, []
        base_url = f"http://{self.api.smartfox_ip().strip()}/api/"
        self.busy = True
        self.status = "sende ..."
        threading.Thread(target=self._run, args=(base_url, path, device, checks, remove), daemon=True).start()

    def configure(self): self._start(remove=False)
    def remove(self):    self._start(remove=True)

    def _run(self, base_url, path, device, checks, remove):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            warnings = []
            for check_path, check in checks:
                code, data = self.api.request(base_url, "GET", check_path)
                warning = check(data) if code == 200 else f"{check_path}: HTTP {code}"
                if warning:
                    warnings.append(warning)

            code, data = self.api.request(base_url, "POST", path, {"device": device})
            if code != 200:
                err = data.get("error") if isinstance(data, dict) else data
                self.status = f"[{now}] {path}: HTTP {code} {err or ''}"
                return
            sch = (data or {}).get("device", {}).get("schnittstelle", {})
            if remove:
                result = f"entfernt (Produkt {sch.get('id')})"
            else:
                result = f"OK: Produkt {sch.get('id')}, IP {sch.get('ip')}, Adresse {sch.get('address')}"
            self.status = f"[{now}] {path} {result}" + "".join(f"  ⚠ {w}" for w in warnings)
        except Exception as e:
            self.status = f"[{now}] Fehler: {e}"
        finally:
            self.busy = False
            print(f"[Smartfox API] {self.status}")


class StationWidget(QGroupBox):
    def __init__(self, station: ChargingStationSimulator, api: SmartfoxApi, parent=None):
        super().__init__(f"Station {station.station_id}", parent)
        self.station = station
        self.api     = api
        self.window  = None
        self.setup_ui()

    def smartfox_config(self):
        """Smartfox charger slot station_id - 1, unit id = station id (Modbus address)."""
        sid = self.station.station_id
        if sid > SF_CC_SLOTS:
            return f"Smartfox hat nur {SF_CC_SLOTS} Ladestations-Slots"
        device = {
            "name": f"SIM Station {sid}",
            "schnittstelle": {"id": SF_PRODUCT_CC_DEBUG, "ip": self.api.sim_ip(), "address": sid},
        }
        return f"devices/config/{SF_TYPE_CC}/{sid - 1}", device, []

    def setup_ui(self):
        layout = QGridLayout()

        layout.addWidget(QLabel("<b>Status:</b>"), 0, 0)
        self.status_label = QLabel("Unplugged")
        self.status_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.status_label, 0, 1)

        btn_layout = QHBoxLayout()
        self.plug_in_btn = QPushButton("Einstecken")
        self.plug_in_btn.clicked.connect(self.on_plug_in)
        btn_layout.addWidget(self.plug_in_btn)
        self.plug_out_btn = QPushButton("Ausstecken")
        self.plug_out_btn.clicked.connect(self.on_plug_out)
        btn_layout.addWidget(self.plug_out_btn)
        self.error_btn = QPushButton("Fehler")
        self.error_btn.clicked.connect(self.on_error)
        btn_layout.addWidget(self.error_btn)
        layout.addLayout(btn_layout, 1, 0, 1, 2)

        layout.addWidget(QLabel("Power:"),       2, 0)
        self.power_label = QLabel("0.0 kW")
        layout.addWidget(self.power_label,       2, 1)

        layout.addWidget(QLabel("Voltage:"),     3, 0)
        self.voltage_label = QLabel("230.0 V")
        layout.addWidget(self.voltage_label,     3, 1)

        layout.addWidget(QLabel("Current:"),     4, 0)
        self.current_label = QLabel("0.0 A")
        layout.addWidget(self.current_label,     4, 1)

        layout.addWidget(QLabel("Energy:"),      5, 0)
        self.energy_label = QLabel("0.00 kWh")
        layout.addWidget(self.energy_label,      5, 1)

        layout.addWidget(QLabel("Set Current:"), 6, 0)
        self.set_current_label = QLabel("0 mA")
        layout.addWidget(self.set_current_label, 6, 1)

        layout.addWidget(QLabel("Station Phases:"), 7, 0)
        self.phases_label = QLabel("3")
        layout.addWidget(self.phases_label,      7, 1)

        layout.addWidget(QLabel("Vehicle Phases:"), 8, 0)
        self.vehicle_phases_combo = QComboBox()
        self.vehicle_phases_combo.addItems(["1", "2", "3"])
        self.vehicle_phases_combo.setCurrentIndex(2)
        self.vehicle_phases_combo.currentIndexChanged.connect(self.update_vehicle_phases)
        layout.addWidget(self.vehicle_phases_combo, 8, 1)

        layout.addWidget(QLabel("Eff. Phases:"), 9, 0)
        self.eff_phases_label = QLabel("3")
        layout.addWidget(self.eff_phases_label,  9, 1)

        layout.addWidget(QLabel("Max Power:"),   10, 0)
        self.max_power_spin = QDoubleSpinBox()
        self.max_power_spin.setRange(1.0, 22.0)
        self.max_power_spin.setValue(self.station.max_power)
        self.max_power_spin.setSuffix(" kW")
        self.max_power_spin.valueChanged.connect(self.update_max_power)
        layout.addWidget(self.max_power_spin,    10, 1)

        layout.addWidget(QLabel("Amperage:"),    11, 0)
        self.amperage_spin = QDoubleSpinBox()
        self.amperage_spin.setRange(0.0, 32.0)
        self.amperage_spin.setValue(0.0)
        self.amperage_spin.setSuffix(" A")
        self.amperage_spin.valueChanged.connect(self.update_amperage)
        layout.addWidget(self.amperage_spin,     11, 1)

        layout.addWidget(QLabel("Manual:"),      12, 0)
        self.manual_checkbox = QCheckBox("Manual Override")
        self.manual_checkbox.setChecked(False)
        self.manual_checkbox.stateChanged.connect(self.toggle_manual)
        layout.addWidget(self.manual_checkbox,   12, 1)

        layout.addWidget(QLabel("Last Request:"), 13, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(self.last_req_label,    13, 1)

        self.config_bar = SmartfoxConfigBar(self.api, self.smartfox_config)
        layout.addWidget(self.config_bar,        14, 0, 1, 2)

        self.setLayout(layout)

    def update_display(self):
        status_name = self.station.STATUS_NAMES.get(self.station.status_code, "Unknown")
        self.status_label.setText(status_name)
        if self.station.status_code == ChargingStationSimulator.STATUS_CHARGING:
            self.status_label.setStyleSheet("font-weight: bold; color: orange;")
        elif self.station.status_code == ChargingStationSimulator.STATUS_ERROR:
            self.status_label.setStyleSheet("font-weight: bold; color: red;")
        elif self.station.status_code == ChargingStationSimulator.STATUS_CONNECTED:
            self.status_label.setStyleSheet("font-weight: bold; color: green;")
        else:
            self.status_label.setStyleSheet("font-weight: bold; color: gray;")

        connected = self.station.status_code in (
            ChargingStationSimulator.STATUS_CONNECTED,
            ChargingStationSimulator.STATUS_CHARGING
        )
        self.plug_in_btn.setEnabled(not connected and self.station.status_code != ChargingStationSimulator.STATUS_ERROR)
        self.plug_out_btn.setEnabled(connected)
        self.error_btn.setEnabled(self.station.status_code != ChargingStationSimulator.STATUS_ERROR)

        self.power_label.setText(f"{self.station.current_power:.1f} kW")
        self.voltage_label.setText(f"{self.station.voltage:.1f} V")
        self.current_label.setText(f"{self.station.current_amperage:.1f} A")
        self.energy_label.setText(f"{self.station.energy_delivered:.2f} kWh")
        self.set_current_label.setText(
            f"{self.station.set_current_ma} mA ({self.station.set_current_ma/1000:.1f} A)"
        )
        self.phases_label.setText(f"{self.station.phases}")

        self.vehicle_phases_combo.blockSignals(True)
        self.vehicle_phases_combo.setCurrentIndex(self.station.vehicle_phases - 1)
        self.vehicle_phases_combo.blockSignals(False)
        self.eff_phases_label.setText(f"{self.station.effective_phases}")

        if not self.station.manual_amperage:
            self.amperage_spin.blockSignals(True)
            self.amperage_spin.setValue(self.station.current_amperage)
            self.amperage_spin.blockSignals(False)

        if self.window and self.window.server_thread and self.window.server_thread.running:
            self.last_req_label.setText(
                f"[{self.station.last_request_time}] {self.station.last_request_type}"
            )

    def on_plug_in(self):   self.station.plug_in();  self.update_display()
    def on_plug_out(self):  self.station.plug_out(); self.update_display()
    def on_error(self):     self.station.set_error(); self.update_display()
    def update_max_power(self, value): self.station.max_power = value
    def update_vehicle_phases(self, index): self.station.vehicle_phases = index + 1
    def update_amperage(self, value):
        self.station.set_amperage(value)
        self.station.manual_amperage = True
        self.manual_checkbox.setChecked(True)
    def toggle_manual(self, state):
        self.station.manual_amperage = (state == Qt.CheckState.Checked)
        if not self.station.manual_amperage:
            self.station.set_amperage(self.station.set_current_ma / 1000.0)


class MeterWidget(QGroupBox):
    def __init__(self, meter: SmartfoxSlaveSimulator, api: SmartfoxApi, parent=None):
        super().__init__("Smartfox Slave Meter  (port 502, Unit-ID 1)", parent)
        self.meter = meter
        self.api   = api
        self._setup_ui()

    def smartfox_config(self):
        """Smartfox meter slot (chosen), added to the grid power: the simulated grid."""
        device = {
            "name": "SIM Netz",
            "schnittstelle": {"id": SF_PRODUCT_EM_SLAVE_TCP, "ip": self.api.sim_ip()},
            "produkt": {"effect_on_gridpower": 1},
        }
        return f"devices/config/{SF_TYPE_EM}/{self.slot_spin.value()}", device, []

    def _setup_ui(self):
        layout = QGridLayout()
        row = 0

        layout.addWidget(QLabel("<b>Total Power (W) [auto]:</b>"), row, 0)
        self.total_power_spin = QSpinBox()
        self.total_power_spin.setRange(-99999, 99999)
        self.total_power_spin.setValue(0)
        self.total_power_spin.setSuffix(" W")
        self.total_power_spin.setEnabled(False)
        layout.addWidget(self.total_power_spin, row, 1, 1, 2)
        row += 1

        layout.addWidget(QLabel("<b>Phase Powers (W):</b>"), row, 0)
        row += 1
        self.phase_power_spins = []
        for i in range(3):
            layout.addWidget(QLabel(f"  L{i+1}:"), row, 0)
            spin = QSpinBox()
            spin.setRange(-99999, 99999)
            spin.setValue(0)
            spin.setSuffix(" W")
            spin.setEnabled(False)
            layout.addWidget(spin, row, 1, 1, 2)
            self.phase_power_spins.append(spin)
            row += 1

        layout.addWidget(QLabel("<b>Phase Currents (mA):</b>"), row, 0)
        row += 1
        self.phase_current_spins = []
        for i in range(3):
            layout.addWidget(QLabel(f"  L{i+1}:"), row, 0)
            spin = QSpinBox()
            spin.setRange(-999999, 999999)
            spin.setValue(0)
            spin.setSuffix(" mA")
            spin.setEnabled(False)
            layout.addWidget(spin, row, 1, 1, 2)
            self.phase_current_spins.append(spin)
            row += 1

        layout.addWidget(QLabel("<b>Energy Forward:</b>"), row, 0)
        self.energy_for_label = QLabel("0.000 Wh")
        layout.addWidget(self.energy_for_label, row, 1)
        btn = QPushButton("Reset"); btn.setFixedWidth(55)
        btn.clicked.connect(lambda: setattr(self.meter, 'energy_forward_wh', 0.0))
        layout.addWidget(btn, row, 2)
        row += 1

        layout.addWidget(QLabel("<b>Energy Reverse:</b>"), row, 0)
        self.energy_rev_label = QLabel("0.000 Wh")
        layout.addWidget(self.energy_rev_label, row, 1)
        btn2 = QPushButton("Reset"); btn2.setFixedWidth(55)
        btn2.clicked.connect(lambda: setattr(self.meter, 'energy_reverse_wh', 0.0))
        layout.addWidget(btn2, row, 2)
        row += 1

        layout.addWidget(QLabel("Last Request:"), row, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(self.last_req_label, row, 1, 1, 2)
        row += 1

        layout.addWidget(QLabel("Smartfox-Zähler-Slot:"), row, 0)
        self.slot_spin = QSpinBox()
        self.slot_spin.setRange(0, 4)
        self.slot_spin.setToolTip("Zähler-Slot am Smartfox (0-basiert); wird als Netz-Zähler (addieren) eingestellt")
        layout.addWidget(self.slot_spin, row, 1)
        row += 1

        self.config_bar = SmartfoxConfigBar(self.api, self.smartfox_config)
        layout.addWidget(self.config_bar, row, 0, 1, 3)

        self.setLayout(layout)

    def _on_total_power(self, v): pass  # driven automatically from surplus + charger sum
    def _on_phase_power(self, idx, v): pass
    def _on_phase_current(self, idx, v): pass

    def update_display(self):
        self.total_power_spin.blockSignals(True)
        self.total_power_spin.setValue(self.meter.total_power_w)
        self.total_power_spin.blockSignals(False)
        for i in range(3):
            for spin, val in [
                (self.phase_power_spins[i],   self.meter.phase_powers_w[i]),
                (self.phase_current_spins[i], self.meter.phase_currents_ma[i]),
            ]:
                spin.blockSignals(True)
                spin.setValue(val)
                spin.blockSignals(False)
        self.energy_for_label.setText(f"{self.meter.energy_forward_wh:.3f} Wh")
        self.energy_rev_label.setText(f"{self.meter.energy_reverse_wh:.3f} Wh")
        self.last_req_label.setText(
            f"[{self.meter.last_request_time}] {self.meter.last_request_type}"
        )


class BatteryWidget(QGroupBox):
    def __init__(self, battery: BatterySimulator, api: SmartfoxApi, parent=None):
        super().__init__(f"Battery {battery.battery_id}  (Victron VE CTRL, unit 0 + 242)", parent)
        self.battery = battery
        self.api     = api
        self._setup_ui()

    def smartfox_config(self):
        """Smartfox battery slot battery_id - 1. Answers on its own local IP if one is set."""
        slot = self.battery.battery_id - 1
        if slot >= SF_BS_SLOTS:
            return f"Smartfox hat nur {SF_BS_SLOTS} Batterie-Slots"
        device = {
            "name": f"SIM Victron {self.battery.battery_id}",
            "schnittstelle": {"id": SF_PRODUCT_BS_VICTRON_VE, "ip": self.battery.local_ip or self.api.sim_ip()},
        }
        return f"devices/config/{SF_TYPE_BS}/{slot}", device, []

    def _spin(self, lo, hi, value, suffix, slot, decimals=None):
        spin = QDoubleSpinBox() if decimals is not None else QSpinBox()
        if decimals is not None:
            spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.valueChanged.connect(slot)
        return spin

    def _setup_ui(self):
        b  = self.battery
        gl = QGridLayout()
        row = 0

        gl.addWidget(QLabel("Local IP (empty = any):"), row, 0)
        self.ip_edit = QLineEdit(b.local_ip)
        self.ip_edit.setPlaceholderText("any")
        self.ip_edit.textChanged.connect(lambda t: setattr(b, 'local_ip', t.strip()))
        gl.addWidget(self.ip_edit, row, 1); row += 1

        gl.addWidget(QLabel("Phases (= firmware setting):"), row, 0)
        self.phases_combo = QComboBox()
        self.phases_combo.addItem("1-phase", userData=1)
        self.phases_combo.addItem("3-phase (setpoint written ×3)", userData=3)
        self.phases_combo.currentIndexChanged.connect(
            lambda _: setattr(b, 'phases', self.phases_combo.currentData()))
        gl.addWidget(self.phases_combo, row, 1); row += 1

        settings = [
            ("capacity_kwh",    "Capacity:",       self._spin(0.1, 200.0, b.capacity_kwh, " kWh", lambda v: setattr(b, 'capacity_kwh', v), 1)),
            ("soc",             "SoC:",            self._spin(0.0, 100.0, b.soc, " %", self._on_soc, 1)),
            ("temperature_c",   "Temperature:",    self._spin(-40.0, 100.0, b.temperature_c, " °C", lambda v: setattr(b, 'temperature_c', v), 1)),
            ("max_charge_w",    "Max charge:",     self._spin(0, 30000, b.max_charge_w, " W", lambda v: setattr(b, 'max_charge_w', v))),
            ("max_discharge_w", "Max discharge:",  self._spin(0, 30000, b.max_discharge_w, " W", lambda v: setattr(b, 'max_discharge_w', v))),
            ("ramp_w_per_s",    "Ramp:",           self._spin(1, 30000, b.ramp_w_per_s, " W/s", lambda v: setattr(b, 'ramp_w_per_s', v))),
            ("delay_s",         "Response delay:", self._spin(0, 30, b.delay_s, " s", lambda v: setattr(b, 'delay_s', v))),
        ]
        self.spins = {}   # battery attribute -> spin box, used by the control API
        for attr, caption, widget in settings:
            gl.addWidget(QLabel(caption), row, 0)
            gl.addWidget(widget, row, 1)
            self.spins[attr] = widget
            row += 1
        self.soc_spin = self.spins["soc"]

        gl.addWidget(QLabel("<b>Setpoint (firmware):</b>"), row, 0)
        self.setpoint_label = QLabel("0 W")
        gl.addWidget(self.setpoint_label, row, 1); row += 1

        gl.addWidget(QLabel("<b>Actual power:</b>"), row, 0)
        self.power_label = QLabel("0 W")
        self.power_label.setStyleSheet("font-weight: bold;")
        gl.addWidget(self.power_label, row, 1); row += 1

        gl.addWidget(QLabel("Last Request:"), row, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        gl.addWidget(self.last_req_label, row, 1); row += 1

        self.config_bar = SmartfoxConfigBar(self.api, self.smartfox_config)
        gl.addWidget(self.config_bar, row, 0, 1, 2)

        self.setLayout(gl)

    def _on_soc(self, value):
        self.battery.soc = value

    def update_display(self):
        b = self.battery
        regs = "/".join(str(v) for v in b.setpoint_regs)
        self.setpoint_label.setText(f"{b.setpoint_w:+d} W   (L1/L2/L3 regs: {regs})")
        state = "charging" if b.actual_power_w > 0 else "discharging" if b.actual_power_w < 0 else "idle"
        self.power_label.setText(f"{int(b.actual_power_w):+d} W  ({state})")
        if not self.soc_spin.hasFocus():
            self.soc_spin.blockSignals(True)
            self.soc_spin.setValue(b.soc)
            self.soc_spin.blockSignals(False)
        self.last_req_label.setText(f"[{b.last_request_time}] {b.last_request_type}")


class HeaterWidget(QGroupBox):
    def __init__(self, heater: HeaterSimulator, api: SmartfoxApi, parent=None):
        super().__init__(f"Debug Heater {heater.slot} (Unit-ID {heater.unit_id})", parent)
        self.heater = heater
        self.api    = api
        self._setup_ui()

    def smartfox_config(self):
        """Smartfox consumer slot = heater slot (unit id 210 + slot is fixed in ht_debug.c)."""
        slot = self.heater.slot
        device = {
            "name": f"SIM Heizstab {slot}",
            "schnittstelle": {"id": SF_PRODUCT_VB_DEBUG, "ip": self.api.sim_ip()},
            "produkt": {"power": self.heater.nominal_power_w},
        }
        checks = []
        if slot > 0:
            # ConsReg_Set.c starts the driver of slot 1/2 on a change only if slot 0 is a debug heater too
            def slot0_is_debug(data):
                product = (data or {}).get("device", {}).get("schnittstelle", {}).get("id")
                if product != SF_PRODUCT_VB_DEBUG:
                    return "Slot 0 ist kein Debug-Heizstab - Treiber läuft erst nach Neustart des Smartfox"
                return None
            checks.append((f"devices/config/{SF_TYPE_VB}/0", slot0_is_debug))
        return f"devices/config/{SF_TYPE_VB}/{slot}", device, checks

    def _setup_ui(self):
        layout = QGridLayout()
        row = 0

        layout.addWidget(QLabel("Nominal Power:"), row, 0)
        self.nominal_spin = QSpinBox()
        self.nominal_spin.setRange(0, 30000)
        self.nominal_spin.setSingleStep(100)
        self.nominal_spin.setValue(self.heater.nominal_power_w)
        self.nominal_spin.setSuffix(" W")
        self.nominal_spin.valueChanged.connect(lambda v: setattr(self.heater, "nominal_power_w", v))
        layout.addWidget(self.nominal_spin, row, 1)
        row += 1

        layout.addWidget(QLabel("Boiler Temp:"), row, 0)
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 99.0)
        self.temp_spin.setDecimals(1)
        self.temp_spin.setValue(self.heater.temperature_c)
        self.temp_spin.setSuffix(" C")
        self.temp_spin.valueChanged.connect(lambda v: setattr(self.heater, "temperature_c", v))
        layout.addWidget(self.temp_spin, row, 1)
        row += 1

        self.auto_temp_check = QCheckBox("Simulate temperature from power")
        self.auto_temp_check.setChecked(self.heater.auto_temperature)
        self.auto_temp_check.toggled.connect(lambda v: setattr(self.heater, "auto_temperature", v))
        layout.addWidget(self.auto_temp_check, row, 0, 1, 2)
        row += 1

        layout.addWidget(QLabel("Max Temp:"), row, 0)
        self.max_temp_spin = QDoubleSpinBox()
        self.max_temp_spin.setRange(20.0, 99.0)
        self.max_temp_spin.setDecimals(1)
        self.max_temp_spin.setValue(self.heater.max_temp_c)
        self.max_temp_spin.setSuffix(" C")
        self.max_temp_spin.valueChanged.connect(lambda v: setattr(self.heater, "max_temp_c", v))
        layout.addWidget(self.max_temp_spin, row, 1)
        row += 1

        self.force_max_check = QCheckBox("Force max. temperature (state 2)")
        self.force_max_check.toggled.connect(lambda v: setattr(self.heater, "force_max_temp", v))
        layout.addWidget(self.force_max_check, row, 0, 1, 2)
        row += 1

        layout.addWidget(QLabel("Setpoint:"), row, 0)
        self.setpoint_label = QLabel("0 W")
        self.setpoint_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.setpoint_label, row, 1)
        row += 1

        layout.addWidget(QLabel("Power:"), row, 0)
        self.power_label = QLabel("0 W")
        self.power_label.setStyleSheet("font-family: monospace; font-weight: bold;")
        layout.addWidget(self.power_label, row, 1)
        row += 1

        layout.addWidget(QLabel("State:"), row, 0)
        self.state_label = QLabel("-")
        layout.addWidget(self.state_label, row, 1)
        row += 1

        layout.addWidget(QLabel("Energy:"), row, 0)
        self.energy_label = QLabel("0 Wh")
        self.energy_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.energy_label, row, 1)
        row += 1

        layout.addWidget(QLabel("Registers:"), row, 0)
        self.reg_label = QLabel("-")
        self.reg_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.reg_label, row, 1)
        row += 1

        layout.addWidget(QLabel("Last Request:"), row, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(self.last_req_label, row, 1)
        row += 1

        self.config_bar = SmartfoxConfigBar(self.api, self.smartfox_config)
        layout.addWidget(self.config_bar, row, 0, 1, 2)

        self.setLayout(layout)

    def update_display(self):
        if self.heater.auto_temperature:
            self.temp_spin.blockSignals(True)
            self.temp_spin.setValue(self.heater.temperature_c)
            self.temp_spin.blockSignals(False)
        self.setpoint_label.setText(f"{self.heater.setpoint_w} W")
        self.power_label.setText(f"{int(self.heater.power_w)} W")
        self.state_label.setText(HeaterSimulator.STATE_NAMES.get(self.heater.state, "?"))
        self.energy_label.setText(f"{self.heater.energy_wh:.1f} Wh")
        regs = self.heater.read_registers(0, 10)
        self.reg_label.setText(" ".join(f"{i}={v}" for i, v in enumerate(regs) if i <= 5 or i >= 8))
        self.last_req_label.setText(
            f"[{self.heater.last_request_time}] {self.heater.last_request_type}"
        )


class CommunityWidget(QGroupBox):
    """
    Energy community (EVG) server stand-in for a debug-build Smartfox.

    Every 10 s it POSTs  http://<device>/api/debug/community/<community_power>/<community_my_surplus>
    like my.smartfox.at answers it: community_power = sum of the members' grid power including ours
    (grid convention, - = feed-in, ours as uploaded about 10 s ago), my_surplus = key * community_power.
    While values keep coming in, the device skips the server fetch.
    """
    SEND_INTERVAL_S = 10
    OWN_DELAY_S     = 10   # our grid power reaches the server via the live view upload

    def __init__(self, own_grid_w, api: SmartfoxApi, parent=None):
        super().__init__("Energiegemeinschaft (Debug-Build: api/debug/community)", parent)
        self.own_grid_w = own_grid_w          # callable: our grid power now (W, + = import)
        self.api        = api
        self.own_hist   = []
        self.tick       = 0
        self.last_result = "-"
        self._setup_ui()

    def _setup_ui(self):
        layout = QGridLayout()
        self.enable_cb = QCheckBox("Senden aktiv (an die Smartfox-IP oben)")
        layout.addWidget(self.enable_cb, 0, 0, 1, 3)

        layout.addWidget(QLabel("Netz übrige Mitglieder:"), 1, 0)
        self.others_spin = QDoubleSpinBox()
        self.others_spin.setRange(-100000.0, 100000.0)
        self.others_spin.setDecimals(0)
        self.others_spin.setSingleStep(500)
        self.others_spin.setSuffix(" W")
        self.others_spin.setValue(-6000.0)
        self.others_spin.setFixedWidth(120)
        layout.addWidget(self.others_spin, 1, 1)
        layout.addWidget(QLabel("(− = Einspeisung)"), 1, 2)

        layout.addWidget(QLabel("Schlüssel:"), 2, 0)
        self.key_spin = QDoubleSpinBox()
        self.key_spin.setRange(0.0, 1.0)
        self.key_spin.setDecimals(3)
        self.key_spin.setSingleStep(0.05)
        self.key_spin.setValue(0.5)
        self.key_spin.setFixedWidth(120)
        layout.addWidget(self.key_spin, 2, 1)

        self.values_label = QLabel("-")
        self.values_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.values_label, 3, 0, 1, 3)
        self.status_label = QLabel("-")
        self.status_label.setStyleSheet("font-family: monospace; color: #444;")
        layout.addWidget(self.status_label, 4, 0, 1, 3)
        self.setLayout(layout)

    def _values(self):
        delayed_own = self.own_hist[0] if self.own_hist else self.own_grid_w()
        community_power = int(delayed_own + self.others_spin.value())
        my_surplus = int(self.key_spin.value() * community_power)
        return delayed_own, community_power, my_surplus

    def tick_1s(self):
        """Called every second by the main window."""
        self.own_hist.append(self.own_grid_w())
        if len(self.own_hist) > self.OWN_DELAY_S:
            self.own_hist.pop(0)

        delayed_own, community_power, my_surplus = self._values()
        expected_s = -self.key_spin.value() * self.others_spin.value()
        self.values_label.setText(
            f"eigenes Netz (vor {self.OWN_DELAY_S} s): {delayed_own:+.0f} W   community_power: {community_power:+d} W   "
            f"my_surplus: {my_surplus:+d} W   →  S erwartet: {expected_s:+.0f} W"
        )
        self.status_label.setText(self.last_result)

        self.tick += 1
        if self.enable_cb.isChecked() and self.tick % self.SEND_INTERVAL_S == 0:
            url = f"http://{self.api.smartfox_ip().strip()}/api/debug/community/{community_power}/{my_surplus}"
            threading.Thread(target=self._send, args=(url,), daemon=True).start()

    def _send(self, url):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self.last_result = f"[{now}] POST {url.rsplit('/api/', 1)[-1]} -> {resp.status}"
        except Exception as e:
            self.last_result = f"[{now}] POST fehlgeschlagen: {e}"
        print(f"[Community] {self.last_result}")


# ---------------------------------------------------------------------------
# Control API (http://127.0.0.1:8020) - drive the simulator from scripts / Claude (mcp_server.py, API.md)
# ---------------------------------------------------------------------------

CONTROL_API_PORT = 8020


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _require(body: dict, key: str, kind, lo=None, hi=None, choices=None):
    """Return body[key] validated, or None if absent."""
    if key not in body:
        return None
    value = body[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if kind is int and isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise ApiError(400, f"'{key}' must be {kind.__name__}")
    if choices is not None and value not in choices:
        raise ApiError(400, f"'{key}' must be one of {list(choices)}")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ApiError(400, f"'{key}' must be within {lo}..{hi}")
    return value


def _reject_unknown(body: dict, allowed: set):
    unknown = set(body) - allowed
    if unknown:
        raise ApiError(400, f"Unknown field(s) {sorted(unknown)}; allowed: {sorted(allowed)}")


class ControlApi:
    """
    Local HTTP JSON API that does what the buttons do. Every request runs in the GUI thread
    (MainWindow.run_in_gui), so the window shows the same state as after a click.

    REST API (used by mcp_server.py, see API.md) - GET/POST/PATCH/DELETE below /api:
      /api/state
      /api/stations[/<id>[/plug_in|/plug_out|/error]]
      /api/heaters[/<slot>]
      /api/batteries[/<id>]
      /api/grid  /api/meter[/reset]  /api/polling  /api/community  /api/smartfox
      /api/configure                   POST {"device": "all"|"meter"|"battery"|"station"|"heater", "id": n, "remove": false}

    Legacy API (kept for existing scripts, every POST returns the full legacy state):
      GET  /state
      POST /surplus        {"w": -3000}        PV surplus input (Unit-ID 100 / meter), - = feed-in
      POST /stations       {"count": 3} | {"clear": true}
      POST /station/<id>   {"action": "plug_in"|"plug_out"|"error", "vehicle_phases": 1-3,
                            "max_power_kw": 11, "manual_amperage": 10 | null}
      POST /heater/<slot>  {"nominal_power_w", "temperature_c", "max_temp_c", "auto_temperature", "force_max_temp"}
      POST /battery        {"soc", "temperature_c", "power_w"}   first battery only
      POST /community      {"enabled": true, "others_w": -16000, "key": 0.5}
      POST /smartfox       {"ip": "192.168.100.170"}
      POST /configure      {"device": "all"|"meter"|"battery"|"station"|"heater", "id": n, "remove": false}
    """

    # (method, path regex) -> method name of this class; regex groups are passed as int args
    ROUTES = [
        ("GET",    r"/api/state",                    "api_state"),
        ("GET",    r"/api/stations",                 "api_list_stations"),
        ("POST",   r"/api/stations",                 "api_add_stations"),
        ("DELETE", r"/api/stations",                 "api_clear_stations"),
        ("GET",    r"/api/stations/(\d+)",           "api_get_station"),
        ("PATCH",  r"/api/stations/(\d+)",           "api_update_station"),
        ("POST",   r"/api/stations/(\d+)/plug_in",   "api_plug_in"),
        ("POST",   r"/api/stations/(\d+)/plug_out",  "api_plug_out"),
        ("POST",   r"/api/stations/(\d+)/error",     "api_station_error"),
        ("GET",    r"/api/heaters",                  "api_list_heaters"),
        ("GET",    r"/api/heaters/(\d+)",            "api_get_heater"),
        ("PATCH",  r"/api/heaters/(\d+)",            "api_update_heater"),
        ("GET",    r"/api/grid",                     "api_get_grid"),
        ("PATCH",  r"/api/grid",                     "api_update_grid"),
        ("GET",    r"/api/meter",                    "api_get_meter"),
        ("POST",   r"/api/meter/reset",              "api_reset_meter"),
        ("GET",    r"/api/batteries",                "api_list_batteries"),
        ("POST",   r"/api/batteries",                "api_add_battery"),
        ("DELETE", r"/api/batteries",                "api_clear_batteries"),
        ("GET",    r"/api/batteries/(\d+)",          "api_get_battery"),
        ("PATCH",  r"/api/batteries/(\d+)",          "api_update_battery"),
        ("GET",    r"/api/polling",                  "api_get_polling"),
        ("PATCH",  r"/api/polling",                  "api_update_polling"),
        ("GET",    r"/api/community",                "api_get_community"),
        ("PATCH",  r"/api/community",                "api_update_community"),
        ("GET",    r"/api/smartfox",                 "api_get_smartfox"),
        ("PATCH",  r"/api/smartfox",                 "api_update_smartfox"),
        ("POST",   r"/api/configure",                "api_configure"),
    ]

    def __init__(self, window, port=CONTROL_API_PORT):
        self.window = window
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj, indent=1).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    if not isinstance(body, dict):
                        raise ApiError(400, "JSON body must be an object")
                    path = self.path.split("?", 1)[0].rstrip("/")
                    if path.startswith("/api"):
                        result = api.window.run_in_gui(lambda: api.rest(method, path, body))
                    else:
                        parts = [x for x in path.split("/") if x]
                        result = api.window.run_in_gui(lambda: api.dispatch(method, parts, body))
                    self._reply(200, result)
                except ApiError as e:
                    self._reply(e.status, {"error": str(e)})
                except json.JSONDecodeError as e:
                    self._reply(400, {"error": f"Invalid JSON: {e}"})
                except (KeyError, ValueError, IndexError) as e:
                    self._reply(400, {"error": f"{type(e).__name__}: {e}"})
                except Exception as e:
                    self._reply(500, {"error": f"{type(e).__name__}: {e}"})

            def do_GET(self):    self._handle("GET")
            def do_POST(self):   self._handle("POST")
            def do_PATCH(self):  self._handle("PATCH")
            def do_DELETE(self): self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"[Control API] listening on http://127.0.0.1:{port}")

    # ---- REST routing (runs in the GUI thread) ----

    def rest(self, method, path, body):
        path_matched = False
        for route_method, pattern, handler in self.ROUTES:
            m = re.fullmatch(pattern, path)
            if not m:
                continue
            path_matched = True
            if route_method != method:
                continue
            args = [int(g) for g in m.groups()]
            fn   = getattr(self, handler)
            return fn(*args) if method == "GET" else fn(*args, body)
        raise ApiError(405 if path_matched else 404, f"No route for {method} {path}")

    # ---- legacy routing (runs in the GUI thread) ----

    def dispatch(self, method, parts, body):
        w = self.window
        if method == "GET" and parts == ["state"]:
            return self.state()
        if method != "POST" or not parts:
            raise KeyError(f"unknown endpoint {method} /{'/'.join(parts)}")

        if parts == ["surplus"]:
            w.surplus_spin.setValue(float(body["w"]))   # also triggers _on_surplus_changed
            w._on_surplus_changed(float(body["w"]))
        elif parts == ["stations"]:
            if body.get("clear"):
                w.clear_stations()
            if "count" in body:
                w.station_count_spin.setValue(int(body["count"]))
                w.add_stations()
        elif parts[0] == "station":
            widget = self._station_widget(int(parts[1]))
            st = widget.station
            action = body.get("action")
            if action == "plug_in":    widget.on_plug_in()
            elif action == "plug_out": widget.on_plug_out()
            elif action == "error":    widget.on_error()
            elif action is not None:   raise ValueError(f"unknown action {action}")
            if "vehicle_phases" in body:
                st.vehicle_phases = int(body["vehicle_phases"])
            if "max_power_kw" in body:
                widget.max_power_spin.setValue(float(body["max_power_kw"]))
            if "manual_amperage" in body:
                if body["manual_amperage"] is None:
                    widget.manual_checkbox.setChecked(False)
                else:
                    widget.amperage_spin.setValue(float(body["manual_amperage"]))
            widget.update_display()
        elif parts[0] == "heater":
            widget = w.heater_widgets[int(parts[1])]
            for key, spin in (("nominal_power_w", widget.nominal_spin), ("temperature_c", widget.temp_spin),
                              ("max_temp_c", widget.max_temp_spin)):
                if key in body:
                    spin.setValue(body[key])
            if "auto_temperature" in body:
                widget.auto_temp_check.setChecked(bool(body["auto_temperature"]))
            if "force_max_temp" in body:
                widget.force_max_check.setChecked(bool(body["force_max_temp"]))
        elif parts == ["battery"]:
            if not w.batteries:
                raise KeyError("no battery")
            widget = self._battery_widget(w.batteries[0].battery_id)
            b = widget.battery
            if "soc" in body:           widget.spins["soc"].setValue(float(body["soc"]))
            if "temperature_c" in body: widget.spins["temperature_c"].setValue(float(body["temperature_c"]))
            if "power_w" in body:       b.actual_power_w = float(body["power_w"])   # ramps back to the firmware setpoint
        elif parts == ["community"]:
            self.api_update_community(body)
        elif parts == ["smartfox"]:
            w.smartfox_ip_edit.setText(str(body["ip"]))
        elif parts == ["configure"]:
            self.api_configure(body)
        else:
            raise KeyError(f"unknown endpoint POST /{'/'.join(parts)}")
        return self.state()

    # ---- widget lookup ----

    def _station_widget(self, station_id):
        for widget in self.window.findChildren(StationWidget):
            if widget.station.station_id == station_id and widget.station in self.window.stations:
                return widget
        raise ApiError(404, f"Station {station_id} not found")

    def _battery_widget(self, battery_id):
        for widget in self.window.findChildren(BatteryWidget):
            if widget.battery.battery_id == battery_id and widget.battery in self.window.batteries:
                return widget
        raise ApiError(404, f"Battery {battery_id} not found")

    def _heater_widget(self, slot):
        if not 0 <= slot < len(self.window.heater_widgets):
            raise ApiError(404, f"Heater {slot} not found")
        return self.window.heater_widgets[slot]

    # ---- JSON views ----

    @staticmethod
    def _station_json(s: ChargingStationSimulator) -> dict:
        return {
            "id":                 s.station_id,
            "status_code":        s.status_code,
            "status":             s.status,
            "is_charging":        s.is_charging,
            "power_kw":           round(s.current_power, 3),
            "current_a":          s.current_amperage,
            "target_current_a":   s.target_amperage,
            "set_current_ma":     s.set_current_ma,
            "phases":             s.phases,
            "vehicle_phases":     s.vehicle_phases,
            "effective_phases":   s.effective_phases,
            "voltage_v":          s.voltage,
            "max_power_kw":       s.max_power,
            "manual_amperage":    s.manual_amperage,
            "error_code":         s.error_code,
            "energy_kwh":         round(s.energy_delivered, 4),
            "session_duration_s": s.session_duration,
            "last_request":       {"time": s.last_request_time, "type": s.last_request_type},
        }

    @staticmethod
    def _battery_json(b: BatterySimulator) -> dict:
        return {
            "id":              b.battery_id,
            "local_ip":        b.local_ip,
            "phases":          b.phases,
            "capacity_kwh":    b.capacity_kwh,
            "soc":             round(b.soc, 3),
            "max_charge_w":    b.max_charge_w,
            "max_discharge_w": b.max_discharge_w,
            "ramp_w_per_s":    b.ramp_w_per_s,
            "delay_s":         b.delay_s,
            "temperature_c":   b.temperature_c,
            "setpoint_w":      b.setpoint_w,
            "setpoint_regs":   list(b.setpoint_regs),
            "actual_power_w":  round(b.actual_power_w, 1),
            "last_request":    {"time": b.last_request_time, "type": b.last_request_type},
        }

    def _heater_json(self, h: HeaterSimulator, widget) -> dict:
        return {
            "slot": h.slot, "unit_id": h.unit_id, "nominal_power_w": h.nominal_power_w,
            "setpoint_w": h.setpoint_w, "power_w": int(h.power_w),
            "state": HeaterSimulator.STATE_NAMES.get(h.state),
            "temperature_c": round(h.temperature_c, 1), "max_temp_c": h.max_temp_c,
            "auto_temperature": h.auto_temperature, "force_max_temp": h.force_max_temp,
            "energy_wh": round(h.energy_wh, 1),
            "last_request": {"time": h.last_request_time, "type": h.last_request_type},
            "config_status": widget.config_bar.status,
        }

    # ---- REST handlers: state ----

    def api_state(self) -> dict:
        w = self.window
        return {
            "modbus_ports": list(MODBUS_PORTS),
            "sim_ip":       w.ip_combo.currentText(),
            "grid":         self.api_get_grid(),
            "meter":        self.api_get_meter(),
            "stations":     self.api_list_stations(),
            "heaters":      self.api_list_heaters(),
            "batteries":    self.api_list_batteries(),
            "polling":      self.api_get_polling(),
            "community":    self.api_get_community(),
            "smartfox":     self.api_get_smartfox(),
        }

    # ---- stations ----

    def api_list_stations(self) -> list:
        return [self._station_json(s) for s in self.window.stations]

    def api_get_station(self, station_id: int) -> dict:
        return self._station_json(self._station_widget(station_id).station)

    def api_add_stations(self, body: dict) -> list:
        _reject_unknown(body, {"count"})
        count  = _require(body, "count", int, 1, 16) or 1
        w      = self.window
        before = len(w.stations)
        w.station_count_spin.setValue(count)
        w.add_stations()
        return [self._station_json(s) for s in w.stations[before:]]

    def api_clear_stations(self, body: dict) -> dict:
        self.window.clear_stations()
        return {"ok": True}

    def api_plug_in(self, station_id: int, body: dict) -> dict:
        widget = self._station_widget(station_id)
        if widget.station.status_code == ChargingStationSimulator.STATUS_ERROR:
            raise ApiError(409, "Station is in error state, plug out first")
        widget.on_plug_in()
        return self._station_json(widget.station)

    def api_plug_out(self, station_id: int, body: dict) -> dict:
        widget = self._station_widget(station_id)
        widget.on_plug_out()
        return self._station_json(widget.station)

    def api_station_error(self, station_id: int, body: dict) -> dict:
        _reject_unknown(body, {"error_code"})
        widget = self._station_widget(station_id)
        widget.on_error()
        error_code = _require(body, "error_code", int, 0, 0xFFFF)
        if error_code is not None:
            widget.station.error_code = error_code
        return self._station_json(widget.station)

    def api_update_station(self, station_id: int, body: dict) -> dict:
        _reject_unknown(body, {"status_code", "max_power_kw", "amperage_a", "manual_amperage", "error_code",
                               "vehicle_phases"})
        widget  = self._station_widget(station_id)
        station = widget.station
        status  = _require(body, "status_code", int, choices=ChargingStationSimulator.STATUS_NAMES)
        max_kw  = _require(body, "max_power_kw", float, 1.0, 22.0)
        amps    = _require(body, "amperage_a", float, 0.0, 32.0)
        manual  = _require(body, "manual_amperage", bool)
        error   = _require(body, "error_code", int, 0, 0xFFFF)
        vphases = _require(body, "vehicle_phases", int, choices=(1, 2, 3))
        if status is not None:
            station.set_status(status)
        if max_kw is not None:
            widget.max_power_spin.setValue(max_kw)
        if amps is not None:
            widget.amperage_spin.setValue(amps)
            widget.update_amperage(amps)   # also when the spin already shows this value
        if manual is not None:
            widget.manual_checkbox.setChecked(manual)
        if error is not None:
            station.error_code = error
        if vphases is not None:
            station.vehicle_phases = vphases
        widget.update_display()
        return self._station_json(station)

    # ---- heaters ----

    def api_list_heaters(self) -> list:
        w = self.window
        return [self._heater_json(h, hw) for h, hw in zip(w.heaters, w.heater_widgets)]

    def api_get_heater(self, slot: int) -> dict:
        widget = self._heater_widget(slot)
        return self._heater_json(widget.heater, widget)

    def api_update_heater(self, slot: int, body: dict) -> dict:
        _reject_unknown(body, {"nominal_power_w", "temperature_c", "max_temp_c", "auto_temperature", "force_max_temp"})
        widget  = self._heater_widget(slot)
        nominal = _require(body, "nominal_power_w", int, 0, 30000)
        temp    = _require(body, "temperature_c", float, 0.0, 99.0)
        max_t   = _require(body, "max_temp_c", float, 20.0, 99.0)
        auto    = _require(body, "auto_temperature", bool)
        force   = _require(body, "force_max_temp", bool)
        if nominal is not None: widget.nominal_spin.setValue(nominal)
        if temp is not None:    widget.temp_spin.setValue(temp)
        if max_t is not None:   widget.max_temp_spin.setValue(max_t)
        if auto is not None:    widget.auto_temp_check.setChecked(auto)
        if force is not None:   widget.force_max_check.setChecked(force)
        return self._heater_json(widget.heater, widget)

    # ---- grid / surplus ----

    def api_get_grid(self) -> dict:
        w = self.window
        return {
            "surplus_input_w":     w._base_surplus,
            "surplus_effective_w": w.surplus_power,
            "grid_total_w":        w.meter_total_w(),
            "unit100_w":           round(w.grid_power_w(), 1),
            "charger_total_w":     round(sum(s.current_power * 1000 for s in w.stations), 1),
            "heater_total_w":      round(sum(h.power_w for h in w.heaters), 1),
            "battery_total_w":     round(w.battery_total_w(), 1),
            "noise_enabled":       w.noise_cb.isChecked(),
            "noise_amplitude_w":   w.noise_spin.value(),
            "noise_w":             round(w.noise_w, 1),
        }

    def api_update_grid(self, body: dict) -> dict:
        _reject_unknown(body, {"surplus_w", "noise_enabled", "noise_amplitude_w"})
        w         = self.window
        surplus   = _require(body, "surplus_w", float, -32768.0, 32767.0)
        noise_on  = _require(body, "noise_enabled", bool)
        amplitude = _require(body, "noise_amplitude_w", int, 0, 10000)
        if surplus is not None:
            if w.poller and w.surplus_source_combo.currentData():
                raise ApiError(409, "Surplus is driven by device polling; set polling surplus_source to null first")
            w.surplus_spin.setValue(surplus)
            w._on_surplus_changed(surplus)
        if noise_on is not None:
            w.noise_cb.setChecked(noise_on)
        if amplitude is not None:
            w.noise_spin.setValue(amplitude)
        return self.api_get_grid()

    # ---- meter ----

    def api_get_meter(self) -> dict:
        m = self.window.meter
        return {
            "total_power_w":     m.total_power_w,
            "phase_powers_w":    list(m.phase_powers_w),
            "phase_currents_ma": list(m.phase_currents_ma),
            "energy_forward_wh": round(m.energy_forward_wh, 3),
            "energy_reverse_wh": round(m.energy_reverse_wh, 3),
            "smartfox_slot":     self.window.meter_widget.slot_spin.value(),
            "last_request":      {"time": m.last_request_time, "type": m.last_request_type},
            "config_status":     self.window.meter_widget.config_bar.status,
        }

    def api_reset_meter(self, body: dict) -> dict:
        _reject_unknown(body, {"forward", "reverse"})
        m = self.window.meter
        if _require(body, "forward", bool) is not False:
            m.energy_forward_wh = 0.0
        if _require(body, "reverse", bool) is not False:
            m.energy_reverse_wh = 0.0
        return self.api_get_meter()

    # ---- batteries ----

    def api_list_batteries(self) -> list:
        return [self._battery_json(b) for b in self.window.batteries]

    def api_get_battery(self, battery_id: int) -> dict:
        return self._battery_json(self._battery_widget(battery_id).battery)

    def api_add_battery(self, body: dict) -> dict:
        w = self.window
        w.add_battery()
        battery = w.batteries[-1]
        if body:
            return self.api_update_battery(battery.battery_id, body)
        return self._battery_json(battery)

    def api_clear_batteries(self, body: dict) -> dict:
        self.window.clear_batteries()
        return {"ok": True}

    def api_update_battery(self, battery_id: int, body: dict) -> dict:
        limits = {
            "capacity_kwh":    (float, 0.1, 200.0),
            "soc":             (float, 0.0, 100.0),
            "temperature_c":   (float, -40.0, 100.0),
            "max_charge_w":    (int, 0, 30000),
            "max_discharge_w": (int, 0, 30000),
            "ramp_w_per_s":    (int, 1, 30000),
            "delay_s":         (int, 0, 30),
        }
        _reject_unknown(body, set(limits) | {"phases", "local_ip"})
        widget  = self._battery_widget(battery_id)
        values  = {k: _require(body, k, *spec) for k, spec in limits.items()}
        phases  = _require(body, "phases", int, choices=(1, 3))
        ip      = _require(body, "local_ip", str)
        for attr, value in values.items():
            if value is not None:
                widget.spins[attr].setValue(value)
                setattr(widget.battery, attr, value)
        if phases is not None:
            widget.phases_combo.setCurrentIndex(widget.phases_combo.findData(phases))
        if ip is not None:
            widget.ip_edit.setText(ip.strip())
        widget.update_display()
        return self._battery_json(widget.battery)

    # ---- device polling (Smartfox JSON API, IP = Smartfox-IP) ----

    def api_get_polling(self) -> dict:
        w = self.window
        values, poll_time, error = w.poller.get() if w.poller else ({}, None, None)
        return {
            "enabled":           w.poller is not None,
            "ip":                w.smartfox_ip_edit.text().strip(),
            "interval_s":        w.poll_interval_spin.value(),
            "surplus_source":    w.surplus_source_combo.currentData(),
            "heater_in_surplus": w.heater_in_surplus_cb.isChecked(),
            "last_poll_time":    poll_time,
            "last_error":        error,
            "values":            values,
        }

    def api_update_polling(self, body: dict) -> dict:
        _reject_unknown(body, {"enabled", "ip", "interval_s", "surplus_source", "heater_in_surplus"})
        w        = self.window
        sources  = [w.surplus_source_combo.itemData(i) for i in range(w.surplus_source_combo.count())]
        enabled  = _require(body, "enabled", bool)
        ip       = _require(body, "ip", str)
        interval = _require(body, "interval_s", int, 1, 60)
        heater   = _require(body, "heater_in_surplus", bool)
        if "surplus_source" in body and body["surplus_source"] not in sources:
            raise ApiError(400, f"'surplus_source' must be one of {sources}")
        if (ip is not None or interval is not None) and w.poller and enabled is not False:
            raise ApiError(409, "Disable polling before changing ip/interval_s")
        if enabled is False:
            w.poll_enable_cb.setChecked(False)
        if ip is not None:
            w.smartfox_ip_edit.setText(ip.strip())
        if interval is not None:
            w.poll_interval_spin.setValue(interval)
        if "surplus_source" in body:
            w.surplus_source_combo.setCurrentIndex(sources.index(body["surplus_source"]))
        if heater is not None:
            w.heater_in_surplus_cb.setChecked(heater)
        if enabled:
            if not w.smartfox_ip_edit.text().strip():
                raise ApiError(400, "Set 'ip' to enable polling")
            w.poll_enable_cb.setChecked(True)
        return self.api_get_polling()

    # ---- energy community ----

    def api_get_community(self) -> dict:
        c = self.window.community_widget
        delayed_own, community_power, my_surplus = c._values()
        return {
            "enabled": c.enable_cb.isChecked(), "others_w": c.others_spin.value(), "key": c.key_spin.value(),
            "own_delayed_w": delayed_own, "community_power": community_power, "my_surplus": my_surplus,
            "expected_share_w": -c.key_spin.value() * c.others_spin.value(),
            "last_result": c.last_result,
        }

    def api_update_community(self, body: dict) -> dict:
        _reject_unknown(body, {"enabled", "others_w", "key"})
        c       = self.window.community_widget
        others  = _require(body, "others_w", float, -100000.0, 100000.0)
        key     = _require(body, "key", float, 0.0, 1.0)
        enabled = _require(body, "enabled", bool)
        if others is not None:  c.others_spin.setValue(others)
        if key is not None:     c.key_spin.setValue(key)
        if enabled is not None: c.enable_cb.setChecked(enabled)
        return self.api_get_community()

    # ---- Smartfox under test ----

    def api_get_smartfox(self) -> dict:
        w = self.window
        return {"smartfox_ip": w.smartfox_ip_edit.text(), "sim_ip": w.ip_combo.currentText()}

    def api_update_smartfox(self, body: dict) -> dict:
        _reject_unknown(body, {"ip"})
        ip = _require(body, "ip", str)
        if ip is not None:
            self.window.smartfox_ip_edit.setText(ip.strip())
        return self.api_get_smartfox()

    def api_configure(self, body: dict) -> dict:
        """Writes the device configuration to the Smartfox flash (same as the 'Konfigurieren' buttons)."""
        _reject_unknown(body, {"device", "id", "remove"})
        w      = self.window
        device = _require(body, "device", str, choices=("all", "meter", "battery", "station", "heater")) or "all"
        ident  = _require(body, "id", int)
        remove = _require(body, "remove", bool)
        if device == "all":
            w.configure_all()
            return {"ok": True, "device": "all"}
        if device != "meter" and ident is None:
            raise ApiError(400, f"'id' is required for device '{device}'")
        bar = {
            "meter":   lambda: w.meter_widget.config_bar,
            "battery": lambda: self._battery_widget(ident).config_bar,
            "station": lambda: self._station_widget(ident).config_bar,
            "heater":  lambda: self._heater_widget(ident).config_bar,
        }[device]()
        bar.remove() if remove else bar.configure()
        return {"ok": True, "device": device, "id": ident}

    # ---- legacy state ----

    def state(self):
        w = self.window
        stations = []
        for st in w.stations:
            widget = self._station_widget(st.station_id)
            stations.append({
                "id": st.station_id, "status": st.status, "set_current_ma": st.set_current_ma,
                "amperage": st.current_amperage, "power_kw": round(st.current_power, 3),
                "phases": st.phases, "vehicle_phases": st.vehicle_phases, "max_power_kw": st.max_power,
                "manual_amperage": st.manual_amperage, "energy_kwh": round(st.energy_delivered, 3),
                "last_request": f"[{st.last_request_time}] {st.last_request_type}",
                "config_status": widget.config_bar.status,
            })
        heaters = [{
            "slot": h.slot, "unit_id": h.unit_id, "nominal_power_w": h.nominal_power_w,
            "setpoint_w": h.setpoint_w, "power_w": int(h.power_w), "state": HeaterSimulator.STATE_NAMES.get(h.state),
            "temperature_c": round(h.temperature_c, 1), "max_temp_c": h.max_temp_c,
            "auto_temperature": h.auto_temperature, "force_max_temp": h.force_max_temp,
            "last_request": f"[{h.last_request_time}] {h.last_request_type}",
            "config_status": hw.config_bar.status,
        } for h, hw in zip(w.heaters, w.heater_widgets)]
        batteries = []
        for b in w.batteries:
            item = self._battery_json(b)
            item["config_status"] = self._battery_widget(b.battery_id).config_bar.status
            batteries.append(item)
        return {
            "sim_ip": w.ip_combo.currentText(), "smartfox_ip": w.smartfox_ip_edit.text(),
            "surplus_w": w.surplus_power, "grid_w": w.grid_power_w(), "meter_total_w": w.meter.total_power_w,
            "stations": stations, "heaters": heaters, "batteries": batteries,
            "meter": {"total_power_w": w.meter.total_power_w, "phase_powers_w": w.meter.phase_powers_w,
                      "smartfox_slot": w.meter_widget.slot_spin.value(),
                      "config_status": w.meter_widget.config_bar.status},
            "community": self.api_get_community(),
        }


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.stations      = []
        self.batteries: list[BatterySimulator] = []
        self.noise_w       = 0.0   # current random load noise, re-drawn every second
        self.server_thread = None
        self._base_surplus = 0.0   # raw spinner / polled source value
        self.surplus_power = 0.0   # effective = _base_surplus + heater offset
        self.meter         = SmartfoxSlaveSimulator()
        self.poller: DevicePoller | None = None
        self.heaters       = [HeaterSimulator(slot) for slot in range(HEATER_COUNT)]
        # Widgets are read when a button is clicked, so the lambdas may refer to them before setup_ui()
        self.api           = SmartfoxApi(lambda: self.smartfox_ip_edit.text(), lambda: self.ip_combo.currentText())
        self.gui_calls     = queue.Queue()   # (fn, result queue) from the control API threads
        self.setup_ui()
        self.start_servers()
        self.start_update_timer()
        self.add_stations()
        self.add_battery()

    def setup_ui(self):
        self.setWindowTitle("EV Charging Station + Smartfox Slave + Victron Battery + Heater Simulator")
        self.setGeometry(100, 100, 1200, 900)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # ---- IP / server info ----
        self.available_ips = self._get_local_ips()
        info_layout = QHBoxLayout()
        info_layout.addWidget(QLabel("Modbus TCP:"))
        self.ip_combo = QComboBox()
        self.ip_combo.addItems(self.available_ips)
        self.ip_combo.setFixedWidth(150)
        self.ip_combo.currentTextChanged.connect(self._on_ip_changed)
        info_layout.addWidget(self.ip_combo)
        info_layout.addWidget(QLabel(":5020 / :502   (chargers: unit-ID = station ID  |  heaters: unit-ID 210-212  |  Victron battery: unit-ID 0/242/246/227  |  meter: addr ≥ 40000)"))
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedWidth(50)
        copy_btn.clicked.connect(self._copy_ip)
        info_layout.addWidget(copy_btn)
        info_layout.addStretch()
        main_layout.addLayout(info_layout)

        # ---- Smartfox under test (configured over its JSON API) ----
        sf_layout = QHBoxLayout()
        sf_layout.addWidget(QLabel("Smartfox-IP:"))
        self.smartfox_ip_edit = QLineEdit("192.168.100.170")
        self.smartfox_ip_edit.setFixedWidth(150)
        sf_layout.addWidget(self.smartfox_ip_edit)
        sf_layout.addWidget(QLabel("(\"Konfigurieren\" stellt das Gerät dort mit der Simulator-IP oben ein)"))
        config_all_btn = QPushButton("Alle konfigurieren")
        config_all_btn.clicked.connect(self.configure_all)
        sf_layout.addWidget(config_all_btn)
        sf_layout.addStretch()
        main_layout.addLayout(sf_layout)

        # ---- Tabs ----
        tabs = QTabWidget()

        # Charger tab
        charger_tab = QWidget()
        charger_layout = QVBoxLayout(charger_tab)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(QLabel("Number of Stations:"))
        self.station_count_spin = QSpinBox()
        self.station_count_spin.setRange(1, 16)
        self.station_count_spin.setValue(3)
        ctrl_layout.addWidget(self.station_count_spin)
        add_btn = QPushButton("Add Stations")
        add_btn.clicked.connect(self.add_stations)
        ctrl_layout.addWidget(add_btn)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_stations)
        ctrl_layout.addWidget(clear_btn)

        surplus_group = QGroupBox("PV-Überschuss (Unit-ID 100)")
        surplus_layout = QHBoxLayout()
        surplus_layout.setContentsMargins(6, 4, 6, 4)
        self.surplus_spin = QDoubleSpinBox()
        self.surplus_spin.setRange(-32768.0, 32767.0)
        self.surplus_spin.setDecimals(0)
        self.surplus_spin.setSuffix(" W")
        self.surplus_spin.setValue(0.0)
        self.surplus_spin.setFixedWidth(120)
        self.surplus_spin.valueChanged.connect(self._on_surplus_changed)
        surplus_layout.addWidget(self.surplus_spin)
        surplus_layout.addWidget(QLabel("Eingabe:"))
        self.surplus_display = QLabel("0 W")
        self.surplus_display.setStyleSheet("font-family: monospace; color: #444;")
        surplus_layout.addWidget(self.surplus_display)
        surplus_layout.addWidget(QLabel("→  Ausgabe:"))
        self.surplus_result_label = QLabel("0 W")
        self.surplus_result_label.setStyleSheet("font-family: monospace; font-weight: bold; color: #1a7a1a;")
        surplus_layout.addWidget(self.surplus_result_label)
        surplus_group.setLayout(surplus_layout)
        ctrl_layout.addWidget(surplus_group)
        ctrl_layout.addStretch()
        charger_layout.addLayout(ctrl_layout)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.stations_container = QWidget()
        self.stations_layout = QGridLayout(self.stations_container)
        self.scroll.setWidget(self.stations_container)
        charger_layout.addWidget(self.scroll)

        tabs.addTab(charger_tab, "Chargers (port 5020)")

        # Meter tab
        meter_tab = QWidget()
        meter_layout = QVBoxLayout(meter_tab)
        self.meter_widget = MeterWidget(self.meter, self.api)
        meter_layout.addWidget(self.meter_widget)
        meter_layout.addStretch()
        tabs.addTab(meter_tab, "Energy Meter (addr ≥ 40000)")

        # Battery tab
        battery_tab = QWidget()
        battery_layout = QVBoxLayout(battery_tab)

        batt_ctrl = QHBoxLayout()
        add_batt_btn = QPushButton("Add Battery")
        add_batt_btn.clicked.connect(self.add_battery)
        batt_ctrl.addWidget(add_batt_btn)
        clear_batt_btn = QPushButton("Clear Batteries")
        clear_batt_btn.clicked.connect(self.clear_batteries)
        batt_ctrl.addWidget(clear_batt_btn)

        noise_group = QGroupBox("Load noise (added to grid power)")
        noise_layout = QHBoxLayout()
        noise_layout.setContentsMargins(6, 4, 6, 4)
        self.noise_cb = QCheckBox("Enable")
        noise_layout.addWidget(self.noise_cb)
        noise_layout.addWidget(QLabel("± amplitude:"))
        self.noise_spin = QSpinBox()
        self.noise_spin.setRange(0, 10000)
        self.noise_spin.setValue(150)
        self.noise_spin.setSuffix(" W")
        noise_layout.addWidget(self.noise_spin)
        self.noise_label = QLabel("0 W")
        self.noise_label.setStyleSheet("font-family: monospace;")
        noise_layout.addWidget(self.noise_label)
        noise_group.setLayout(noise_layout)
        batt_ctrl.addWidget(noise_group)
        batt_ctrl.addStretch()
        battery_layout.addLayout(batt_ctrl)

        battery_layout.addWidget(QLabel(
            "Firmware: product Victron VE CTRL (bs_victron.c, port 502 or 5020), IP = this PC. "
            "Several batteries: give each its own local IP (IP alias on this PC)."
        ))

        batt_scroll = QScrollArea()
        batt_scroll.setWidgetResizable(True)
        self.batteries_container = QWidget()
        self.batteries_layout = QHBoxLayout(self.batteries_container)
        self.batteries_layout.addStretch()
        batt_scroll.setWidget(self.batteries_container)
        battery_layout.addWidget(batt_scroll)

        tabs.addTab(battery_tab, "Victron Batteries (Unit-ID 0/242/246/227)")

        # Heater tab
        heater_tab = QWidget()
        heater_layout = QHBoxLayout(heater_tab)
        self.heater_widgets = []
        for heater in self.heaters:
            widget = HeaterWidget(heater, self.api)
            self.heater_widgets.append(widget)
            heater_layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignTop)
        heater_layout.addStretch()
        tabs.addTab(heater_tab, "Heaters (Unit-ID 210-212)")

        # Energy community tab
        community_tab = QWidget()
        community_layout = QVBoxLayout(community_tab)
        self.community_widget = CommunityWidget(self.grid_power_w, self.api)
        community_layout.addWidget(self.community_widget)
        community_layout.addStretch()
        tabs.addTab(community_tab, "Energiegemeinschaft")

        # Device polling tab (reads the real Smartfox JSON API at the Smartfox-IP above)
        poll_tab = QWidget()
        poll_outer = QVBoxLayout(poll_tab)

        conn_group = QGroupBox("Device Connection (IP = Smartfox-IP above)")
        conn_gl = QGridLayout()
        conn_gl.addWidget(QLabel("Interval (s):"), 0, 0)
        self.poll_interval_spin = QSpinBox()
        self.poll_interval_spin.setRange(1, 60)
        self.poll_interval_spin.setValue(5)
        conn_gl.addWidget(self.poll_interval_spin, 0, 1)
        self.poll_enable_cb = QCheckBox("Enable polling")
        self.poll_enable_cb.stateChanged.connect(self._on_poll_toggle)
        conn_gl.addWidget(self.poll_enable_cb, 1, 0, 1, 2)
        conn_group.setLayout(conn_gl)
        poll_outer.addWidget(conn_group)

        apply_group = QGroupBox("Auto-apply to simulator")
        apply_gl = QGridLayout()
        apply_gl.addWidget(QLabel("Surplus source:"), 0, 0)
        self.surplus_source_combo = QComboBox()
        self.surplus_source_combo.addItem("Manual (disabled)", userData=None)
        self.surplus_source_combo.addItem("detailsPowerValue  (W, pos=consuming)", userData="detailsPowerValue_neg")
        self.surplus_source_combo.addItem("toGridValue  (kW, pos=feeding/surplus)", userData="toGridValue_kw")
        apply_gl.addWidget(self.surplus_source_combo, 0, 1)
        self.heater_in_surplus_cb = QCheckBox("Add heater + analog output power to surplus (×1000 → W)")
        self.heater_in_surplus_cb.setToolTip(
            "Adds consumptionControl1/2/3Power and analogOutPower (kW × 1000)\n"
            "to the surplus, making that power available again for car charging."
        )
        apply_gl.addWidget(self.heater_in_surplus_cb, 1, 0, 1, 2)
        apply_group.setLayout(apply_gl)
        poll_outer.addWidget(apply_group)

        vals_group = QGroupBox("Polled Values")
        vals_gl = QGridLayout()
        vals_gl.addWidget(QLabel("<b>Status:</b>"), 0, 0)
        self.poll_status_label = QLabel("Idle")
        vals_gl.addWidget(self.poll_status_label, 0, 1)
        self._poll_labels: dict[str, QLabel] = {}
        _display_fields = [
            ("detailsPowerValue", "Total Power (W)"),
            ("toGridValue",       "Grid Power (kW)"),
            ("powerL1Value",      "Power L1 (W)"),
            ("powerL2Value",      "Power L2 (W)"),
            ("powerL3Value",      "Power L3 (W)"),
            ("ampereL1Value",     "Current L1 (A)"),
            ("ampereL2Value",     "Current L2 (A)"),
            ("ampereL3Value",     "Current L3 (A)"),
            ("cc1Power",          "CC1 Power (kW)"),
            ("cc2Power",          "CC2 Power (kW)"),
            ("cc3Power",          "CC3 Power (kW)"),
            ("cc4Power",          "CC4 Power (kW)"),
            ("cc5Power",          "CC5 Power (kW)"),
            ("hidCcSt1",                  "CC1 State"),
            ("hidCcSt2",                  "CC2 State"),
            ("hidCcSt3",                  "CC3 State"),
            ("hidCcSt4",                  "CC4 State"),
            ("hidCcSt5",                  "CC5 State"),
            ("consumptionControl1Power",  "Heater 1 Power"),
            ("consumptionControl2Power",  "Heater 2 Power"),
            ("consumptionControl3Power",  "Heater 3 Power"),
            ("analogOutPower",            "Analogausgang (kW)"),
            ("batt1Power",                "Battery 1 Power (W)"),
            ("batt2Power",                "Battery 2 Power (W)"),
            ("batt3Power",                "Battery 3 Power (W)"),
        ]
        for row, (fid, caption) in enumerate(_display_fields, start=1):
            vals_gl.addWidget(QLabel(f"{caption}:"), row, 0)
            lbl = QLabel("—")
            lbl.setStyleSheet("font-family: monospace;")
            vals_gl.addWidget(lbl, row, 1)
            self._poll_labels[fid] = lbl
        vals_group.setLayout(vals_gl)
        vals_scroll = QScrollArea()
        vals_scroll.setWidget(vals_group)
        vals_scroll.setWidgetResizable(True)
        poll_outer.addWidget(vals_scroll)
        tabs.addTab(poll_tab, "Device Polling")

        main_layout.addWidget(tabs)

    @staticmethod
    def _get_local_ips():
        """Return all non-loopback IPv4 addresses from all network interfaces."""
        ips = []
        try:
            import netifaces   # optional: no wheel for newer Pythons, socket fallback below
            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr in addrs[netifaces.AF_INET]:
                        ip = addr.get('addr', '')
                        if ip and not ip.startswith("127."):
                            ips.append(ip)
        except Exception:
            # Fallback: use socket method
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ips.append(s.getsockname()[0])
                s.close()
            except Exception:
                pass
        if not ips:
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    ip = info[4][0]
                    if not ip.startswith("127.") and ip not in ips:
                        ips.append(ip)
            except Exception:
                pass
        return ips or ["127.0.0.1"]

    def add_stations(self):
        count    = self.station_count_spin.value()
        start_id = len(self.stations) + 1
        for i in range(count):
            station_id = start_id + i
            station    = ChargingStationSimulator(station_id)
            self.stations.append(station)
            row    = len(self.stations) // 4
            col    = (len(self.stations) - 1) % 4
            widget = StationWidget(station, self.api)
            widget.window = self
            self.stations_layout.addWidget(widget, row, col)

    def configure_all(self):
        """Grid meter, all stations (up to the Smartfox slots), heaters and batteries. Heater 0 first:
        slots 1/2 only start on a change when slot 0 is a debug heater already."""
        bars = [self.meter_widget.config_bar]
        bars += [w.config_bar for w in self.findChildren(StationWidget) if w.station.station_id <= SF_CC_SLOTS]
        bars += [w.config_bar for w in self.heater_widgets]
        bars += [w.config_bar for w in self.findChildren(BatteryWidget) if w.battery in self.batteries]
        # One after another (parallel POSTs would collide in the small httpd); the bars read Qt widgets,
        # so they are started from a GUI-thread timer, not from a worker thread.
        self._config_queue = bars
        self._config_current = None
        if not hasattr(self, "_config_timer"):
            self._config_timer = QTimer(self)
            self._config_timer.timeout.connect(self._configure_next)
        self._config_timer.start(200)

    def _configure_next(self):
        if self._config_current is not None and self._config_current.busy:
            return
        if not self._config_queue:
            self._config_timer.stop()
            self._config_current = None
            return
        self._config_current = self._config_queue.pop(0)
        self._config_current.configure()

    def add_battery(self):
        battery = BatterySimulator(len(self.batteries) + 1)
        self.batteries.append(battery)
        self.batteries_layout.insertWidget(self.batteries_layout.count() - 1, BatteryWidget(battery, self.api))

    def clear_batteries(self):
        self.batteries.clear()
        while self.batteries_layout.count() > 1:
            widget = self.batteries_layout.takeAt(0).widget()
            if widget:
                widget.deleteLater()

    def battery_total_w(self) -> float:
        return sum(b.actual_power_w for b in self.batteries)

    def clear_stations(self):
        self.stations.clear()
        while self.stations_layout.count():
            item   = self.stations_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def start_servers(self):
        self.server_thread = CombinedServerThread(self)
        self.server_thread.start()
        self.gui_call_timer = QTimer(self)
        self.gui_call_timer.timeout.connect(self._run_gui_calls)
        self.gui_call_timer.start(50)
        try:
            self.control_api = ControlApi(self)
        except OSError as e:
            print(f"[Control API] port {CONTROL_API_PORT} not available: {e}")

    def run_in_gui(self, fn, timeout=5.0):
        """Runs fn in the GUI thread and returns its result (called from other threads)."""
        result = queue.Queue(maxsize=1)
        self.gui_calls.put((fn, result))
        ok, value = result.get(timeout=timeout)
        if not ok:
            raise value
        return value

    def _run_gui_calls(self):
        while not self.gui_calls.empty():
            fn, result = self.gui_calls.get()
            try:
                result.put((True, fn()))
            except Exception as e:
                result.put((False, e))

    def start_update_timer(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_all)
        self.timer.start(1000)

    def _on_ip_changed(self, ip: str):
        """Handle IP selection change."""
        pass  # Currently just for display; can be used for binding if needed

    def _copy_ip(self):
        """Copy selected IP to clipboard."""
        QApplication.clipboard().setText(self.ip_combo.currentText())

    def _dac_offset_w(self, values: dict) -> float:
        if not self.heater_in_surplus_cb.isChecked():
            return 0.0
        return values.get("analogOutPower", 0.0) * 1000.0

    def _heater_offset_w(self, values: dict) -> float:
        if not self.heater_in_surplus_cb.isChecked():
            return 0.0
        return sum(values.get(f"consumptionControl{i}Power", 0.0) * 1000.0 for i in range(1, 4))

    def _recompute_effective_surplus(self):
        values = {}
        if self.poller:
            values, _, _ = self.poller.get()
        self.surplus_power = self._base_surplus + self._heater_offset_w(values) + self._dac_offset_w(values)

    def _on_surplus_changed(self, value: float):
        self._base_surplus = value
        self._recompute_effective_surplus()
        self.update_surplus_display()

    def _on_poll_toggle(self, state):
        if state == Qt.CheckState.Checked.value:
            ip       = self.smartfox_ip_edit.text().strip()
            interval = self.poll_interval_spin.value()
            if not ip:
                self.poll_enable_cb.blockSignals(True)
                self.poll_enable_cb.setChecked(False)
                self.poll_enable_cb.blockSignals(False)
                self.poll_status_label.setText("Error: no Smartfox-IP entered")
                self.poll_status_label.setStyleSheet("color: red;")
                return
            self.poller = DevicePoller(ip, interval)
            self.poller.start()
            self.poll_interval_spin.setEnabled(False)
        else:
            if self.poller:
                self.poller.stop()
                self.poller = None
            self.poll_interval_spin.setEnabled(True)
            self.poll_status_label.setText("Idle")
            self.poll_status_label.setStyleSheet("")
            self._recompute_effective_surplus()

    def _update_poll_display(self):
        if self.poller is None:
            return
        values, poll_time, error = self.poller.get()

        if error:
            self.poll_status_label.setText(f"Error: {error}")
            self.poll_status_label.setStyleSheet("color: red;")
        else:
            self.poll_status_label.setText(f"OK  @ {poll_time}")
            self.poll_status_label.setStyleSheet("color: green;")

        for fid, lbl in self._poll_labels.items():
            val = values.get(fid)
            lbl.setText(str(val) if val is not None else "—")

        source = self.surplus_source_combo.currentData()
        if source == "detailsPowerValue_neg" and "detailsPowerValue" in values:
            self._base_surplus = -values["detailsPowerValue"]
        elif source == "toGridValue_kw" and "toGridValue" in values:
            self._base_surplus = values["toGridValue"] * 1000.0

        self.surplus_power = self._base_surplus + self._heater_offset_w(values) + self._dac_offset_w(values)

    def _compute_phase_values(self):
        """Build per-phase power (W) and current (mA) from chargers, battery and positive surplus."""
        phase_powers   = [0, 0, 0]
        phase_currents = [0, 0, 0]

        # Positive surplus: spread evenly across all 3 phases
        if self.surplus_power > 0:
            per_phase = int(self.surplus_power / 3)
            for i in range(3):
                phase_powers[i] += per_phase

        for s in self.stations:
            if not s.is_charging:
                continue
            power_w    = int(s.current_amperage * s.voltage)  # W on each active phase
            current_ma = int(s.current_amperage * 1000)
            indices    = range(s.effective_phases)             # [0], [0,1] or [0,1,2]
            for i in indices:
                phase_powers[i]   += power_w
                phase_currents[i] += current_ma

        # Heaters: spread evenly across all 3 phases
        heater_total_w = sum(int(h.power_w) for h in self.heaters)
        if heater_total_w:
            for i in range(3):
                phase_powers[i]   += int(heater_total_w / 3)
                phase_currents[i] += int((heater_total_w / 3) / 230.0 * 1000.0)

        # bs_victron.c writes the same setpoint to every phase register: 3-phase batteries split evenly
        for b in self.batteries:
            if b.phases == 3:
                for i in range(3):
                    phase_powers[i]   += int(b.actual_power_w / 3)
                    phase_currents[i] += int((b.actual_power_w / 3) / 230.0 * 1000.0)
            else:
                phase_powers[0]   += int(b.actual_power_w)
                phase_currents[0] += int(b.actual_power_w / 230.0 * 1000.0)
        for i in range(3):
            phase_powers[i] += int(self.noise_w / 3)

        self.meter.phase_powers_w    = phase_powers
        self.meter.phase_currents_ma = phase_currents

    def update_surplus_display(self):
        self.surplus_display.setText(f"{int(self._base_surplus):+d} W")
        self.surplus_spin.blockSignals(True)
        self.surplus_spin.setValue(self._base_surplus)
        self.surplus_spin.blockSignals(False)
        result_w        = self.meter_total_w()
        self.surplus_result_label.setText(f"{result_w:+d} W")
        self.meter.total_power_w = result_w
        self._compute_phase_values()

    def grid_power_w(self) -> float:
        """Our grid power as Unit-ID 100 reports it (W, + = import, - = feed-in)."""
        return self.surplus_power + self.consumer_total_w()

    def meter_total_w(self) -> int:
        """Grid power the meter reports (+ = import): surplus + chargers + heaters + batteries + noise."""
        return int(self.surplus_power + self.consumer_total_w() + self.battery_total_w() + self.noise_w)

    def consumer_total_w(self) -> float:
        """Power of all simulated consumers (chargers + heaters) in W."""
        charger_total_w = sum(s.current_power * 1000 for s in self.stations)
        heater_total_w  = sum(h.power_w for h in self.heaters)
        return charger_total_w + heater_total_w

    def update_all(self):
        self._update_batteries()
        for station in self.stations:
            station.update()
        for heater in self.heaters:
            heater.update()
        for widget in self.heater_widgets:
            widget.update_display()
        for widget in self.findChildren(StationWidget):
            widget.update_display()

        self.meter.update()          # energy accumulation uses total_power_w from last cycle
        self._update_poll_display()  # apply polled values before recomputing totals
        self.update_surplus_display()  # set new total + phase values for this cycle
        self.meter_widget.update_display()
        self.community_widget.tick_1s()

    def _update_batteries(self):
        for battery in self.batteries:
            battery.update()
        for widget in self.findChildren(BatteryWidget):
            widget.update_display()
        amplitude = self.noise_spin.value()
        self.noise_w = random.uniform(-amplitude, amplitude) if self.noise_cb.isChecked() else 0.0
        self.noise_label.setText(f"{int(self.noise_w):+d} W")


def main():
    app    = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
