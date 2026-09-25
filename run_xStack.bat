@echo off
setlocal
title xStack Preview
rem Use the Python installation already verified on this computer.
set "XSTACK_PYTHON=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not exist "%XSTACK_PYTHON%" (
    echo Python was not found at:
    echo %XSTACK_PYTHON%
    echo Edit XSTACK_PYTHON in this BAT to point to your Python 3 installation.
    pause
    exit /b 1
)
rem Do not inherit unrelated ChemOffice Python modules.
set "PYTHONPATH="
rem Preserve quoted file arguments and the caller's working directory.
"%XSTACK_PYTHON%" "%~dp0xStack.py" %*
if errorlevel 1 (
    echo xStack exited with an error. See the message above.
    pause
    exit /b 1
)
endlocal
