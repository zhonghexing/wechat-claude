Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = "D:\claude自动"
ws.Run "cmd /c python -u wechat_bridge.py 文件传输助手", 2, False
