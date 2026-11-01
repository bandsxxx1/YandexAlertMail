@echo off
title Mail alert v3
rem Exit codes: 0 = stopped by user, 2 = already running -> do not restart.
rem Anything else (3 = watchdog killed a hung script, 1 = crash) -> restart.
:loop
python "%~dp0yandex_alert_v3.py"
set RC=%errorlevel%
if "%RC%"=="0" goto :eof
if "%RC%"=="2" goto :eof
echo [%date% %time%] Script exited with code %RC%, restarting in 5 sec...
ping -n 6 127.0.0.1 >nul
goto loop
