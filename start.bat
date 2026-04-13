@echo off
cd /d "%~dp0"
python monitor.py
if %errorlevel% neq 0 (
    echo.
    echo Fehler beim Starten. Sind alle Abhaengigkeiten installiert?
    echo Tipp: pip install -r requirements.txt
    pause
)
