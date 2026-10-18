@echo off
cd /d "%~dp0"
title WeChat OneBot Bridge
chcp 65001 >nul 2>nul

echo ============================================
echo   WeChat to AstrBot Bridge
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo and make sure "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
)

if not exist "config.json" (
    echo [SETUP] config.json not found. Starting setup wizard...
    echo.
    python init_config.py
    if errorlevel 1 (
        echo.
        echo [ERROR] Setup cancelled or failed.
        pause
        exit /b 1
    )
)

if not exist ".deps_ok" (
    echo [SETUP] Installing dependencies, first run only...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed. Check your network or use a mirror:
        echo   python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
    echo ok> .deps_ok
    echo.
)

echo [RUN] Starting bridge... Ctrl+C to stop.
echo.
python main.py
echo.
echo [EXIT] Bridge stopped.
pause
