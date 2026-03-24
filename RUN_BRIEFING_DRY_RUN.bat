@echo off
title BetterWiser Briefing Agent — Dry Run
echo ============================================
echo  BetterWiser Legal-Tech AI Briefing Agent
echo  Mode: DRY RUN (saves HTML to disk only)
echo ============================================
echo.

cd /d "%~dp0"
call conda activate bw-briefing 2>nul || (
    echo ERROR: conda environment 'bw-briefing' not found.
    echo Please run the setup steps in SETUP_CHECKLIST.md first.
    pause
    exit /b 1
)

echo Starting briefing generation for current month...
echo (This will take 15-30 minutes. Do not close this window.)
echo.

python -m src.orchestrator --dry-run

echo.
echo ============================================
echo  DONE. Check the runs\ folder for output.
echo  Open the .html files in Chrome or Edge.
echo ============================================
pause
