# Modbus Register Dokumentation

## Übersicht

Jede Ladestation wird über dieselbe IP/Port-Adresse erreicht, differenzert über die Modbus Unit-ID (Slave-ID).

## Verbindung

| Parameter | Wert |
|-----------|------|
| Host | `0.0.0.0` (alle Interfaces) |
| Port | `5020` (zentral für alle Stationen) |
| Unit ID | Station-ID (1, 2, 3, ...) |
| Protokoll | Modbus TCP |

### Beispiele

| Station | Port | Unit ID |
|---------|------|---------|
| Station 1 | 5020 | 1 |
| Station 2 | 5020 | 2 |
| Station 3 | 5020 | 3 |
| Station N | 5020 | N |

### Netzwerk

Der Server ist über alle Netzwerkinterfaces erreichbar, z.B.:
- `127.0.0.1:5020` (localhost)
- `192.168.100.136:5020` (LAN)

---

## Holding Register (Function Code 03/06/16)

### Register-Tabelle

| Adresse | Name | Typ | Einheit | Skala | Zugriff | Wertebereich |
|---------|------|-----|---------|-------|---------|--------------|
| 0 | Status | UINT16 | - | 1 | R | 0-6 |
| 1 | Current Power | UINT16 | kW | 0.1 | R | 0-6553.5 |
| 2 | Voltage | UINT16 | V | 0.1 | R | 0-6553.5 |
| 3 | Current | UINT16 | A | 0.1 | R | 0-6553.5 |
| 4-5 | Energy Delivered | UINT32 | kWh | 0.01 | R | 0-655.35 |
| 6 | Max Power | UINT16 | kW | 0.1 | R | 0-6553.5 |
| 7 | Error Code | UINT16 | - | 1 | R | 0-255 |
| 8-9 | Set Current | UINT32 | mA | 1 | R/W | 0-32000 |
| 10 | Phases | UINT16 | - | 1 | R/W | 1, 3 |

---

## Register-Beschreibungen

### Register 0: Status

| Wert | Bedeutung |
|------|-----------|
| 0 | Unplugged |
| 1 | Disconnected |
| 2 | Connected |
| 3 | Charging |
| 4 | Error |
| 5 | Unauthorized |
| 6 | Stop |

---

### Register 1-3: Power, Voltage, Current

| Register | Größe | Skala | Beispiel |
|----------|-------|-------|----------|
| 1: Power | UINT16 | ×0.1 kW | 125 = 12.5 kW |
| 2: Voltage | UINT16 | ×0.1 V | 2300 = 230.0 V |
| 3: Current | UINT16 | ×0.1 A | 160 = 16.0 A |

---

### Register 4-5: Energy Delivered (UINT32)

32-bit Wert über 2 Register (Little Endian):
- Register 4: Low Word
- Register 5: High Word

**Beispiel:** 12.50 kWh = 1250 → Register 4 = 1250, Register 5 = 0

---

### Register 8-9: Set Current (UINT32)

Ladestrom in **Milliampere (mA)** über 2 Register:
- Register 8: Low Word
- Register 9: High Word

| mA Wert | Register 8 | Register 9 | Bedeutung |
|---------|------------|------------|-----------|
| 0 | 0 | 0 | Kein Strom |
| 6000 | 6000 | 0 | 6.0 A |
| 16000 | 16000 | 0 | 16.0 A |
| 32000 | 32000 | 0 | 32.0 A |

---

### Register 10: Phases

| Wert | Bedeutung |
|------|-----------|
| 1 | 1-phasig |
| 3 | 3-phasig |

---

## Python-Client-Beispiele

### Status lesen

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('192.168.100.136', port=5020)
client.connect()

# Alle Register lesen (0-10)
result = client.read_holding_registers(address=0, count=11, device_id=1)

status = result.registers[0]
power_kw = result.registers[1] * 0.1
voltage = result.registers[2] * 0.1
current_a = result.registers[3] * 0.1
energy_kwh = (result.registers[4] | (result.registers[5] << 16)) * 0.01
phases = result.registers[10]

client.close()
```

### Ladestrom setzen (in mA)

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('192.168.100.136', port=5020)
client.connect()

# 16A = 16000 mA setzen
client.write_register(address=8, value=16000, device_id=1)

# 32A = 32000 mA setzen
client.write_register(address=8, value=32000, device_id=1)

# 0A = Strom stoppen
client.write_register(address=8, value=0, device_id=1)

client.close()
```

### Phasen umschalten

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('192.168.100.136', port=5020)
client.connect()

# Auf 1-phasig schalten
client.write_register(address=10, value=1, device_id=1)

# Auf 3-phasig schalten
client.write_register(address=10, value=3, device_id=1)

client.close()
```

### Komplettes Steuerungsbeispiel

```python
from pymodbus.client import ModbusTcpClient
import time

class ChargingStation:
    def __init__(self, host, station_id):
        self.client = ModbusTcpClient(host, port=5020)
        self.station_id = station_id
        
    def connect(self):
        self.client.connect()
        
    def disconnect(self):
        self.client.close()
        
    def read_status(self):
        result = self.client.read_holding_registers(0, count=11, device_id=self.station_id)
        return {
            'status': result.registers[0],
            'power_kw': result.registers[1] * 0.1,
            'voltage': result.registers[2] * 0.1,
            'current_a': result.registers[3] * 0.1,
            'energy_kwh': (result.registers[4] | (result.registers[5] << 16)) * 0.01,
            'max_power_kw': result.registers[6] * 0.1,
            'error_code': result.registers[7],
            'set_current_ma': result.registers[8] | (result.registers[9] << 16),
            'phases': result.registers[10]
        }
    
    def set_current(self, amps):
        """Set current in Amperes"""
        ma_value = int(amps * 1000)
        self.client.write_register(8, ma_value, device_id=self.station_id)
        
    def set_phases(self, phases):
        """Set phases (1 or 3)"""
        if phases in [1, 3]:
            self.client.write_register(10, phases, device_id=self.station_id)

# Usage
station = ChargingStation('192.168.100.136', 1)
station.connect()

# Status lesen
status = station.read_status()
print(f"Status: {status}")

# Auf 16A setzen
station.set_current(16.0)
time.sleep(0.5)

# Status erneut lesen
status = station.read_status()
print(f"New current: {status['set_current_ma']} mA")

station.disconnect()
```

---

## Modbus Function Codes

| FC | Name | Beschreibung |
|----|------|--------------|
| 03 | Read Holding Registers | Register lesen |
| 06 | Write Single Register | Einzelnes Register schreiben |
| 16 | Write Multiple Registers | Mehrere Register schreiben |

---

## Fehlerbehandlung

| Exception Code | Bedeutung |
|----------------|-----------|
| 01 | Illegal Function |
| 02 | Illegal Data Address |
| 03 | Illegal Data Value |
| 04 | Slave Device Failure |
