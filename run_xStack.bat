@echo off
setlocal EnableDelayedExpansion
title xStack
rem Do not inherit unrelated ChemOffice Python modules.
set "PYTHONPATH="
set "XSTACK_CHECK=import numpy, matplotlib, PyQt6.QtWidgets, scipy"

rem Use the first interpreter that has all xStack dependencies installed:
rem   1. XSTACK_PYTHON, if set before running this file
rem   2. the project's .venv
rem   3. Python 3.14 down to 3.11 via the py launcher, then python on PATH
set "XSTACK_EXE="
if defined XSTACK_PYTHON (
    if exist "%XSTACK_PYTHON%" call :try "%XSTACK_PYTHON%"
)
if not defined XSTACK_EXE if exist "%~dp0.venv\Scripts\python.exe" call :try "%~dp0.venv\Scripts\python.exe"
for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined XSTACK_EXE call :try_launcher %%V
)
if not defined XSTACK_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined XSTACK_EXE call :try "%%P"
    )
)

if not defined XSTACK_EXE (
    echo No Python installation with the xStack dependencies was found.
    echo Install them into your Python 3 environment, for example:
    echo     py -m pip install numpy matplotlib PyQt6 scipy
    echo or set XSTACK_PYTHON to a python.exe that already has them.
    pause
    exit /b 1
)

rem Preserve quoted file arguments and the caller's working directory.
"%XSTACK_EXE%" "%~dp0xStack.py" %*
if errorlevel 1 (
    echo xStack exited with an error. See the message above.
    echo Python used: %XSTACK_EXE%
    pause
    exit /b 1
)
endlocal
exit /b 0

:try_launcher
where py >nul 2>nul || exit /b 1
for /f "delims=" %%E in ('py -%1 -c "import sys; print(sys.executable)" 2^>nul') do call :try "%%E"
exit /b 0

:try
"%~1" -c "%XSTACK_CHECK%" >nul 2>nul && set "XSTACK_EXE=%~1"
exit /b 0
