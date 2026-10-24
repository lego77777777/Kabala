@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Kabala for the first time...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)
start "" ".venv\Scripts\pythonw.exe" -m kabala
exit /b 0

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed (python.org or Microsoft Store).
pause
exit /b 1
