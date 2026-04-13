@echo off
echo Building SquadMonitor.exe with PyInstaller...
echo.

pip install pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed.
    pause
    exit /b 1
)

pyinstaller --onefile --windowed --name "SquadMonitor" monitor.py
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo Build complete!
echo Executable: dist\SquadMonitor.exe
echo.
echo Copy dist\SquadMonitor.exe and config.json to your target directory.
pause
