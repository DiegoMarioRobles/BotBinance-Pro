@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bot BTC Top-Down Pullback

if not exist ".venv\Scripts\python.exe" (
  echo Primera ejecucion: creando el entorno e instalando dependencias...
  py -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" main.py %*
echo.
echo El bot se ha detenido. Las ordenes Stop Loss / Take Profit siguen activas en Binance.
pause
