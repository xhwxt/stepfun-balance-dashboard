@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH. Please install Python 3.8+ first.
  pause
  exit /b 1
)
python query.py
