@echo off
cd /d D:\claude自动
echo 启动 WeChat-Claude 桥接...
echo 收到指令时会自动弹出 Claude 终端窗口显示执行过程
echo.
python -u wechat_bridge.py 文件传输助手
pause
