@echo off
setlocal
cd /d "%~dp0"
set "TEMP=%~dp0.cache\tmp"
set "TMP=%TEMP%"
set "TMPDIR=%TEMP%"
set "PIP_CACHE_DIR=%~dp0.cache\pip"
set "PYTHONPYCACHEPREFIX=%~dp0.cache\pycache"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.cache\playwright"
if not exist "%TEMP%" mkdir "%TEMP%"
".venv\Scripts\python.exe" ui\server.py
if errorlevel 1 pause
