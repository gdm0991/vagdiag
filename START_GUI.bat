@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VAG Diagnostics
python\python.exe app\webui.py
if errorlevel 1 pause
