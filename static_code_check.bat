@echo off
REM Static checks for the voice-transcriber bot. Run on Windows: static_code_check.bat
REM One-time setup: pip install ruff mypy

echo === ruff (lint / bug-find) ===
ruff check src bot.py
set RUFF_RC=%ERRORLEVEL%

echo.
echo === mypy (type check) ===
mypy src bot.py
set MYPY_RC=%ERRORLEVEL%

echo.
if %RUFF_RC%==0 if %MYPY_RC%==0 (
    echo ALL STATIC CHECKS PASSED
) else (
    echo STATIC CHECKS FOUND ISSUES  ^(ruff=%RUFF_RC% mypy=%MYPY_RC%^)
)
