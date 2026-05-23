# WeChat ↔ Claude Bridge

通过微信远程操控 Claude Code 执行任务。在手机上发微信消息，Claude 帮你截图、写代码、查资料、管理文件、操控应用——结果秒回微信。

## 演示

```
手机微信发送:  claude 帮我截一张电脑屏幕截图 stop
微信收到回复:  截图已保存 D:\screenshot.png (1.2 MB)

手机微信发送:  claude 酷狗播放恋人 stop
微信收到回复:  酷狗: 恋人 — 完成！正在播放
```

## 功能特性

- **微信远程操控** — 手机发指令，Claude 在电脑上执行
- **会话连贯** — `--continue` 自动跨消息保持上下文
- **本地快捷指令** — 酷狗播放等常用操作秒级响应，不走 Claude
- **静默监控** — 空闲时零焦点干扰，只在处理命令时激活窗口
- **自动恢复** — 微信最小化时自动还原窗口
- **单实例锁** — 重复启动自动拦截
- **自动弹窗** — 收到指令时弹出 Claude 终端显示执行过程
- **开机自启** — 支持 Windows 启动文件夹自启
- **比例坐标** — UI 校准使用百分比，自适应窗口大小变化

## 系统架构

```
手机微信 ──消息──→ 微信桌面版 ──UIAutomation──→ wechat_bridge.py
                                                    │
                                         ┌─ 本地快捷指令 (酷狗等)
                                         │
                                    claude/stop 协议解析
                                         │
                                   claude --continue -p
                                         │
                                    Claude Code 执行
                                         │
                              结果 ←── 自动回复到微信
```

## 项目文件

### 核心

| 文件 | 说明 |
|------|------|
| `wechat_bridge.py` | **主程序** — Claude 桥接，状态机驱动 |
| `wechat_send.py` | 工具 — 命令行发送文本消息 |
| `wechat_reply.py` | 工具 — 监控并自动回复 |
| `wechat_send_image.py` | 工具 — 发送剪贴板图片 |

### 酷狗音乐

| 文件 | 说明 |
|------|------|
| `kugou_play.py` | 搜索并播放歌曲（使用比例坐标） |
| `kugou_calibrate.py` | UI 位置校准工具（鼠标悬停 + F8 记录） |
| `kugou_positions.json` | 校准数据（比例坐标，自适应窗口大小） |

### 启动脚本

| 文件 | 说明 |
|------|------|
| `启动桥接.bat` | 一键启动桥接 |
| `校准酷狗.bat` | 一键启动酷狗校准 |
| `安装开机自启.bat` | 安装到 Windows 启动文件夹 |
| `卸载开机自启.bat` | 卸载开机自启 |
| `WeChatBridge.vbs` | 开机自启 VBS 脚本（最小化启动） |

## 环境要求

- **Windows 10/11** — UIAutomation 依赖 Windows UI 框架
- **Python 3.10+** — 需安装 `uiautomation`, `pyautogui`, `pillow`
- **微信桌面版** — 中英文版本均支持
- **Claude Code CLI** — v2.1+，需配置 `CLAUDE_CLI` 路径

## 安装

```bash
# 1. 安装依赖
pip install uiautomation pyautogui pillow

# 2. 修改 wechat_bridge.py 中的 Claude CLI 路径
CLAUDE_CLI = r"C:\...\claude.exe"

# 3. (可选) 校准酷狗音乐位置
双击 校准酷狗.bat
```

## 使用

### 启动桥接

```bash
# 终端启动
python wechat_bridge.py

# 或双击
启动桥接.bat
```

### 通信协议

```
claude <任务内容> stop          # 单条命令
claude                         # 多行命令
<第1行>
<第2行>
stop
```

规则：
- `claude` 开头 → 激活
- `stop` 结尾 → 执行
- 无 `claude` 的消息 → **忽略**（不影响日常使用）

### 本地快捷指令

| 命令模式 | 说明 | 响应 |
|----------|------|------|
| `claude 酷狗播放<歌名> stop` | 播放歌曲 | 秒级，不走 Claude |
| `claude 重置 stop` | 清空对话上下文 | 下次从新会话开始 |

### 工具脚本

```bash
python wechat_send.py "消息" "联系人"        # 发文本
python wechat_reply.py "联系人" "回复内容"    # 自动回复
python wechat_send_image.py "联系人"          # 发剪贴板图片
python kugou_play.py "歌名"                   # 酷狗播放
```

## 酷狗音乐集成

校准一次，精准永久：

1. 打开酷狗音乐
2. 双击 `校准酷狗.bat`
3. 鼠标悬停到 4 个位置，分别按 F8：

| 顺序 | 位置 |
|------|------|
| 1/4 | 搜索框中间 |
| 2/4 | 放大镜图标 |
| 3/4 | 第一首歌歌名 |
| 4/4 | 播放按钮 |

校准数据使用**比例坐标**（%），酷狗窗口缩放后仍然精准。

## 开机自启

```bash
双击 安装开机自启.bat    # 安装
双击 卸载开机自启.bat    # 卸载
```

开机后桥接自动以最小化窗口运行，静默监控微信。

## 常见问题

**Q: 微信最小化后没反应？**
A: 已内置自动还原机制，检测到命令时自动恢复窗口。

**Q: 重复启动会怎样？**
A: 单实例锁会提示"桥接已在运行中"并退出。

**Q: 酷狗换了窗口大小还能用吗？**
A: 比例坐标自适应，大小变化不影响。如果 UI 布局大改，重新校准即可。

**Q: 微信变成英文版 (Weixin) 怎么办？**
A: 已兼容中英文窗口名，无需配置。

**Q: Claude 上下文不连贯？**
A: 自动使用 `--continue` 保持会话。发送 `claude 重置 stop` 清空上下文。

## 技术要点

- **UI 自动化**: `uiautomation` 操作微信 Qt/Skia 控件树，`pyautogui` 处理自定义渲染应用
- **消息检测**: `ChatSessionCell.Name` 监听聊天列表预览变化
- **窗口管理**: `ctypes` + Win32 API (`ShowWindow`, `SetForegroundWindow`)
- **状态机**: IDLE → COLLECT → EXECUTE → IDLE
- **进程隔离**: `subprocess` + `CREATE_NEW_CONSOLE` 弹出 Claude 窗口
- **焦点控制**: 被动监控零激活，处理命令时才 `SetForegroundWindow`

## 许可

MIT
