# Claude 自动 — WeChat ↔ Claude 桥接

## 项目概述

Windows 微信桌面版 → UIAutomation → Claude Code 的桥接系统。通过手机微信远程操控电脑端 Claude 执行任务。

## 核心文件

- `wechat_bridge.py` — 主程序，状态机驱动（IDLE→ACTIVE→EXECUTE→ACTIVE）
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
- `.bridge_lock` — 单实例锁（存 PID）
- `.claude_pids` — Claude 子进程 PID 跟踪文件

## 架构模式

### 状态机
```
IDLE ──(收到"claude")──→ ACTIVE ──(每条消息=指令)──→ EXECUTE ──→ ACTIVE
  ↑                        ↑                              │
  └──(收到"claude"/exit)───┘                              │
        退出指令模式                                        │
```

### 消息去重（五层防护）
1. **发送缓存(3s)** — `send_message` 级 MD5 哈希，完全相同的文本 3 秒内不重复发送
2. **指纹比对** — `get_cell_fingerprint` 取 session cell 完整 Name（含时间戳），Name 不变则跳过
3. **前缀匹配(30字符)** — `last_processed_text` 与 `current` 的前 30 字符比对
4. **命令哈希(30s)** — `recent_hashes` 字典，同一条命令文本 30 秒内不处理第二次
5. **冷却期(5s)** — `cooldown_until`，处理完指令后 5 秒内忽略所有消息

### 本地指令调度
`dispatch_local()` 在调用 Claude 前拦截纯本地操作（秒级响应）：
- **酷狗播放** — 必须为"纯播放"指令，含 `然后/截图/发给` 等混合操作词则跳过交给 Claude
- **发文件/发图片** — 通过 CF_HDROP 剪贴板直接发送

### 进程管理
- **启动自动杀旧** — `acquire_lock()` 用 `TerminateProcess` 杀死旧桥接进程后接管
- **Claude PID 跟踪** — `.claude_pids` 文件记录所有子进程 PID，启动时 `cleanup_stale_claude()` 清理残留
- **预热+复用** — `warmup_claude()` 后台预热，`call_claude()` 检测完成后用 `--continue` 保持上下文

## 技术栈与 Win32 经验

### 剪贴板操作（不用 clip.exe / PowerShell）
- **文本** — Win32 `CF_UNICODETEXT`，`GlobalAlloc` + `wcscpy` + `SetClipboardData`
  - **关键**: 64 位 Windows 必须声明 `GlobalAlloc.restype=c_void_p` 和 `argtypes`，否则句柄当 32 位 int 溢出
- **文件** — Win32 `CF_HDROP`，`DROPFILES` 结构体 + UTF-16 路径 + `SetClipboardData`，比 PowerShell 快 10 倍

### 微信 UIA 规律
- Session list cell: `AutomationId="session_item_{chat_name}"`
- Cell Name 格式: `{聊天名}\n{最新消息}\n{时间}\n[未读数]`
- 输入框: `AutomationId="chat_input_field"`，位于 `mmui::ChatMessagePage` 内
- 图片控件: `ControlTypeName="ImageControl"`
- **坑**: `Ctrl+V` 粘贴可能触发微信"粘贴即发送"，需检测输入框是否已空再决定是否按 Enter

### 常见 Bug 与修复
| 症状 | 根因 | 修复 |
|------|------|------|
| 桥接重复回复同一条消息 | Ctrl+V 触发微信"粘贴即发送" + Enter 又发一次 | 粘贴后读输入框 Name，已空则不按 Enter |
| 多个 Claude 终端窗口 | 预热进程未完成被丢弃但未 kill | call_claude 中 kill 未完成的预热进程 |
| "酷狗播放X然后Y" 全部当歌名 | dispatch_local 正则太贪婪 | 加 MIXED_OPS 关键词检测，混合操作交给 Claude |
| Unicode 特殊字符崩溃 | clip.exe 在中文 Windows 上走 GBK 编码 | 用 Win32 CF_UNICODETEXT 替代 |
| 64 位句柄溢出 | ctypes 默认返回 32 位 int | 声明 restype=c_void_p |
| 旧桥接残留 Claude 进程 | 崩溃后子进程未清理 | .claude_pids 文件 + cleanup_stale_claude |
