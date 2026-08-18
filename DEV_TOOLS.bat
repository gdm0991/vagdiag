@echo off
chcp 65001 >nul
cd /d "%~dp0"
title VAG Diag - developer tools
:menu
cls
echo ======================================================================
echo   INSTRUMENTY RAZRABOTCHIKA / DEVELOPER TOOLS
echo ======================================================================
echo.
echo   Ishodniki lezhat v papke app - eto obychnye tekstovye fayly.
echo   Sources are in the app folder - plain editable text files.
echo.
echo   1 - Otkryt papku s ishodnikami / Open sources folder
echo   2 - Zapustit avtotesty / Run smoke tests
echo   3 - Zapustit zaglushku adaptera / Start adapter mock
echo   4 - Zapustit programmu / Start the program
echo   0 - Vyhod / Exit
echo.
set /p c=Nomer / Number: 
if "%c%"=="1" start "" explorer "%~dp0app" ^& goto menu
if "%c%"=="2" goto tests
if "%c%"=="3" goto mock
if "%c%"=="4" start "" "%~dp0START_GUI.bat" ^& goto menu
if "%c%"=="0" exit /b 0
goto menu
:tests
cls
python\python.exe tests\test_smoke.py
echo.
pause
goto menu
:mock
start "Adapter mock" python\python.exe app\mock_elm327.py 35003 --glitch
echo.
echo Mock started on 127.0.0.1:35003
echo Connect the program to this address to test without a car.
echo.
pause
goto menu
