# Windows Build

## Voraussetzungen
- Python 3.10+ installiert (https://python.org)
- Git (optional)

## Build

```cmd
build_win.bat
```

Die `.exe` liegt danach in `dist\EV Charging Simulator\EV Charging Simulator.exe`.

## Manuelles Build (ohne Skript)

```cmd
python -m venv venv_win
venv_win\Scripts\activate
pip install PyQt6 pymodbus pyinstaller
pyinstaller --name "EV Charging Simulator" --windowed --onedir --noconfirm main.py
```