@echo off
rem Pre-market analysis (run on trading days around 08:00)
rem Double-click to run. Report opens in the browser when done.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python -u analyze.py pre
if errorlevel 1 echo. & echo *** Finished with problems, see messages above ***
start "" "%~dp0docs\index.html"
echo.
pause
