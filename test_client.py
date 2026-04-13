#!/usr/bin/env python3
"""
Test Client für EV Charging Station Simulator

Demonstriert die Modbus-Kommunikation mit den simulierten Ladestationen.
"""

from pymodbus.client import ModbusTcpClient
import time


class ChargingStationClient:
    """Client für eine Ladestation"""
    
    STATUS_NAMES = {
        0: "Unplugged",
        1: "Disconnected",
        2: "Connected",
        3: "Charging",
        4: "Error",
        5: "Unauthorized",
        6: "Stop"
    }
    
    def __init__(self, host: str, station_id: int):
        self.host = host
        self.station_id = station_id
        self.port = 5020
        self.client = ModbusTcpClient(host, port=self.port)
        
    def connect(self):
        """Verbindung herstellen"""
        print(f"Verbinde zu Station {self.station_id} @ {self.host}:{self.port} (Unit {self.station_id})")
        self.client.connect()
        
    def disconnect(self):
        """Verbindung trennen"""
        self.client.close()
        print("Verbindung getrennt")
        
    def read_all_registers(self) -> dict:
        """Alle Register lesen"""
        result = self.client.read_holding_registers(0, count=11, device_id=self.station_id)
        
        return {
            'status': result.registers[0],
            'status_name': self.STATUS_NAMES.get(result.registers[0], 'Unknown'),
            'power_kw': result.registers[1] * 0.1,
            'voltage': result.registers[2] * 0.1,
            'current_a': result.registers[3] * 0.1,
            'energy_kwh': (result.registers[4] | (result.registers[5] << 16)) * 0.01,
            'max_power_kw': result.registers[6] * 0.1,
            'error_code': result.registers[7],
            'set_current_ma': result.registers[8] | (result.registers[9] << 16),
            'phases': result.registers[10]
        }
    
    def read_register(self, address: int, count: int = 1) -> list:
        """Einzelne Register lesen"""
        result = self.client.read_holding_registers(address, count=count, device_id=self.station_id)
        return result.registers
    
    def write_current_ma(self, ma_value: int):
        """Ladestrom in mA setzen (Register 8-9)"""
        self.client.write_register(8, ma_value & 0xFFFF, device_id=self.station_id)
        print(f"Set Current: {ma_value} mA ({ma_value/1000:.1f} A)")
        
    def set_current_a(self, amps: float):
        """Ladestrom in Ampere setzen"""
        ma_value = int(amps * 1000)
        self.write_current_ma(ma_value)
        
    def set_phases(self, phases: int):
        """Phasen setzen (1 oder 3)"""
        if phases in [1, 3]:
            self.client.write_register(10, phases, device_id=self.station_id)
            print(f"Set Phases: {phases}")
        else:
            print("Invalid phases value (must be 1 or 3)")
            
    def print_status(self):
        """Status ausgeben"""
        status = self.read_all_registers()
        print("\n" + "="*50)
        print(f"Station {self.station_id} Status")
        print("="*50)
        print(f"Status:        {status['status_name']} ({status['status']})")
        print(f"Power:         {status['power_kw']:.1f} kW")
        print(f"Voltage:       {status['voltage']:.1f} V")
        print(f"Current:       {status['current_a']:.1f} A")
        print(f"Energy:        {status['energy_kwh']:.2f} kWh")
        print(f"Max Power:     {status['max_power_kw']:.1f} kW")
        print(f"Error Code:    {status['error_code']}")
        print(f"Set Current:   {status['set_current_ma']} mA ({status['set_current_ma']/1000:.1f} A)")
        print(f"Phases:        {status['phases']}")
        print("="*50 + "\n")


def test_single_station():
    """Test einer einzelnen Station"""
    print("\n" + "="*60)
    print("TEST: Einzelne Station")
    print("="*60)
    
    station = ChargingStationClient('127.0.0.1', 1)
    
    try:
        station.connect()
        time.sleep(0.5)
        
        # Status lesen
        print("\n1. Status lesen:")
        station.print_status()
        
        # Strom setzen
        print("2. Strom auf 16A setzen:")
        station.set_current_a(16.0)
        time.sleep(0.5)
        station.print_status()
        
        # Strom erhöhen
        print("3. Strom auf 32A setzen:")
        station.set_current_a(32.0)
        time.sleep(0.5)
        station.print_status()
        
        # Strom stoppen
        print("4. Strom auf 0A setzen:")
        station.set_current_a(0.0)
        time.sleep(0.5)
        station.print_status()
        
        # Phasen testen
        print("5. Phasen auf 1 setzen:")
        station.set_phases(1)
        time.sleep(0.5)
        station.print_status()
        
        print("6. Phasen auf 3 setzen:")
        station.set_phases(3)
        time.sleep(0.5)
        station.print_status()
        
    finally:
        station.disconnect()


def test_multiple_stations():
    """Test mehrerer Stationen"""
    print("\n" + "="*60)
    print("TEST: Mehrere Stationen")
    print("="*60)
    
    stations = []
    for i in range(1, 4):
        station = ChargingStationClient('127.0.0.1', i)
        station.connect()
        stations.append(station)
        time.sleep(0.3)
    
    try:
        # Alle Stationen lesen
        print("\nAlle Stationen lesen:")
        for station in stations:
            station.print_status()
        
        # Alle auf unterschiedliche Ströme setzen
        print("Ströme setzen:")
        stations[0].set_current_a(10.0)
        stations[1].set_current_a(20.0)
        stations[2].set_current_a(32.0)
        
        time.sleep(0.5)
        
        # Erneut lesen
        print("\nNeue Werte:")
        for station in stations:
            status = station.read_all_registers()
            print(f"Station {station.station_id}: {status['set_current_ma']} mA")
        
    finally:
        for station in stations:
            station.disconnect()


if __name__ == "__main__":
    import sys
    
    print("EV Charging Station Test Client")
    print("="*60)
    
    if len(sys.argv) > 1 and sys.argv[1] == "multi":
        test_multiple_stations()
    else:
        test_single_station()
    
    print("Tests abgeschlossen!")
