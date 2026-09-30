@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launch_interface.py
) else (
  python launch_interface.py
)
if errorlevel 1 (
  echo.
  echo Could not start. Install requirements with: python -m pip install -r requirements.txt
  pause
)
