@echo off
setlocal
set "TEMP=D:\TianxinWorker\.cache\tmp"
set "TMP=%TEMP%"
set "PSModuleAnalysisCachePath=D:\TianxinWorker\.cache\powershell\ModuleAnalysisCache"
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_history.ps1"
if errorlevel 1 (
  echo Cleanup did not finish. Keep this window and all backup files.
  pause
  exit /b 1
)
echo Done. Refresh the office workbench. Real sending is still locked.
pause
