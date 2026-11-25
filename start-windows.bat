@echo off
cd /d "%~dp0"
if "%STUDY_PORT%"=="" set STUDY_PORT=8765
echo Starting Study Exchange at http://127.0.0.1:%STUDY_PORT%
echo Close this window to stop the server.
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:%STUDY_PORT%/"
py -3 server.py
pause
