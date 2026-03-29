@echo off
title BetterWiser Briefing Agent — Demo Run
echo ============================================
echo  BetterWiser Legal-Tech AI Briefing Agent
echo  Mode: DEMO (smoke test, minimal API usage)
echo ============================================
echo.
echo  What this does:
echo    - Runs the FULL pipeline (all 6 passes, all 3 tracks)
echo    - Uses pre-built demo data (no real web scraping)
echo    - Uses Claude Haiku (cheapest model, ~$0.05 total)
echo    - Saves demo HTML to runs\  folder
echo    - Does NOT send email (add --send-email to send)
echo.
echo  Estimated time: 2-4 minutes
echo  Estimated cost: under USD 0.10
echo.

cd /d "%~dp0"
call conda activate bw-briefing 2>nul || (
    echo ERROR: conda environment 'bw-briefing' not found.
    echo Please run the setup steps in SETUP_CHECKLIST.md first.
    pause
    exit /b 1
)

:: Check for --send-email flag passed to this bat file
set EXTRA_FLAGS=
if "%~1"=="--send-email" set EXTRA_FLAGS=--send-email

python demo_run.py %EXTRA_FLAGS%

echo.
echo ============================================
echo  DONE. Check the runs\ folder for HTML output.
echo  Look for a folder named DEMO_*.
echo ============================================
pause
