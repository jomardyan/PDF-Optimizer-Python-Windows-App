@echo off
setlocal

cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Could not open the application directory:
    echo         %~dp0
    exit /b 1
)

set "VENV_DIR=%CD%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

if not exist "app.py" (
    echo [ERROR] app.py was not found in:
    echo         %CD%
    exit /b 1
)

if not exist "requirements.txt" (
    echo [ERROR] requirements.txt was not found in:
    echo         %CD%
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo [SETUP] Creating the local Python environment at .venv ...
    call :create_venv
    if errorlevel 1 (
        echo [ERROR] Python could not create .venv.
        echo         Check that the venv module is installed, then try again.
        exit /b 1
    )
)

"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The existing .venv does not use Python 3.11 or newer.
    echo         Rename or remove "%VENV_DIR%", then run this launcher again.
    exit /b 1
)

echo [SETUP] Checking runtime dependencies ...
"%VENV_PY%" -m pip install --quiet -r "requirements.txt"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo         Check the messages above and your internet connection, then try again.
    exit /b 1
)

echo [START] Opening PDF Optimizer ...
"%VENV_PY%" "app.py" %*
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo [ERROR] PDF Optimizer exited with code %APP_EXIT%.
)
exit /b %APP_EXIT%

:create_venv
call :find_python
if errorlevel 1 exit /b 1
if /i "%PY_LAUNCHER%"=="py" (
    call py -3 -m venv "%VENV_DIR%"
) else (
    call python -m venv "%VENV_DIR%"
)
exit /b %ERRORLEVEL%

:find_python
where py >nul 2>&1
if not errorlevel 1 (
    call py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_LAUNCHER=py"
        exit /b 0
    )
)

where python >nul 2>&1
if not errorlevel 1 (
    call python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_LAUNCHER=python"
        exit /b 0
    )
)

echo [ERROR] Python 3.11 or newer was not found.
echo         Install 64-bit Python from https://www.python.org/downloads/windows/
echo         Enable the Python launcher or add Python to PATH, then run this file again.
exit /b 1
