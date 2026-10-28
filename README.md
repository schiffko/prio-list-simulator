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

Die Applikation öffnet sich und startet automatisch einen Modbus-TCP-Server auf Port **5020** und **502** (gleicher Server, beide Ports liefern dieselben Daten).
Port 502 ist für Treiber mit fest eingestelltem Port (`em_smartfox_slave.c`, `bs_victron.c`) – eine Portweiterleitung 502 → 5020 ist damit nicht mehr nötig.
Unter macOS und Windows klappt das ohne Admin-Rechte; unter Linux braucht Port 502 root bzw. `CAP_NET_BIND_SERVICE`, sonst läuft nur 5020 (Hinweis in der Konsole).
Die aktive Host-IP wird oben rechts im Fenster angezeigt (Button "Copy IP" kopiert sie in die Zwischenablage).

## Modbus-Verbindung

| Parameter | Wert |
|-----------|------|
| Host | IP aus der Titelleiste |
| Port | 5020 |
| Unit ID | Station-ID (1, 2, 3, …), Victron-Batterie `0` / `242` / `246` / `227`, Heizstab `210`-`212`, Überschuss `100` |

Alle Stationen laufen auf demselben Port und werden über die Unit ID unterschieden.

## Batterie-Simulation

Zusätzlich simuliert die App steuerbare Victron-Batterien (Tab "Victron Batteries", beliebig viele) auf derselben IP / denselben Ports.
Mehrere Batterien werden über die lokale IP unterschieden (IP-Alias pro Batterie auf diesem PC, Feld "Local IP").

| Parameter | Wert |
|-----------|------|
| Host | IP aus der Titelleiste bzw. lokale IP der Batterie |
| Port | 502 oder 5020 |
| Unit ID | `0` (System: SoC 843, Cerbo-Leistung 842), `242` / `246` / `227` (VE / CC / Cerbo) |
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
- Port: Treiber nutzt fest `502`, der Simulator lauscht dort direkt
- `threePhases = 0` für den einfachsten Start

Schreibverhalten:

- Der Simulator akzeptiert `FC16` auf `37`, `40`, `41`
- Der geschriebene Setpoint wird als Ziel-Leistung übernommen (bei 3-phasig ×3)
- Die gemessene Leistung folgt nach einstellbarer Verzögerung mit einstellbarer Rampe, begrenzt durch max. Lade-/Entladeleistung; der SoC wird über die Kapazität mitgerechnet
- Optionales Lastrauschen (Tab "Victron Batteries") wird auf die Netzleistung addiert

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

## Steuer-API (für Skripte / Claude)

Der Simulator lässt sich lokal über HTTP bedienen: `http://127.0.0.1:8020` (nur localhost). Jeder Aufruf macht dasselbe wie der entsprechende Klick.

Neu: REST-API unter `/api/...` (Stationen, Heizstäbe, Batterien, Netz, Zähler, Polling, Energiegemeinschaft, Smartfox), siehe [API.md](API.md). Sie wird vom MCP-Server benutzt.
Die folgenden älteren Aufrufe funktionieren weiter und geben den gesamten Zustand zurück (`POST /battery` wirkt auf die erste Batterie, `power_w` fährt danach wieder auf den Firmware-Setpoint):

| Aufruf | Body (alle Felder optional, außer markiert) |
|--------|---------------------------------------------|
| `GET /state` | – |
| `POST /surplus` | `{"w": -3000}` (Pflicht; − = Einspeisung) |
| `POST /stations` | `{"count": 3}` hinzufügen, `{"clear": true}` leeren |
| `POST /station/<id>` | `{"action": "plug_in"\|"plug_out"\|"error", "vehicle_phases": 1-3, "max_power_kw": 11, "manual_amperage": 10 oder null}` |
| `POST /heater/<slot>` | `{"nominal_power_w", "temperature_c", "max_temp_c", "auto_temperature", "force_max_temp"}` |
| `POST /battery` | `{"power_w", "soc", "temperature_c"}` |
| `POST /community` | `{"enabled": true, "others_w": -16000, "key": 0.5}` |
| `POST /smartfox` | `{"ip": "192.168.100.170"}` |
| `POST /configure` | `{"device": "all"\|"meter"\|"battery"\|"station"\|"heater", "id": n, "remove": false}` |

