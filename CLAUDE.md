# Claude 自动 — WeChat ↔ Claude 桥接

手机微信远程控制电脑端 Claude Code 执行任务。支持两套桥接方案。

## 推荐：ClawBot 桥接 `wechat_clawbot_bridge.py`

基于腾讯 iLink Bot API（微信 ClawBot 插件），**HTTP 长轮询，不依赖桌面微信**。

### 前置条件
- 微信 → 设置 → 插件 → 安装 ClawBot
- Python 3.8+, `requests`

### 使用
```bash
python wechat_clawbot_bridge.py              # 首次扫码登录
python wechat_clawbot_bridge.py --reset      # 换号/过期重登
```

### 消息协议
- 发任意消息 → Claude 执行 → 微信回复结果
- 发 `重置` → 清空会话上下文
- Claude 可：执行命令、读写文件、截图(pyautogui)、锁屏、关机

### 技术要点
- API: `https://ilinkai.weixin.qq.com`，`iLink-App-Id: bot`
- 接收: `POST ilink/bot/getupdates` 长轮询（35s），cursor `get_updates_buf` 天然去重
- 发送: `POST ilink/bot/sendmessage` JSON + `context_token` 上下文回传
- 状态: `.clawbot_state.json`（token+cursor），`runtime/clawbot_bridge.pid`
- 日志: `logs/clawbot_bridge.log`

---

## 备用：UIA 桥接 `wechat_bridge.py`

UIAutomation 操控桌面微信 GUI，无需插件但依赖微信窗口。

```bash
python wechat_bridge.py                      # 监控文件传输助手
python wechat_bridge.py "联系人名"
```

### 架构
```
IDLE ──(收到"claude")──→ ACTIVE ──(每条消息)──→ EXECUTE ──→ ACTIVE
```

### 五层去重
发送缓存(3s MD5) → 指纹比对(含时间戳) → 前缀匹配(30字符) → 命令哈希(30s) → 冷却期(5s)

### 关键 Win32 经验
- **剪贴板文本**: `CF_UNICODETEXT` + `GlobalAlloc`（64位必须声明 `restype=c_void_p`，否则句柄溢出）
- **剪贴板文件**: `CF_HDROP` + `DROPFILES` 结构体，比 PowerShell 快 10 倍
- **微信 UI**: `AutomationId="session_item_{name}"`, 输入框 `chat_input_field`
- **坑**: Ctrl+V 可能触发微信"粘贴即发送"，需读输入框是否已空

### 本地指令调度
`dispatch_local()` 拦截纯本地操作：酷狗播放、发文件/图片

---

## 项目文件

| 文件 | 说明 |
|------|------|
| `wechat_clawbot_bridge.py` | ClawBot 桥接主程序（推荐） |
| `wechat_bridge.py` | UIA 桥接主程序（备用） |
| `kugou_play.py` | 酷狗音乐搜索播放 |
| `kugou_calibrate.py` | 酷狗 UI 校准（F8 记录） |
| `wechat_send.py` | 命令行发消息工具 |
| `wechat_reply.py` | 自动回复工具 |
| `wechat_send_image.py` | 粘贴图片工具 |
| `config.env` | 配置文件（不提交 git） |
| `.clawbot_state.json` | ClawBot 登录凭据（不提交 git） |

## 启动脚本

| 脚本 | 用途 |
|------|------|
| `启动ClawBot桥接.bat` | 一键启动 ClawBot 桥接 |
| `安装ClawBot开机自启.bat` | 安装 ClawBot 桥接开机自启 |
| `启动桥接.bat` | 一键启动 UIA 桥接 |
| `安装开机自启.bat` | 安装 UIA 桥接开机自启 |
| `卸载开机自启.bat` | 移除开机自启 |

## 配置 (`config.env`)

```ini
CTI_WORK_DIR=D:\claude自动
CTI_CLAUDE_CLI=<claude.exe 路径>
CTI_CLAUDE_EFFORT=low
CTI_CLAUDE_TIMEOUT=300
CTI_CLAUDE_PERMISSION_MODE=bypassPermissions
CTI_MAX_RESPONSE_LENGTH=500
```
