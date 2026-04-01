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
python --version >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo [*] Python not found. Installing via winget...
    winget install Python.Python.3.11 -e --silent
    echo [*] Please restart this script after Python installs.
    pause & exit
)
echo [OK] Python found.

:: Install dependencies
echo [*] Installing Python dependencies...
pip install requests win10toast --quiet --break-system-packages 2>/dev/null || pip install requests win10toast --quiet
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
python "%SCRIPT_DIR%sc_auto_bridge.py" --force

:: Remove any old tasks
schtasks /delete /tn "TradeLog Auto-Sync" /f >/dev/null 2>&1
schtasks /delete /tn "TradeLog Midday-Sync" /f >/dev/null 2>&1
schtasks /delete /tn "TradeLog Close-Sync" /f >/dev/null 2>&1
schtasks /delete /tn "TradeLog Hourly-Sync" /f >/dev/null 2>&1

:: Register hourly task via PowerShell (supports repetition interval)
echo [*] Registering hourly Task Scheduler (06:00-23:00, ET gated in Python)...
powershell -Command "$xml = @'
<?xml version=\"1.0\" encoding=\"UTF-16\"?>
<Task version=\"1.3\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">
  <Triggers><CalendarTrigger><Repetition><Interval>PT1H</Interval><Duration>PT17H</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2026-01-01T06:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id=\"Author\"><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT1H</ExecutionTimeLimit></Settings>
  <Actions Context=\"Author\"><Exec><Command>python</Command><Arguments>\"%SCRIPT_DIR%sc_auto_bridge.py\"</Arguments></Exec></Actions>
</Task>
'@; Register-ScheduledTask -TaskName 'TradeLog Hourly-Sync' -Xml $xml -Force"
if %errorlevel% equ 0 (
    echo [OK] Hourly sync scheduled: every hour 06:00-23:00, ET market-hours gated
) else (
    echo [WARN] Could not register task. Run as Administrator.
)

:: Register analytics task (every 15 min, ET market-hours gated in Python)
echo [*] Registering analytics task (every 15 min)...
schtasks /create /tn "TradeLog Analytics" /tr "python \"%SCRIPT_DIR%trade_analytics.py\"" /sc minute /mo 15 /f >/dev/null
if %errorlevel% equ 0 (
    echo [OK] Analytics scheduled: every 15 min, ET market-hours gated
) else (
    echo [WARN] Could not register analytics task.
)

echo.
echo  ========================================
echo   Setup complete!
echo   Journal:   https://!GH_OWNER!.github.io/!GH_REPO!/
echo   Analytics: https://!GH_OWNER!.github.io/!GH_REPO!/analytics.html
echo   Trade sync: hourly during ET market hours (Mon-Fri 8AM-4PM)
echo   Analytics:  every 15 min during ET market hours
echo  ========================================
echo.
pause
