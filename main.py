"""
EV Charging Station Simulator
Multi-platform desktop application for simulating multiple Modbus-TCP charging stations
"""

import sys
import socket
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSpinBox, QGroupBox, QScrollArea,
    QGridLayout, QDoubleSpinBox, QComboBox, QCheckBox
)
from PyQt6.QtCore import QTimer, Qt
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
import threading


class ChargingStationSimulator:
    """Simulates a single EV charging station with Modbus TCP interface"""
    
    STATUS_UNPLUGGED = 0
    STATUS_DISCONNECTED = 1
    STATUS_CONNECTED = 2
    STATUS_CHARGING = 3
    STATUS_ERROR = 4
    STATUS_UNAUTHORIZED = 5
    STATUS_STOP = 6
    
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
        self.station_id = station_id
        self.is_charging = False
        self.current_power = 0.0
        self.max_power = 22.0
        self.current_amperage = 0.0
        self.target_amperage = 0.0
        self.max_amperage = 32.0
        self.voltage = 230.0
        self.phases = 3
        self.energy_delivered = 0.0
        self.session_duration = 0
        self.status_code = 0
        self.error_code = 0
        self.set_current_ma = 0
        self.manual_amperage = False
        self.registers = [0] * 20
        
    @property
    def status(self):
        return self.STATUS_NAMES.get(self.status_code, "Unknown")
        
    def get_modbus_data(self) -> list:
        """Returns all Modbus registers as a list"""
        self.registers[0] = self.status_code
        self.registers[1] = int(self.current_power * 10)
        self.registers[2] = int(self.voltage * 10)
        self.registers[3] = int(self.current_amperage * 10)
        self.registers[4] = int(self.energy_delivered * 100) & 0xFFFF
        self.registers[5] = (int(self.energy_delivered * 100) >> 16) & 0xFFFF
        self.registers[6] = int(self.max_power * 10)
        self.registers[7] = self.error_code
        self.registers[8] = self.set_current_ma & 0xFFFF
        self.registers[9] = (self.set_current_ma >> 16) & 0xFFFF
        self.registers[10] = self.phases
        return self.registers
    
    def write_register(self, address: int, value: int) -> bool:
        """Write to a Modbus register"""
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
                self.is_charging = True
                self.target_amperage = self.set_current_ma / 1000.0
            else:
                self.is_charging = False
                self.target_amperage = 0.0
                self.current_amperage = 0.0
                self.current_power = 0.0
        else:
            self.is_charging = False
            self.current_power = 0.0
            self.current_amperage = 0.0
            self.target_amperage = 0.0
            self.energy_delivered = 0.0
    
    def plug_in(self):
        self.set_status(self.STATUS_CONNECTED)
    
    def plug_out(self):
        self.set_status(self.STATUS_UNPLUGGED)
    
    def set_error(self):
        self.set_status(self.STATUS_ERROR)
        
    def set_amperage(self, amps: float):
        self.target_amperage = min(amps, self.max_amperage)
    
    def _auto_transition(self):
        if self.status_code not in (self.STATUS_CONNECTED, self.STATUS_CHARGING):
            return
        if self.set_current_ma > 0 and not self.is_charging:
            self.is_charging = True
            self.target_amperage = self.set_current_ma / 1000.0
            self.status_code = self.STATUS_CHARGING
        elif self.set_current_ma == 0 and self.is_charging:
            self.is_charging = False
            self.target_amperage = 0.0
            self.current_amperage = 0.0
            self.current_power = 0.0
            self.status_code = self.STATUS_CONNECTED
    
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
            self.current_power = self.current_amperage * self.voltage * self.phases / 1000
            self.energy_delivered += (self.current_power / 3600)
            if self.current_amperage == 0.0 and self.target_amperage == 0.0:
                self.is_charging = False
                self.current_power = 0.0
                self.status_code = self.STATUS_CONNECTED


class StationDataBlock(ModbusSequentialDataBlock):
    """Custom datablock that live-syncs with a ChargingStationSimulator.
    
    pymodbus 3.x calls getValues/setValues on the datablock object,
    so we override those to pull live data from the station on every read
    and delegate writes to the station's write_register method.
    """
    
    def __init__(self, station: ChargingStationSimulator):
        super().__init__(0, [0] * 100)
        self.station = station
        self.last_request_time = "Never"
        self.last_request_type = "None"
    
    def getValues(self, address, count=1):
        # pymodbus uses 1-based addressing internally (address+1 in ModbusDeviceContext)
        # so we convert back to 0-based
        addr = address - 1
        self.last_request_time = datetime.now().strftime("%H:%M:%S")
        self.last_request_type = f"Read HR {addr} (qty {count})"
        values = self.station.get_modbus_data()
        if addr < 0 or addr + count > len(values):
            return [0] * count
        result = values[addr:addr + count]
        print(f"[Modbus RX] Unit {self.station.station_id}: Read HR {addr} qty {count} -> {result}")
        return result
    
    def setValues(self, address, values):
        addr = address - 1
        self.last_request_time = datetime.now().strftime("%H:%M:%S")
        self.last_request_type = f"Write HR {addr} (qty {len(values)})"
        print(f"[Modbus TX] Unit {self.station.station_id}: Write HR {addr} qty {len(values)} <- {values}")
        for i, value in enumerate(values):
            self.station.write_register(addr + i, value)


