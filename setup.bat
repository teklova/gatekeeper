@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD=python"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 set "PYTHON_CMD=py -3"

%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or later is required.
    exit /b 1
)

%PYTHON_CMD% -m venv --upgrade .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo Setup complete. Activate with .venv\Scripts\activate.bat