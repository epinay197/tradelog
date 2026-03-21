@echo off
setlocal enabledelayedexpansion
title TradeLog Auto-Sync Setup
color 0A
echo.
echo  ========================================
echo   TradeLog Pro - Windows Auto-Sync Setup
echo  ========================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Python not found. Installing via winget...
    winget install Python.Python.3.11 -e --silent
    echo [*] Please restart this script after Python installs.
    pause & exit
)
echo [OK] Python found.

:: Install dependencies
echo [*] Installing Python dependencies...
pip install requests win10toast --quiet --break-system-packages 2>nul || pip install requests win10toast --quiet
echo [OK] Dependencies ready.

echo.
echo  --- GitHub Configuration ---
set /p GH_OWNER=GitHub username [epinay197]: 
if "!GH_OWNER!"=="" set GH_OWNER=epinay197
set /p GH_REPO=Repository name [tradelog]: 
if "!GH_REPO!"=="" set GH_REPO=tradelog
set /p GH_TOKEN=Personal Access Token (github_pat_...): 
set /p GH_BRANCH=Branch [main]: 
if "!GH_BRANCH!"=="" set GH_BRANCH=main

echo.
echo  --- Sierra Chart Configuration ---
set /p SC_DIR=Path to Sierra Chart Data folder [C:\SierraChart\Data]: 
if "!SC_DIR!"=="" set SC_DIR=C:\SierraChart\Data
set /p CLOSE_HOUR=Auto-sync hour (24h, ET) [16]: 
if "!CLOSE_HOUR!"=="" set CLOSE_HOUR=16
set /p CLOSE_MIN=Auto-sync minute [30]: 
if "!CLOSE_MIN!"=="" set CLOSE_MIN=30

echo.
echo  --- Trade Defaults ---
set /p DEFAULT_TICK=Tick value (NQ=5, MNQ=0.5, ES=12.5) [5]: 
if "!DEFAULT_TICK!"=="" set DEFAULT_TICK=5
set /p DEFAULT_COMM=Commission per contract [4.0]: 
if "!DEFAULT_COMM!"=="" set DEFAULT_COMM=4.0

:: Write config.json next to the script
set SCRIPT_DIR=%~dp0
set CONFIG=%SCRIPT_DIR%config.json
echo { > "%CONFIG%"
echo   "gh_owner": "!GH_OWNER!", >> "%CONFIG%"
echo   "gh_repo":  "!GH_REPO!", >> "%CONFIG%"
echo   "gh_token": "!GH_TOKEN!", >> "%CONFIG%"
echo   "gh_branch":"!GH_BRANCH!", >> "%CONFIG%"
echo   "sc_dir":   "!SC_DIR!", >> "%CONFIG%"
echo   "close_hour":"!CLOSE_HOUR!", >> "%CONFIG%"
echo   "close_min": "!CLOSE_MIN!", >> "%CONFIG%"
echo   "default_tick":"!DEFAULT_TICK!", >> "%CONFIG%"
echo   "default_comm":"!DEFAULT_COMM!" >> "%CONFIG%"
echo } >> "%CONFIG%"
echo [OK] config.json written.

:: Test GitHub connection
echo [*] Testing GitHub connection...
python -c "import urllib.request,json; r=urllib.request.Request('https://api.github.com/repos/!GH_OWNER!/!GH_REPO!',headers={'Authorization':'token !GH_TOKEN!','Accept':'application/vnd.github.v3+json'}); print('  Repo:', json.loads(urllib.request.urlopen(r).read())['full_name'])"
if %errorlevel% neq 0 (
    echo [ERROR] GitHub connection failed. Check your token and repo name.
    pause & exit
)
echo [OK] GitHub connected.

:: First sync
echo [*] Running first sync...
python "%SCRIPT_DIR%sc_auto_bridge.py"

:: Register Windows Task Scheduler
set TASK_NAME=TradeLog Auto-Sync
set SYNC_TIME=!CLOSE_HOUR!:!CLOSE_MIN!
echo [*] Registering Task Scheduler (weekdays at !SYNC_TIME! ET)...
schtasks /create /tn "!TASK_NAME!" /tr "python \"%SCRIPT_DIR%sc_auto_bridge.py\"" /sc weekly /d MON,TUE,WED,THU,FRI /st !SYNC_TIME! /rl highest /f >nul
if %errorlevel% equ 0 (
    echo [OK] Task scheduled: runs weekdays at !SYNC_TIME!
) else (
    echo [WARN] Could not register task. Run as Administrator to schedule.
)

echo.
echo  ========================================
echo   Setup complete!
echo   Journal: https://!GH_OWNER!.github.io/!GH_REPO!/
echo   Syncs automatically weekdays at !SYNC_TIME! ET
echo  ========================================
echo.
pause
