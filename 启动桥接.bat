@echo off
cd /d "%~dp0"
echo Starting WeChat-Claude Bridge...
echo.
python -u wechat_bridge.py
echo.
echo Bridge exited (code: %ERRORLEVEL%)
pause
