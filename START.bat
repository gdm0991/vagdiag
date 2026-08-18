@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title PDC Diagnostics - VW Polo Sedan
python\python.exe app\menu.py
if errorlevel 1 pause
