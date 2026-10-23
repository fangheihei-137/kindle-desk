@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (python usb_editor.pyw) else (py -3 usb_editor.pyw)
