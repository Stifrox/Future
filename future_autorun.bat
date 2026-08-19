@echo off
REM Opens a visible terminal, installs dependencies, and starts FUTURE automatically.
REM Works from any drive letter since it resolves its own folder location.
setlocal
set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

echo Installing/updating FUTURE dependencies...
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3.14 -m pip install --upgrade pip
    py -3.14 -m pip install -r requirements.txt
) else (
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
)

echo Starting FUTURE server...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%start_future_server.ps1"

echo.
echo FUTURE startup finished. This window will stay open so you can see any errors.
pause
