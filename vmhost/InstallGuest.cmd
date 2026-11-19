@echo off
rem Run inside the paired virtual machine, after installing Guest Additions.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_guest.ps1"
pause
