@echo off
rem Abre o Copiloto de Reuniões sem janela de console.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" app.py
