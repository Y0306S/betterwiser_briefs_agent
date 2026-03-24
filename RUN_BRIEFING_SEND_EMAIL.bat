@echo off
title BetterWiser Briefing Agent — Send Email
echo ============================================
echo  BetterWiser Legal-Tech AI Briefing Agent
echo  Mode: SEND EMAIL (requires Azure AD setup)
echo ============================================
echo.

cd /d "%~dp0"
call conda activate bw-briefing 2>nul || (
    echo ERROR: conda environment 'bw-briefing' not found.
    echo Please run the setup steps in SETUP_CHECKLIST.md first.
    pause
    exit /b 1
)

echo Starting briefing generation and sending for current month...
echo (This will take 15-30 minutes. Do not close this window.)
echo.

python -m src.orchestrator --send

echo.
echo ============================================
echo  DONE. Check the runs\ folder for output.
echo  Briefings have been sent if Azure was configured.
echo ============================================
pause