class DynamicStationProxy:
    """Dict-like proxy that maps unit_id → ModbusDeviceContext dynamically.
    
    pymodbus ModbusServerContext(single=False) uses `devices[unit_id]` 
    to look up the datastore for each incoming request.
    By implementing __getitem__, __contains__, and keys(), this proxy
    always returns the current live stations from the MainWindow.
    """
    
    def __init__(self, main_window):
        self.main_window = main_window
        self._blocks = {}

    def _get_or_create_block(self, unit_id: int) -> StationDataBlock:
        station = None
        for s in self.main_window.stations:
            if s.station_id == unit_id:
                station = s
                break
        if station is None:
            return None
        if unit_id not in self._blocks or self._blocks[unit_id].station is not station:
            self._blocks[unit_id] = StationDataBlock(station)
        return self._blocks[unit_id]
    
    def __getitem__(self, unit_id: int):
        block = self._get_or_create_block(unit_id)
        if block is None:
            empty = ModbusSequentialDataBlock(0, [0] * 100)
            return ModbusDeviceContext(hr=empty)
        return ModbusDeviceContext(hr=block)
    
    def __contains__(self, unit_id: int) -> bool:
        return any(s.station_id == unit_id for s in self.main_window.stations)
    
    def keys(self):
        return [s.station_id for s in self.main_window.stations]


class ModbusServerThread(threading.Thread):
    """Thread for running a single Modbus TCP server for all stations"""
    
    def __init__(self, main_window):
        super().__init__(daemon=True)
        self.main_window = main_window
        self.running = False
        self.proxy = DynamicStationProxy(main_window)
        
    def run(self):
        self.running = True
        context = ModbusServerContext(devices=self.proxy, single=False)
        try:
            StartTcpServer(context=context, address=("0.0.0.0", 5020))
        except Exception as e:
            print(f"Modbus server error: {e}")
        finally:
            self.running = False

    def get_datastore(self, station_id: int):
        return self.proxy._get_or_create_block(station_id)


