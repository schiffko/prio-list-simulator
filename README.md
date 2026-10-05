# EV Charging Station Simulator

Desktop-Applikation zur Simulation mehrerer Modbus-TCP-Ladestationen, eines Smartfox-Slave-Energiezählers und einer steuerbaren Victron-Batterie.

## Voraussetzungen

- Python 3.8 oder neuer
- pip

## Installation

```bash
cd charging_simulator
python -m venv venv
```

**macOS / Linux:**
```bash
source venv/bin/activate
```

**Windows:**
```bash
venv\Scripts\activate
```

Dann Dependencies installieren:
```bash
pip install -r requirements.txt
```

## Starten

```bash
python main.py
```

Die Applikation öffnet sich und startet automatisch einen Modbus-TCP-Server auf Port **5020**.
Die aktive Host-IP wird oben rechts im Fenster angezeigt (Button "Copy IP" kopiert sie in die Zwischenablage).

## Modbus-Verbindung

| Parameter | Wert |
|-----------|------|
| Host | IP aus der Titelleiste |
| Port | 5020 |
| Unit ID | Station-ID (1, 2, 3, …), Victron-Batterie `242`, Überschuss `100` |

Alle Stationen laufen auf demselben Port und werden über die Unit ID unterschieden.

## Batterie-Simulation

Zusätzlich simuliert die App eine steuerbare Victron-VE-Batterie auf derselben IP / demselben Port:

| Parameter | Wert |
|-----------|------|
| Host | IP aus der Titelleiste |
| Port | 5020 |
| Unit ID | 242 |
| Typ-Empfehlung | `BS_ID_VICTRON_VE_CTRL` |

Abgebildeter Registersatz aus `bs_victron.c`:

| Register | Beschreibung | Einheit | Skalierung |
|----------|-------------|---------|------------|
| 843 | SoC | % | 1 |
| 26-27 | Batterie-Leistung | W | simuliert als `reg26 * reg27 / 1000` |
| 61 | Temperatur | °C | ÷ 10 |
| 37 | Setpoint L1 | W | 1 |
| 40 | Setpoint L2 | W | 1 |
| 41 | Setpoint L3 | W | 1 |

Empfohlene Smartfox-Konfiguration für den Test:

- Produkt: `BS_ID_VICTRON_VE_CTRL`
- Port testweise auf `5020`
- `threePhases = 0` für den einfachsten Start

Schreibverhalten:

- Der Simulator akzeptiert `FC16` auf `37`, `40`, `41`
- Der zuletzt geschriebene Setpoint wird als Ziel-Leistung übernommen
- Die gemessene Leistung fährt mit einer kleinen Rampe auf den Zielwert

## Heizstab-Simulation (Debug-Heizstab)

Simuliert bis zu drei Heizstäbe für das Smartfox-Produkt `VB_ID_DEBUG` (Treiber `ht_debug.c`, im Web-UI admin-only als Verbrauchsregler "DEBUG").

| Parameter | Wert |
|-----------|------|
| Host | IP aus der Titelleiste (alle Debug-Heizstäbe müssen dieselbe IP haben) |
| Port | 5020 |
| Unit ID | 210 + Heizstab-Platz (210, 211, 212) |

| Register | Beschreibung | Einheit | Zugriff |
|----------|-------------|---------|---------|
| 0 | Zustand: 0 = bereit, 1 = heizt, 2 = Max. Temperatur | – | R |
| 1-2 | Gemessene Leistung (High/Low Word) | W | R |
| 3-4 | Energie gesamt (High/Low Word) | Wh | R |
| 5 | Kesseltemperatur | 0,1 °C | R |
| 8-9 | Sollleistung (High/Low Word) | W | R/W (FC16 vom Smartfox) |

Verhalten:
- Die Leistung folgt der Sollleistung mit 500 W/s, begrenzt auf die Nennleistung.
- Optional wird die Kesseltemperatur aus der Leistung simuliert. Ab "Max Temp" meldet der Heizstab Zustand 2 und schaltet ab, unter "Max Temp − 5 °C" wieder ein. "Force max. temperature" erzwingt Zustand 2.
- Die Heizstab-Leistung geht gleichmäßig auf alle 3 Phasen in den simulierten Energiezähler und in Unit-ID 100 ein.

## Modbus-Register (Holding Registers, 0-basiert)

| Register | Beschreibung | Einheit | Skalierung |
|----------|-------------|---------|------------|
| 0 | Status | – | – |
| 1 | Aktuelle Leistung | kW | ÷ 10 |
| 2 | Spannung | V | ÷ 10 |
| 3 | Stromstärke | A | ÷ 10 |
| 4 | Gelieferte Energie (Low Word) | kWh | ÷ 100 |
| 5 | Gelieferte Energie (High Word) | kWh | ÷ 100 |
| 6 | Max. Leistung | kW | ÷ 10 |
| 7 | Fehlercode | – | – |
| 8 | Sollstrom (Low Word) | mA | – |
| 9 | Sollstrom (High Word) | mA | – |
| 10 | Phasen (1 oder 3) | – | – |

**Statuswerte (Register 0):**

| Wert | Bedeutung |
|------|-----------|
| 0 | Unplugged |
| 1 | Disconnected |
| 2 | Connected |
| 3 | Charging |
| 4 | Error |
| 5 | Unauthorized |
| 6 | Stop |

## Bedienung

- **Einstecken / Ausstecken / Fehler** – Status der Station manuell setzen
- **Max Power** – maximale Ladeleistung der Station konfigurieren
- **Amperage + Manual Override** – Stromstärke manuell vorgeben (sonst wird sie per Modbus-Schreiben auf Register 8/9 gesteuert)
- **Stationen hinzufügen / leeren** – Anzahl der simulierten Stationen anpassen
- **Battery Tab** – gemessene Leistung, SoC und Temperatur direkt vorgeben; geschriebene Victron-Setpoints werden angezeigt

## Abhängigkeiten

| Paket | Version |
|-------|---------|
| PyQt6 | ≥ 6.4.0 |
| pymodbus | ≥ 3.6.9 |
