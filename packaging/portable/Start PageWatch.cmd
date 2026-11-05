@echo off
rem PageWatch, portable. Everything PageWatch knows lives in the "data" folder next to this
rem file, so this whole folder can be copied to another PC or a USB stick, or run beside another
rem PageWatch that has its own data. See README-portable.txt.
setlocal
set "HERE=%~dp0"
set "DATA=%HERE%data"
if not exist "%DATA%" mkdir "%DATA%"

rem Start the engine for this data folder. If one is already running for it, the new one says so
rem and exits, which is fine.
start "" "%HERE%pagewatch-engine.exe" --data-dir "%DATA%" --no-console-log

rem Wait up to 30 seconds for the engine to publish its lockfile, so the window finds it.
set /a TRIES=0
:wait
if exist "%DATA%\engine.lock" goto ready
set /a TRIES+=1
if %TRIES% GEQ 30 goto ready
ping -n 2 127.0.0.1 >nul
goto wait

:ready
start "" "%HERE%pagewatch-ui.exe" --data-dir "%DATA%"
endlocal
