@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python sync_standalone.py) else (py -3 sync_standalone.py)
pause
