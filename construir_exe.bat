@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Construyendo BotBTC.exe

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno e instalando dependencias...
  py -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt || goto :error
".venv\Scripts\python.exe" make_icon.py || goto :error

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name BotBTC ^
  --icon assets\botbtc.ico ^
  --add-data "botbinance\dashboard\templates;botbinance\dashboard\templates" ^
  --add-data "botbinance\dashboard\static;botbinance\dashboard\static" ^
  --collect-submodules keyring --copy-metadata keyring --hidden-import win32ctypes.core ^
  app_desktop.py || goto :error

echo.
echo ============================================================
echo  Listo:  dist\BotBTC.exe   (copialo a cualquier PC con Windows)
echo ============================================================
pause
exit /b 0

:error
echo.
echo *** La construccion fallo. Revisa los mensajes de arriba. ***
pause
exit /b 1
