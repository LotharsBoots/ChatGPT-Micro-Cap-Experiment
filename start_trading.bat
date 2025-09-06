@echo off
setlocal

REM Change to this script's directory
cd /d %~dp0

echo ========================================
echo    Automated Trading Dashboard Setup
echo ========================================
echo.
echo Checking Python...
where python >nul 2>nul
if %errorlevel% neq 0 (
  echo Python not found in PATH. Please install Python 3.12+ from https://python.org and retry.
  pause
  exit /b 1
)

echo Upgrading pip, setuptools, and wheel (fixes 'distutils' issue)...
python -m pip install --upgrade pip setuptools wheel
if %errorlevel% neq 0 (
  echo Failed to upgrade pip/setuptools/wheel.
  pause
  exit /b 1
)

echo.
echo Installing Python dependencies from requirements.txt ...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo Failed to install dependencies. See errors above.
  pause
  exit /b 1
)

echo.
echo Starting the trading dashboard...
echo Open your browser to: http://localhost:5000
echo Press Ctrl+C in this window to stop the app.

python app.py

pause
