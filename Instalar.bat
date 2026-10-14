@echo off
rem Roda o instalador PowerShell sem precisar mudar a política de execução.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1" %*
echo.
pause
