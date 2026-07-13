"""
EV Charging Station + Smartfox Slave Energy Meter + Victron Battery Simulator

Charger simulation  (port 5020, unit-IDs = station IDs):
  Implements the register map expected by car_cc_debug.c

Energy meter simulation  (port 502, unit-ID 1):
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
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSpinBox, QGroupBox, QScrollArea,
    QGridLayout, QDoubleSpinBox, QComboBox, QCheckBox, QTabWidget
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


class ModbusTcpBatterySimulator:
    """Simple Victron VE battery model for bs_victron.c"""

    REG_SOC = 843
    REG_POWER = 26
    REG_TEMP = 61
    REG_SETPOINT_L1 = 37
    REG_SETPOINT_L2 = 40
    REG_SETPOINT_L3 = 41

    def __init__(self, unit_id: int = 242):
        self.unit_id = unit_id
        self.power_w = 0
        self.soc_percent = 50
        self.temperature_c = 23.5
        self.setpoint_l1_w = 0
        self.setpoint_l2_w = 0
        self.setpoint_l3_w = 0
        self.target_power_w = 0
        self.power_source_scale = 1000
        self.last_request_time = "Never"
        self.last_request_type = "None"

    @staticmethod
    def _to_u16(value: int) -> int:
        return value & 0xFFFF

    def read_registers(self, start: int, count: int) -> list:
        regs = {}
        regs[self.REG_SOC] = self._to_u16(int(self.soc_percent))
        regs[self.REG_POWER] = self._to_u16(self.power_source_scale)
        regs[self.REG_POWER + 1] = self._to_u16(int(self.power_w))
        regs[self.REG_TEMP] = self._to_u16(int(round(self.temperature_c * 10.0)))
        regs[self.REG_SETPOINT_L1] = self._to_u16(self.setpoint_l1_w)
        regs[self.REG_SETPOINT_L2] = self._to_u16(self.setpoint_l2_w)
        regs[self.REG_SETPOINT_L3] = self._to_u16(self.setpoint_l3_w)
        return [regs.get(start + i, 0) for i in range(count)]

    def write_register(self, address: int, value: int) -> bool:
        signed = value if value < 0x8000 else value - 0x10000

        if address == self.REG_SOC:
            self.soc_percent = max(0, min(100, signed))
            return True
        if address == self.REG_TEMP:
            self.temperature_c = signed / 10.0
            return True
        if address == self.REG_SETPOINT_L1:
            self.setpoint_l1_w = signed
            self.target_power_w = signed
            return True
        if address == self.REG_SETPOINT_L2:
            self.setpoint_l2_w = signed
            self.target_power_w = signed
            return True
        if address == self.REG_SETPOINT_L3:
            self.setpoint_l3_w = signed
            self.target_power_w = signed
            return True
        return False

    def update(self):
        diff = self.target_power_w - self.power_w
        if diff == 0:
            return
        step = 250 if diff > 0 else -250
        if abs(diff) <= abs(step):
            self.power_w = self.target_power_w
        else:
            self.power_w += step


# ---------------------------------------------------------------------------
# Combined Modbus TCP server (port 5020)
# Dispatches by register address range:
#   start >= 40000  ->  energy meter (em_smartfox_slave.c register map)
#   start <  40000  ->  charger      (car_cc_debug.c register map)
# ---------------------------------------------------------------------------

METER_REG_THRESHOLD = 40000
BATTERY_UNIT_ID = 242
BATTERY_SYSTEM_UNIT_ID = 0


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

    @property
    def battery(self) -> ModbusTcpBatterySimulator:
        return self.main_window.battery

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info('peername')
        print(f"[Modbus] Connected: {peer}")
        try:
            while True:
                header   = await reader.readexactly(7)
                trans_id = struct.unpack_from('>H', header, 0)[0]
                length   = struct.unpack_from('>H', header, 4)[0]
                unit_id  = header[6]
                pdu          = await reader.readexactly(length - 1)
                func_code    = pdu[0]
                response_pdu = self._dispatch(unit_id, func_code, pdu[1:])
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

    def _dispatch(self, unit_id, func_code, data):
        try:
            start = struct.unpack_from('>H', data, 0)[0]

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

    # ---- meter reads ----

    def _read_meter(self, start, count):
        now = datetime.now().strftime("%H:%M:%S")
        self.meter.last_request_time = now
        self.meter.last_request_type = f"FC3 addr={start} qty={count}"
        result = self.meter.read_registers(start, count)
        print(f"[Meter RX] Read HR {start} qty {count} -> {result}")
        return result

    # ---- charger reads / writes ----

    def _get_station(self, unit_id):
        for s in self.main_window.stations:
            if s.station_id == unit_id:
                return s
        return None

    def _read_charger(self, unit_id, start, count):
        now = datetime.now().strftime("%H:%M:%S")
        if unit_id in (BATTERY_UNIT_ID, BATTERY_SYSTEM_UNIT_ID):
            self.battery.last_request_time = now
            self.battery.last_request_type = f"Read HR {start} (qty {count}, unit {unit_id})"
            result = self.battery.read_registers(start, count)
            print(f"[Battery RX] Unit {unit_id}: Read HR {start} qty {count} -> {result}")
            return result
        if unit_id == 100:
            charger_total_w = sum(s.current_power * 1000 for s in self.main_window.stations)
            result_w = self.main_window.surplus_power + charger_total_w
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
        if unit_id == BATTERY_UNIT_ID:
            self.battery.last_request_time = now
            self.battery.last_request_type = f"Write HR {start} (qty {len(values)})"
            print(f"[Battery TX] Unit {unit_id}: Write HR {start} qty {len(values)} <- {values}")
            for i, value in enumerate(values):
                self.battery.write_register(start + i, value)
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
        self._server = await asyncio.start_server(self._handle_client, '0.0.0.0', 5020)
        print("[Modbus] Combined server listening on 0.0.0.0:5020")
        async with self._server:
            await self._server.serve_forever()


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

class StationWidget(QGroupBox):
    def __init__(self, station: ChargingStationSimulator, parent=None):
        super().__init__(f"Station {station.station_id}", parent)
        self.station = station
        self.window  = None
        self.setup_ui()

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
    def __init__(self, meter: SmartfoxSlaveSimulator, parent=None):
        super().__init__("Smartfox Slave Meter  (port 5020, Unit-ID 1)", parent)
        self.meter = meter
        self._setup_ui()

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
    def __init__(self, battery: ModbusTcpBatterySimulator, parent=None):
        super().__init__(f"Victron Battery (port 5020, Unit-ID {battery.unit_id})", parent)
        self.battery = battery
        self._setup_ui()

    def _setup_ui(self):
        layout = QGridLayout()

        layout.addWidget(QLabel("Measured Power:"), 0, 0)
        self.power_spin = QSpinBox()
        self.power_spin.setRange(-50000, 50000)
        self.power_spin.setValue(self.battery.power_w)
        self.power_spin.setSuffix(" W")
        self.power_spin.valueChanged.connect(self._on_power_changed)
        layout.addWidget(self.power_spin, 0, 1)

        layout.addWidget(QLabel("SoC:"), 1, 0)
        self.soc_spin = QSpinBox()
        self.soc_spin.setRange(0, 100)
        self.soc_spin.setValue(self.battery.soc_percent)
        self.soc_spin.setSuffix(" %")
        self.soc_spin.valueChanged.connect(self._on_soc_changed)
        layout.addWidget(self.soc_spin, 1, 1)

        layout.addWidget(QLabel("Temperature:"), 2, 0)
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(-40.0, 100.0)
        self.temp_spin.setDecimals(1)
        self.temp_spin.setValue(self.battery.temperature_c)
        self.temp_spin.setSuffix(" C")
        self.temp_spin.valueChanged.connect(self._on_temp_changed)
        layout.addWidget(self.temp_spin, 2, 1)

        layout.addWidget(QLabel("Target Power:"), 3, 0)
        self.target_label = QLabel("0 W")
        self.target_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.target_label, 3, 1)

        layout.addWidget(QLabel("Setpoints:"), 4, 0)
        self.setpoint_label = QLabel("-")
        self.setpoint_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.setpoint_label, 4, 1)

        layout.addWidget(QLabel("Registers:"), 5, 0)
        self.reg_label = QLabel("-")
        self.reg_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.reg_label, 5, 1)

        layout.addWidget(QLabel("Last Request:"), 6, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(self.last_req_label, 6, 1)

        self.setLayout(layout)

    def _on_power_changed(self, value: int):
        self.battery.power_w = value

    def _on_soc_changed(self, value: int):
        self.battery.soc_percent = value

    def _on_temp_changed(self, value: float):
        self.battery.temperature_c = value

    def update_display(self):
        self.power_spin.blockSignals(True)
        self.power_spin.setValue(self.battery.power_w)
        self.power_spin.blockSignals(False)

        self.soc_spin.blockSignals(True)
        self.soc_spin.setValue(self.battery.soc_percent)
        self.soc_spin.blockSignals(False)

        self.temp_spin.blockSignals(True)
        self.temp_spin.setValue(self.battery.temperature_c)
        self.temp_spin.blockSignals(False)

        reg_soc = self.battery.read_registers(self.battery.REG_SOC, 1)[0]
        reg_power = self.battery.read_registers(self.battery.REG_POWER, 2)
        reg_temp = self.battery.read_registers(self.battery.REG_TEMP, 1)[0]
        self.target_label.setText(f"{self.battery.target_power_w} W")
        self.setpoint_label.setText(
            f"L1={self.battery.setpoint_l1_w} L2={self.battery.setpoint_l2_w} L3={self.battery.setpoint_l3_w}"
        )
        self.reg_label.setText(
            f"843={reg_soc}  26={reg_power[0]} 27={reg_power[1]}  61={reg_temp}"
        )
        self.last_req_label.setText(
            f"[{self.battery.last_request_time}] {self.battery.last_request_type}"
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.stations      = []
        self.server_thread = None
        self.surplus_power = 0.0
        self.meter         = SmartfoxSlaveSimulator()
        self.battery       = ModbusTcpBatterySimulator()
        self.setup_ui()
        self.start_servers()
        self.start_update_timer()
        self.add_stations()

    def setup_ui(self):
        self.setWindowTitle("EV Charging Station + Smartfox Slave + Victron Battery Simulator")
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
        info_layout.addWidget(QLabel(":5020   (chargers: unit-ID = station ID  |  Victron battery: unit-ID 242  |  meter: addr ≥ 40000)"))
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedWidth(50)
        copy_btn.clicked.connect(self._copy_ip)
        info_layout.addWidget(copy_btn)
        info_layout.addStretch()
        main_layout.addLayout(info_layout)

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
        self.meter_widget = MeterWidget(self.meter)
        meter_layout.addWidget(self.meter_widget)
        meter_layout.addStretch()
        tabs.addTab(meter_tab, "Energy Meter (addr ≥ 40000)")

        # Battery tab
        battery_tab = QWidget()
        battery_layout = QVBoxLayout(battery_tab)
        self.battery_widget = BatteryWidget(self.battery)
        battery_layout.addWidget(self.battery_widget)
        battery_layout.addStretch()
        tabs.addTab(battery_tab, "Victron Battery (Unit-ID 242)")

        main_layout.addWidget(tabs)

    @staticmethod
    def _get_local_ips():
        """Return all non-loopback IPv4 addresses from all network interfaces."""
        import netifaces
        ips = []
        try:
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
            widget = StationWidget(station)
            widget.window = self
            self.stations_layout.addWidget(widget, row, col)

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

    def _on_surplus_changed(self, value: float):
        self.surplus_power = value
        self.update_surplus_display()

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

        battery_phase_indices = []
        if self.battery.setpoint_l1_w != 0:
            battery_phase_indices.append(0)
        if self.battery.setpoint_l2_w != 0:
            battery_phase_indices.append(1)
        if self.battery.setpoint_l3_w != 0:
            battery_phase_indices.append(2)
        if not battery_phase_indices:
            battery_phase_indices = [0]

        battery_power_per_phase = int(self.battery.power_w / len(battery_phase_indices))
        battery_current_ma = int((battery_power_per_phase / 230.0) * 1000.0)
        for i in battery_phase_indices:
            phase_powers[i] += battery_power_per_phase
            phase_currents[i] += battery_current_ma

        self.meter.phase_powers_w    = phase_powers
        self.meter.phase_currents_ma = phase_currents

    def update_surplus_display(self):
        self.surplus_display.setText(f"{int(self.surplus_power):+d} W")
        self.surplus_spin.blockSignals(True)
        self.surplus_spin.setValue(self.surplus_power)
        self.surplus_spin.blockSignals(False)
        charger_total_w = sum(s.current_power * 1000 for s in self.stations)
        battery_total_w = self.battery.power_w
        result_w        = int(self.surplus_power + charger_total_w + battery_total_w)
        self.surplus_result_label.setText(f"{result_w:+d} W")
        self.meter.total_power_w = result_w
        self._compute_phase_values()

    def update_all(self):
        for station in self.stations:
            station.update()
        self.battery.update()
        for widget in self.findChildren(StationWidget):
            widget.update_display()

        self.meter.update()          # energy accumulation uses total_power_w from last cycle
        self.update_surplus_display()  # set new total + phase values for this cycle
        self.meter_widget.update_display()
        self.battery_widget.update_display()


def main():
    app    = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
