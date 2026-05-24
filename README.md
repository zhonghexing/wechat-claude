# WeChat ↔ Claude Bridge

手机微信远程控制电脑端 Claude Code。发微信消息，Claude 帮你截图、写代码、管理文件、操控应用——结果秒回微信。

## 两套桥接

| 方案 | 文件 | 依赖 | 推荐场景 |
|------|------|------|---------|
| **ClawBot** | `wechat_clawbot_bridge.py` | 微信 ClawBot 插件 | ✅ 日常使用 |
| **UIA** | `wechat_bridge.py` | 桌面微信 + UIAutomation | 备用/无插件 |

## 快速开始（ClawBot 推荐）

```bash
# 1. 装依赖
pip install requests

# 2. 启动（首次扫描二维码登录）
python wechat_clawbot_bridge.py

# 3. 发微信消息给 ClawBot
#    任意消息 → Claude 自动执行 → 微信回复
#    发"重置" → 清空会话
```

重启电脑后双击 `启动ClawBot桥接.bat` 即可。
可选：右键 `安装ClawBot开机自启.bat` → 以管理员运行，开机自动启动。

## 快速开始（UIA 备用）

```bash
pip install uiautomation pyautogui pillow
python wechat_bridge.py
```

通信协议：`claude <指令> stop`，或不带参数直接发 `claude` 进入对话模式。

## 电脑操控能力

Claude 通过微信能操控你的电脑：

| 能力 | 示例 |
|------|------|
| 💻 执行命令 | "帮我查看 CPU 温度" / "重启电脑" |
| 📁 文件操作 | "把桌面整理一下" / "下载这个文件" |
| 📸 截图 | "截图" / "截屏发给我" |
| 🎵 酷狗音乐 | "酷狗播放晴天" |
| 🔒 锁屏 | "锁屏" |
| ⚡ 关机 | "关机" (60秒延迟，可取消) |

## 项目结构

```
核心:
  wechat_clawbot_bridge.py   ClawBot 桥接（推荐）
  wechat_bridge.py           UIA 桥接（备用）

工具:
  wechat_send.py             命令行发消息
  wechat_reply.py            自动回复
  wechat_send_image.py       粘贴图片

酷狗:
  kugou_play.py              搜索播放
  kugou_calibrate.py         坐标校准

配置:
  config.env                 统一配置（环境变量 > 文件 > 默认值）
  .clawbot_state.json        ClawBot 登录凭据 & cursor

启动:
  启动ClawBot桥接.bat         一键启动 ClawBot
  安装ClawBot开机自启.bat      ClawBot 开机自启
  启动桥接.bat                UIA 一键启动
  安装开机自启.bat            UIA 开机自启
  卸载开机自启.bat            移除自启
```

## ClawBot 桥接原理

```
手机微信 → ClawBot 插件 → iLink API (腾讯云端)
                ↓
      POST ilink/bot/getupdates  (HTTP 长轮询 35s)
                ↓
      wechat_clawbot_bridge.py → Claude Code CLI
                ↓
      POST ilink/bot/sendmessage → 微信回复
```

- 基于腾讯官方 iLink Bot API（合法合规）
- 不依赖桌面微信，不操控 UI
- 服务端 cursor 天然去重，无需客户端防抖

## 配置 `config.env`

```ini
CTI_WORK_DIR=D:\claude自动
CTI_CLAUDE_CLI=<claude.exe 路径>
CTI_CLAUDE_EFFORT=low          # low | medium | high
CTI_CLAUDE_TIMEOUT=300         # Claude 执行超时秒数
CTI_CLAUDE_PERMISSION_MODE=bypassPermissions
CTI_MAX_RESPONSE_LENGTH=500    # 回复截断长度
```

## 许可

MIT