Beispiel: `curl -X POST localhost:8020/station/1 -d '{"action":"plug_in"}'`

## Steuerung durch Claude (MCP)

`mcp_server.py` stellt die Steuer-API einer Claude-Session als Tools zur Verfügung (Stationen, Heizstäbe, Batterien, Netz, Energiegemeinschaft, Smartfox konfigurieren, Polling):

```bash
venv/bin/pip install -r requirements-mcp.txt
claude mcp add prio-list-simulator -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

`main.py` muss laufen. Alle Endpunkte und Tools: [API.md](API.md). Hinweise zum Code für Claude-Sessions: [claude_memory.md](claude_memory.md).

## Device Polling

Tab "Device Polling": liest die Liveview-JSON-API des echten Smartfox (`/api/liveview/data`, `/api/liveview/devices`, IP = Smartfox-IP oben) alle N Sekunden. Als Überschuss-Quelle lässt sich die Netzleistung des Geräts übernehmen; optional wird die Leistung der Verbrauchsregler / des Analogausgangs zum Überschuss addiert.

## Smartfox automatisch einstellen

Oben im Fenster die **Smartfox-IP** eintragen; als Simulator-IP gilt die Auswahl in der Zeile "Modbus TCP". Jedes Gerät hat **Konfigurieren** / **Entfernen**, "Alle konfigurieren" stellt Zähler, Stationen, Heizstäbe und Batterien nacheinander ein. Der Simulator schreibt dazu über die JSON-API des Smartfox (`POST /api/devices/config/<typ>/<slot>`, nur die geänderten Felder; andere Einstellungen des Slots bleiben):

| Simulator | Smartfox-Slot | Produkt (`schnittstelle.id`) | Unit-ID / Port |
|-----------|---------------|------------------------------|----------------|
| Station N | Ladestation N−1 (max. 5) | 24 `CC_ID_CC_DEBUG` | `address` = N, Port 5020 |
| Heizstab k | Verbrauchsregler k | 10 `VB_ID_DEBUG`, Leistung = Nennleistung | 210 + k, Port 5020 (fest) |
| Victron-Batterie N | Batterie N−1 (max. 3) | 9 `BS_ID_VICTRON_VE_CTRL` | 242, Port 502 (fest), IP = lokale IP der Batterie (falls gesetzt) |
| Slave-Zähler | Zähler (Slot wählbar) | 8 Smartfox Slave TCP, "auf Netz addieren" | 1, Port 502 (fest) |

"Entfernen" setzt das Produkt des Slots auf 0. Betriebsart (Automatik/Manuell) und Priorität werden nicht verändert.

Hinweise:
- Jeder Klick speichert die Einstellungen im Flash des Smartfox.
- Heizstab 1/2: Die Firmware startet den Treiber bei einer Änderung nur, wenn Slot 0 auch ein Debug-Heizstab ist (sonst erst nach Neustart) - der Simulator warnt dann.
- Ladestationen brauchen eine CC-Lizenz (Testlizenz reicht).

## Energiegemeinschaft (EVG)

Ersetzt den Server `my.smartfox.at` für die Gemeinschaftswerte. Funktioniert nur mit einer **Debug-Build-Firmware** (der Endpunkt fehlt im Release-Build).

Tab "Energiegemeinschaft": "Senden aktiv" anhaken (geht an die Smartfox-IP oben im Fenster). Alle 10 s geht ein

```
POST http://<Geräte-IP>/api/debug/community/<community_power>/<community_my_surplus>
```

an das Gerät, berechnet wie vom Server:

| Wert | Berechnung |
|------|------------|
| `community_power` | eigene Netzleistung (Unit-ID 100, von vor 10 s – so verzögert kommt sie über den Liveview-Upload beim Server an) + "Netz übrige Mitglieder" (− = Einspeisung) |
| `community_my_surplus` | Schlüssel × `community_power` |

Das Gerät rechnet die eigene Leistung wieder heraus; der erwartete Anteil S = −Schlüssel × "Netz übrige Mitglieder" wird im Tab angezeigt (die Firmware filtert S mit τ = 60 s).
Solange Werte kommen, ruft das Gerät den Server nicht ab; 30 s nach dem letzten POST übernimmt wieder der Server.

Ohne Simulator geht es auch per `curl -X POST http://<Geräte-IP>/api/debug/community/-6000/-3000`.

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
