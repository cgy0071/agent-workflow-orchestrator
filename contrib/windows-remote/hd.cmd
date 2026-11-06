@echo off
rem Runs hd.ps1 from the same folder. Put both files in a folder on PATH.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hd.ps1" %*
