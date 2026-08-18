@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Build single EXE
echo.
echo This script is OPTIONAL. It builds a single pdc_diag.exe file.
echo The package already works without it - just run START.bat
echo.
echo Requires a normally installed Python and internet access.
echo.
pause
where python >nul 2>nul
if errorlevel 1 goto nopython
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto pipfail
python -m PyInstaller --onefile --console --name pdc_diag --paths app --hidden-import pdc_core --hidden-import pdc_diag app\menu.py
if exist dist\pdc_diag.exe (echo.& echo DONE: dist\pdc_diag.exe) else (echo.& echo BUILD FAILED - see messages above)
pause
exit /b 0
:nopython
echo Python not found. Install from python.org with "Add Python to PATH".
pause
exit /b 1
:pipfail
echo Could not install pyinstaller. Check internet connection.
pause
exit /b 1
