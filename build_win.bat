@echo off
echo ================================
echo EV Charging Simulator - Windows Build
echo ================================

python -m venv venv_win
call venv_win\Scripts\activate.bat

pip install PyQt6 pymodbus pyinstaller

pyinstaller --name "EV Charging Simulator" ^
    --windowed ^
    --onedir ^
    --noconfirm ^
    main.py

echo.
echo Build complete! Output: dist\EV Charging Simulator\
pause