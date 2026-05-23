@echo off
cd /d %~dp0
set "DST=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WeChatBridge.vbs"
copy /Y WeChatBridge.vbs "%DST%" >nul
echo [OK] 已安装开机自启
echo 路径: %DST%
pause