class StationWidget(QGroupBox):
    """UI Widget for a single charging station"""
    
    def __init__(self, station: ChargingStationSimulator, parent=None):
        super().__init__(f"Station {station.station_id}", parent)
        self.station = station
        self.window = None # Will be set by MainWindow
        self.setup_ui()
        
    def setup_ui(self):
        layout = QGridLayout()
        
        # --- Status Section ---
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
        
        # --- Read-only Values (Monitoring) ---
        layout.addWidget(QLabel("Power:"), 2, 0)
        self.power_label = QLabel("0.0 kW")
        layout.addWidget(self.power_label, 2, 1)
        
        layout.addWidget(QLabel("Voltage:"), 3, 0)
        self.voltage_label = QLabel("230.0 V")
        layout.addWidget(self.voltage_label, 3, 1)
        
        layout.addWidget(QLabel("Current:"), 4, 0)
        self.current_label = QLabel("0.0 A")
        layout.addWidget(self.current_label, 4, 1)
        
        layout.addWidget(QLabel("Energy:"), 5, 0)
        self.energy_label = QLabel("0.00 kWh")
        layout.addWidget(self.energy_label, 5, 1)
        
        layout.addWidget(QLabel("Set Current:"), 6, 0)
        self.set_current_label = QLabel("0 mA")
        layout.addWidget(self.set_current_label, 6, 1)
        
        layout.addWidget(QLabel("Phases:"), 7, 0)
        self.phases_label = QLabel("3")
        layout.addWidget(self.phases_label, 7, 1)
        
        # --- Controls (Settings) ---
        layout.addWidget(QLabel("Max Power:"), 8, 0)
        self.max_power_spin = QDoubleSpinBox()
        self.max_power_spin.setRange(1.0, 22.0)
        self.max_power_spin.setValue(self.station.max_power)
        self.max_power_spin.setSuffix(" kW")
        self.max_power_spin.valueChanged.connect(self.update_max_power)
        layout.addWidget(self.max_power_spin, 8, 1)
        
        layout.addWidget(QLabel("Amperage:"), 9, 0)
        self.amperage_spin = QDoubleSpinBox()
        self.amperage_spin.setRange(0.0, 32.0)
        self.amperage_spin.setValue(0.0)
        self.amperage_spin.setSuffix(" A")
        self.amperage_spin.valueChanged.connect(self.update_amperage)
        layout.addWidget(self.amperage_spin, 9, 1)
        
        layout.addWidget(QLabel("Manual:"), 10, 0)
        self.manual_checkbox = QCheckBox("Manual Override")
        self.manual_checkbox.setChecked(False)
        self.manual_checkbox.stateChanged.connect(self.toggle_manual)
        layout.addWidget(self.manual_checkbox, 10, 1)
        
        layout.addWidget(QLabel("Last Request:"), 11, 0)
        self.last_req_label = QLabel("None")
        self.last_req_label.setStyleSheet("font-size: 10px; color: gray;")
        layout.addWidget(self.last_req_label, 11, 1)
        
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
        self.set_current_label.setText(f"{self.station.set_current_ma} mA ({self.station.set_current_ma/1000:.1f} A)")
        self.phases_label.setText(f"{self.station.phases}")
        
        if not self.station.manual_amperage:
            self.amperage_spin.blockSignals(True)
            self.amperage_spin.setValue(self.station.current_amperage)
            self.amperage_spin.blockSignals(False)
            
        # Update last modbus request display
        if self.window.modbus_server_thread and self.window.modbus_server_thread.running:
            datastore = self.window.modbus_server_thread.get_datastore(self.station.station_id)
            if datastore:
                info = f"[{datastore.last_request_time}] {datastore.last_request_type}"
                self.last_req_label.setText(info)
        
    def on_plug_in(self):
        self.station.plug_in()
        self.update_display()
        
    def on_plug_out(self):
        self.station.plug_out()
        self.update_display()
        
    def on_error(self):
        self.station.set_error()
        self.update_display()
        
    def update_max_power(self, value):
        self.station.max_power = value
        
    def update_amperage(self, value):
        self.station.set_amperage(value)
        self.station.manual_amperage = True
        self.manual_checkbox.setChecked(True)
        
    def toggle_manual(self, state):
        self.station.manual_amperage = (state == Qt.CheckState.Checked)
        if not self.station.manual_amperage:
            self.station.set_amperage(self.station.set_current_ma / 1000.0)


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.stations = []
        self.modbus_server_thread = None
        self.setup_ui()
        self.start_modbus_server()
        self.start_update_timer()
        self.add_stations()
        
    def setup_ui(self):
        self.setWindowTitle("EV Charging Station Simulator")
        self.setGeometry(100, 100, 1200, 800)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        control_layout = QHBoxLayout()
        
        control_layout.addWidget(QLabel("Number of Stations:"))
        self.station_count_spin = QSpinBox()
        self.station_count_spin.setRange(1, 16)
        self.station_count_spin.setValue(3)
        control_layout.addWidget(self.station_count_spin)
        
        add_btn = QPushButton("Add Stations")
        add_btn.clicked.connect(self.add_stations)
        control_layout.addWidget(add_btn)
        
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_stations)
        control_layout.addWidget(clear_btn)
        
        control_layout.addStretch()

        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
        if not ips:
            ips = ["127.0.0.1"]
        host_ip = "  |  ".join(ips)
        server_label = QLabel(f"Modbus TCP:  <b>{host_ip}:5020</b>")
        server_label.setStyleSheet("color: #444; font-family: monospace; padding: 0 8px;")
        control_layout.addWidget(server_label)

        copy_btn = QPushButton("Copy IP")
        copy_btn.setFixedWidth(70)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(ips)))
        control_layout.addWidget(copy_btn)

        main_layout.addLayout(control_layout)
        
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.stations_container = QWidget()
        self.stations_layout = QGridLayout(self.stations_container)
        self.scroll.setWidget(self.stations_container)
        main_layout.addWidget(self.scroll)
        
    def add_stations(self):
        count = self.station_count_spin.value()
        start_id = len(self.stations) + 1
        
        for i in range(count):
            station_id = start_id + i
            station = ChargingStationSimulator(station_id)
            self.stations.append(station)
            
            row = len(self.stations) // 4
            col = (len(self.stations) - 1) % 4
            widget = StationWidget(station)
            widget.window = self
            self.stations_layout.addWidget(widget, row, col)
            
    def start_modbus_server(self):
        if self.modbus_server_thread and self.modbus_server_thread.running:
            return
        self.modbus_server_thread = ModbusServerThread(self)
        self.modbus_server_thread.start()
        
    def clear_stations(self):
        self.stations.clear()
        
        while self.stations_layout.count():
            item = self.stations_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
    def start_update_timer(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_stations)
        self.timer.start(1000)
        
    def update_stations(self):
        for station in self.stations:
            station.update()
            
        for widget in self.findChildren(StationWidget):
            widget.update_display()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
