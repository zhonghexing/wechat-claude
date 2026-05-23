# Claude 自动 — WeChat ↔ Claude 桥接

## 项目概述

Windows 微信桌面版 → UIAutomation → Claude Code 的桥接系统。通过手机微信远程操控电脑端 Claude 执行任务。

## 核心文件

- `wechat_bridge.py` — 主程序，状态机驱动（IDLE→COLLECT→EXECUTE）
- `kugou_play.py` — 酷狗音乐搜索播放（比例坐标，自适应窗口）
- `kugou_calibrate.py` — 酷狗 UI 位置校准（F8 记录）

## 开发调试

```bash
python wechat_bridge.py              # 启动桥接
python wechat_send.py "测试"          # 发测试消息
python kugou_play.py "歌名"           # 测试酷狗播放
python kugou_calibrate.py            # 重新校准酷狗位置
```

## 关键配置

- `CLAUDE_CLI` — Claude Code exe 路径
- `kugou_positions.json` — 酷狗校准数据（比例坐标 %），不提交 git

## 技术栈

- uiautomation — 微信 UI 自动化
- pyautogui — 酷狗等自绘应用屏幕操作
- ctypes — Win32 API 窗口管理
- subprocess — Claude CLI + 本地快捷指令
