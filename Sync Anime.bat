@echo off
cd /d "%~dp0"

rem Load credentials from .env (KEY=VALUE lines, no spaces around =)
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "%%A=%%B"
    )
) else (
    echo [warn] .env not found. Create one with QBIT_USERNAME and QBIT_PASSWORD.
)

.venv\Scripts\python.exe anime_downloader.py
echo.
pause
