@echo off
cd /d "%~dp0"
title PTT Monitor - Step 2 - Setup
set "PYEXE="

py -3 --version >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto RUN

python --version >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto RUN

goto NOPYTHON

:RUN
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
%PYEXE% setup.py
echo.
pause
exit /b

:NOPYTHON
echo.
echo  ==========================================================
echo    Python is NOT installed. Please run 1_install first.
echo    Opening the Chinese instructions in Notepad...
echo  ==========================================================
echo.
start "" notepad "python-install-help.txt"
pause
exit /b 1
