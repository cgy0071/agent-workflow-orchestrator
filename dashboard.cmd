@echo off
rem Double-click to open the workflow dashboard in your browser.
rem Extra arguments are passed to scripts\dashboard.py. Set AWO_PYTHON to choose an interpreter.
setlocal
set "DASHBOARD=%~dp0scripts\dashboard.py"
set "CHECK=import sys; sys.exit(sys.version_info < (3, 10))"
if defined AWO_PYTHON goto custom
py -3 -c "%CHECK%" >nul 2>nul
if errorlevel 1 goto try_python
py -3 -B "%DASHBOARD%" %*
goto finish
:try_python
python -c "%CHECK%" >nul 2>nul
if errorlevel 1 goto missing
python -B "%DASHBOARD%" %*
goto finish
:custom
"%AWO_PYTHON%" -B "%DASHBOARD%" %*
goto finish
:missing
echo Python 3.10 or newer was not found. Install it, or set AWO_PYTHON to the full path of python.exe.
pause
exit /b 1
:finish
if errorlevel 1 pause
