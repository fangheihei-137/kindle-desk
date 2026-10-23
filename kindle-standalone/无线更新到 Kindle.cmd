@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python wireless_update.py) else (py -3 wireless_update.py)
pause
