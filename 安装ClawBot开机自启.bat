@echo off
chcp 65001 >nul
echo 安装 ClawBot 桥接开机自启...

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\ClawBotBridge.vbs"

echo Set WshShell = CreateObject("WScript.Shell") > "%VBS%"
echo WshShell.Run "python -u D:\claude自动\wechat_clawbot_bridge.py", 0 >> "%VBS%"

echo ✅ 已安装开机自启
echo    开机后会静默启动 ClawBot 桥接（无窗口）
pause
