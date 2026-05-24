"""
微信 ClawBot 桥接系统 — 基于腾讯 iLink Bot API
通过微信 ClawBot 插件远程控制 Claude Code 执行任务 + 操作电脑。

与旧 wechat_bridge.py 的区别:
  - 无需 UIAutomation 操控桌面微信（不再依赖微信 UI）
  - 使用 HTTP 长轮询接收消息（不再轮询 UI 控件）
  - 服务端 cursor 天然去重（不再需要五层客户端去重）
  - QR 码扫码登录（不依赖桌面微信已登录）
  - 腾讯官方合法通道（有法律条款背书）

前置条件:
  - 微信需安装 ClawBot 插件（微信设置 → 插件 → ClawBot）
  - Python 3.8+, requests 库

用法:
  python wechat_clawbot_bridge.py              # 首次运行扫码登录
  python wechat_clawbot_bridge.py --config config.env
  python wechat_clawbot_bridge.py --reset      # 重新扫码登录
"""

import sys
import os
import io
import time
import json
import re
import hashlib
import base64
import struct
import subprocess
import logging
import logging.handlers
import argparse
import threading
import queue
from pathlib import Path

# ---------- 第三方依赖检查 ----------
try:
    import requests
except ImportError:
    print("请先安装: pip install requests")
    sys.exit(1)

# 可选依赖
try:
    import pyautogui as _pyautogui_available
    PYAUTOGUI = True
except ImportError:
    PYAUTOGUI = False

# ---------- 编码修复 ----------
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ---------- 常量 ----------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = 0x020404  # 2.4.4 → major<<16 | minor<<8 | patch
ILINK_BOT_TYPE = "3"
DEFAULT_BOT_AGENT = "ClaudeCode/1.0"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
SESSION_EXPIRED_ERRCODE = -14
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clawbot_state.json")

# ---------- 配置加载 ----------
DEFAULT_CONFIG = {
    'CTI_WORK_DIR': os.path.dirname(os.path.abspath(__file__)),
    'CTI_CLAUDE_CLI': r"C:\Users\zhx\Desktop\CC\nodejs\node-v20.11.0-win-x64\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
    'CTI_LOG_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'),
    'CTI_RUNTIME_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime'),
    'CTI_MAX_RESPONSE_LENGTH': '500',
    'CTI_CLAUDE_EFFORT': 'low',
    'CTI_CLAUDE_TIMEOUT': '300',
    'CTI_CLAUDE_PERMISSION_MODE': 'bypassPermissions',
    'CTI_CHECK_INTERVAL': '0.5',
}


def parse_env_file(filepath):
    result = {}
    if not os.path.isfile(filepath):
        return result
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                result[key] = value
    return result


def load_config(config_path=None):
    config = dict(DEFAULT_CONFIG)
    if config_path is None:
        search_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.env'),
            os.path.join(os.path.expanduser('~'), '.wechat-bridge', 'config.env'),
        ]
        for p in search_paths:
            if os.path.isfile(p):
                config_path = p
                break
    if config_path and os.path.isfile(config_path):
        config.update(parse_env_file(config_path))
    for key in config:
        env_val = os.environ.get(key)
        if env_val is not None:
            config[key] = env_val
    return config


# ---------- 日志 ----------
def setup_logging(log_dir, level=logging.INFO):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'clawbot_bridge.log')
    logger = logging.getLogger('clawbot_bridge')
    logger.setLevel(level)
    logger.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding='utf-8'
    )
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(ch)
    return logger


log = logging.getLogger('clawbot_bridge')


# ---------- 状态持久化 ----------
def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or '.', exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- iLink API 客户端 ----------
def random_wechat_uin():
    u32 = struct.unpack('>I', os.urandom(4))[0]
    return base64.b64encode(str(u32).encode()).decode()


def build_headers(token=None):
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_post(endpoint, body=None, token=None, timeout=15):
    url = f"{ILINK_BASE_URL}/{endpoint}"
    headers = build_headers(token)
    log.debug(f"POST {endpoint} body={json.dumps(body or {}, ensure_ascii=False)[:200]}")
    try:
        resp = requests.post(url, json=body or {}, headers=headers, timeout=timeout)
        if not resp.ok:
            log.error(f"{endpoint} HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.text else {}
    except requests.Timeout:
        log.debug(f"{endpoint}: timeout after {timeout}s")
        return {}
    except Exception as e:
        log.error(f"{endpoint}: {e}")
        return {}


def api_get(endpoint, timeout=35):
    url = f"{ILINK_BASE_URL}/{endpoint}"
    headers = build_headers()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        return resp.json() if resp.text else {}
    except requests.Timeout:
        return {"status": "wait"}
    except Exception as e:
        log.warn(f"GET {endpoint}: {e}")
        return {"status": "wait"}


# ---------- QR 码登录 ----------
def get_qr_code(token_list=None):
    """获取登录二维码，返回 {qrcode, qrcode_img_content}"""
    body = {"local_token_list": token_list or []}
    return api_post("ilink/bot/get_bot_qrcode?bot_type=" + ILINK_BOT_TYPE, body)


def poll_qr_status(qrcode, verify_code=None):
    """长轮询扫码状态"""
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={qrcode}"
    if verify_code:
        endpoint += f"&verify_code={verify_code}"
    return api_get(endpoint, timeout=35)


def display_qr_terminal(qrcode_url):
    """在终端显示二维码和备用链接"""
    try:
        import qrcode as _qr
        qr = _qr.QRCode()
        qr.add_data(qrcode_url)
        qr.print_ascii()
    except ImportError:
        pass
    print(f"\n若二维码无法显示，请访问以下链接:\n{qrcode_url}\n")


def do_login():
    """执行 QR 码登录流程，返回 {bot_token, ilink_bot_id, ilink_user_id, baseurl}"""
    # 尝试复用已有 token
    state = load_state()
    known_tokens = []
    for account_id, data in state.get("accounts", {}).items():
        if data.get("token"):
            known_tokens.append(data["token"])

    print("正在获取登录二维码...")
    result = get_qr_code(known_tokens[-10:] if known_tokens else [])
    qrcode = result.get("qrcode")
    qrcode_url = result.get("qrcode_img_content")

    if not qrcode or not qrcode_url:
        print(f"获取二维码失败: {result}")
        return None

    print("\n" + "=" * 50)
    print("请用手机微信扫描以下二维码登录:")
    print("=" * 50)
    display_qr_terminal(qrcode_url)

    print("等待扫码...")
    scanned_printed = False
    refresh_count = 0
    pending_verify = None

    while True:
        status = poll_qr_status(qrcode, pending_verify)
        st = status.get("status", "wait")
        log.debug(f"QR status: {st}")

        if st == "wait":
            if not scanned_printed:
                print(".", end="", flush=True)
        elif st == "scaned":
            pending_verify = None
            if not scanned_printed:
                print("\n正在验证...")
                scanned_printed = True
        elif st == "need_verifycode":
            code = input("\n请输入手机微信显示的数字: ").strip()
            pending_verify = code
            continue
        elif st == "expired":
            refresh_count += 1
            if refresh_count > 3:
                print("\n二维码多次过期，请重试")
                return None
            print(f"\n二维码已过期，正在刷新 ({refresh_count}/3)...")
            result = get_qr_code(known_tokens[-10:] if known_tokens else [])
            qrcode = result.get("qrcode")
            qrcode_url = result.get("qrcode_img_content")
            if qrcode and qrcode_url:
                display_qr_terminal(qrcode_url)
                scanned_printed = False
                print("请重新扫描...")
        elif st == "verify_code_blocked":
            print("\n多次输入错误，请稍后再试")
            refresh_count += 1
            if refresh_count > 3:
                return None
        elif st == "binded_redirect":
            print("\n已连接过此 ClawBot，无需重复连接")
            return {"already_connected": True}
        elif st == "confirmed":
            print("\n✅ 登录成功！")
            return {
                "bot_token": status.get("bot_token"),
                "ilink_bot_id": status.get("ilink_bot_id"),
                "ilink_user_id": status.get("ilink_user_id"),
                "baseurl": status.get("baseurl") or ILINK_BASE_URL,
            }

        time.sleep(1)


# ---------- 消息收发 ----------
def get_updates(token, get_updates_buf="", timeout_ms=DEFAULT_LONG_POLL_TIMEOUT_MS):
    body = {
        "get_updates_buf": get_updates_buf,
        "base_info": {
            "channel_version": "2.4.4",
            "bot_agent": DEFAULT_BOT_AGENT,
        }
    }
    return api_post("ilink/bot/getupdates", body, token=token, timeout=max(timeout_ms // 1000, 10))


def send_message(token, to_user_id, text, context_token=None):
    """发送文本消息"""
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"clawbot-{os.urandom(8).hex()}",
            "message_type": 2,  # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context_token or "",
        },
        "base_info": {
            "channel_version": "2.4.4",
            "bot_agent": DEFAULT_BOT_AGENT,
        }
    }
    return api_post("ilink/bot/sendmessage", body, token=token)


def send_typing(token, ilink_user_id, typing_ticket, status=1):
    """发送'正在输入'状态 (1=typing, 2=cancel)"""
    body = {
        "ilink_user_id": ilink_user_id,
        "typing_ticket": typing_ticket,
        "status": status,
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    return api_post("ilink/bot/sendtyping", body, token=token, timeout=10)


def get_config(token, ilink_user_id, context_token=None):
    body = {
        "ilink_user_id": ilink_user_id,
        "context_token": context_token or "",
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    return api_post("ilink/bot/getconfig", body, token=token, timeout=10)


def notify_start(token):
    body = {"base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT}}
    return api_post("ilink/bot/msg/notifystart", body, token=token, timeout=10)


def notify_stop(token):
    body = {"base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT}}
    return api_post("ilink/bot/msg/notifystop", body, token=token, timeout=10)


# ---------- 消息解析 ----------
def extract_text(item_list):
    """从 item_list 中提取纯文本"""
    if not item_list:
        return ""
    for item in item_list:
        if item.get("type") == 1:  # TEXT
            return item.get("text_item", {}).get("text", "")
        if item.get("type") == 3:  # VOICE (含语音转文字)
            text = item.get("voice_item", {}).get("text", "")
            if text:
                return f"[语音] {text}"
        if item.get("type") == 2:  # IMAGE
            return "[图片]"
        if item.get("type") == 5:  # VIDEO
            return "[视频]"
        if item.get("type") == 4:  # FILE
            return f"[文件] {item.get('file_item', {}).get('file_name', '')}"
    return ""


# ---------- 命令解析 ----------
CLAUDE_PATTERN = re.compile(r'(?i)^\s*claude\b')
STOP_PATTERN = re.compile(r'(?i)\bstop\s*$')


def is_claude_start(text):
    return bool(CLAUDE_PATTERN.match(text))


def is_claude_stop(text):
    return bool(STOP_PATTERN.search(text))


def extract_command(text):
    text = CLAUDE_PATTERN.sub('', text, count=1).strip()
    text = STOP_PATTERN.sub('', text, count=1).strip()
    return text


# ---------- 本地指令调度 ----------
def dispatch_local(cmd, work_dir):
    """本地快捷操作（秒级响应，不走 Claude），返回 (handled, result)"""
    cmd_lower = cmd.lower().strip()

    # 酷狗音乐播放
    if ('酷狗' in cmd_lower or 'kugou' in cmd_lower) and '播放' in cmd_lower:
        MIXED_OPS = ['然后', '接着', '截图', '发给', '发送', '发图片', '并且', '还有', '以及', '之后', '完了', '后再']
        if any(kw in cmd for kw in MIXED_OPS):
            return False, None

        m = re.search(r'播放\s*(.+?)(?:\s*stop)?\s*$', cmd, re.IGNORECASE)
        if m:
            song = m.group(1).strip()
            song = re.sub(r'[歌曲音乐]', '', song).strip()
        else:
            song = cmd.split('播放')[-1].strip()

        if song:
            try:
                kugou_path = os.path.join(work_dir, 'kugou_play.py')
                r = subprocess.run(
                    ['python', '-u', kugou_path, song],
                    capture_output=True, text=True, timeout=30,
                    cwd=work_dir, encoding='utf-8', errors='replace',
                    creationflags=0x08000000,
                )
                return True, f"酷狗: {song}\n{r.stdout.strip()[-300:] if r.stdout else '(无输出)'}"
            except Exception as e:
                return True, f"酷狗播放失败: {e}"

    return False, None


# ---------- Claude 调用 ----------
_session_started = False
_prime_proc = None
_my_claude_pids = []


def track_claude_pid(pid):
    if pid and pid not in _my_claude_pids:
        _my_claude_pids.append(pid)


def untrack_claude_pid(pid):
    if pid in _my_claude_pids:
        _my_claude_pids.remove(pid)


def kill_my_claude():
    global _prime_proc
    if _prime_proc:
        try:
            _prime_proc.kill()
        except:
            pass
        _prime_proc = None
    for pid in list(_my_claude_pids):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0001, False, pid)
            if h:
                kernel32.TerminateProcess(h, 0)
                kernel32.CloseHandle(h)
        except:
            pass
    _my_claude_pids.clear()


def reset_session():
    global _session_started, _prime_proc
    _session_started = False
    if _prime_proc:
        try:
            _prime_proc.kill()
        except:
            pass
        untrack_claude_pid(_prime_proc.pid)
        _prime_proc = None


def call_claude(prompt, config):
    """调用 Claude Code CLI，自动保持会话连贯"""
    global _session_started, _prime_proc

    cli = config.get('CTI_CLAUDE_CLI', '')
    work_dir = config.get('CTI_WORK_DIR', os.path.dirname(os.path.abspath(__file__)))
    effort = config.get('CTI_CLAUDE_EFFORT', 'low')
    perm = config.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions')
    timeout_s = int(config.get('CTI_CLAUDE_TIMEOUT', '300'))
    max_len = int(config.get('CTI_MAX_RESPONSE_LENGTH', '500'))

    # 处理预热进程
    if _prime_proc is not None:
        if _prime_proc.poll() is None:
            try:
                _prime_proc.kill()
            except:
                pass
            untrack_claude_pid(_prime_proc.pid)
        else:
            _session_started = True
            untrack_claude_pid(_prime_proc.pid)
        _prime_proc = None

    # 文件快照
    existing_files = set()
    try:
        for f in os.listdir(work_dir):
            existing_files.add(f)
    except:
        pass

    try:
        if _session_started:
            args = [cli, '--continue', '-p', prompt, '--permission-mode', perm, '--effort', effort]
        else:
            args = [cli, '-p', prompt, '--permission-mode', perm, '--effort', effort]
            _session_started = True

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6  # SW_MINIMIZE

        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            cwd=work_dir,
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
            startupinfo=startupinfo,
        )
        track_claude_pid(proc.pid)

        try:
            stdout, stderr_text = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            untrack_claude_pid(proc.pid)
            return "(Claude 执行超时)"

        untrack_claude_pid(proc.pid)

        output = stdout.strip()
        if stderr_text:
            stderr_short = stderr_text.strip()[:200]
            if stderr_short:
                output += f"\n[stderr: {stderr_short}]"
        if not output:
            output = f"(无输出, exit={proc.returncode})"

        # 检测新文件
        try:
            for f in os.listdir(work_dir):
                if f not in existing_files and os.path.isfile(os.path.join(work_dir, f)):
                    output += f"\n__FILE__:{os.path.join(work_dir, f)}"
        except:
            pass

        return output[:max_len + 500]
    except FileNotFoundError:
        return f"(未找到 Claude CLI: {cli})"
    except Exception as e:
        return f"(调用失败: {e})"


# ---------- Claude 系统提示词 ----------
SYSTEM_PROMPT = """你是通过微信 ClawBot 连接的用户电脑上的 AI 助手。你有以下能力：

## 电脑操控
- **执行命令**: 用 Bash 工具运行 Windows 命令（dir, type, tasklist, reg query, ...）
- **文件操作**: 读写/搜索项目文件（Read, Write, Edit, Glob, Grep）
- **Python 脚本**: 编写运行 Python 脚本自动化任务
- **截图**: pyautogui.screenshot() 截图保存为文件
- **窗口管理**: PowerShell 操控 Windows 窗口

## 微信特殊功能
- 用户说"截图"/"截屏"→ 用以下代码截图:
```python
import pyautogui, time
time.sleep(0.5)
path = 'D:/claude自动/_screenshot.png'
pyautogui.screenshot(path)
print(f"截图已保存: {path}")
```
- 用户说"发文件XXX"→ 创建/找到文件后直接给出路径，系统自动发送
- 用户说"放歌XXX"/"播放XXX"→ 走酷狗本地播放
- 用户说"锁屏"→ `rundll32.exe user32.dll,LockWorkStation`
- 用户说"关机"→ `shutdown /s /t 60`（60秒延迟，可取消）
- 用户说"取消关机"→ `shutdown /a`

## 规则
- 回复简洁直接（微信消息有长度限制）
- 文件保存到 D:\\claude自动 目录
- 创建的新文件会自动发送给用户
- 优先用本地工具，不需要联网查的就别查
- 用中文回复
"""


# ---------- 主循环 ----------
def run_bridge(config, reset_login=False):
    global _session_started

    work_dir = config.get('CTI_WORK_DIR', os.path.dirname(os.path.abspath(__file__)))
    log_dir = config.get('CTI_LOG_DIR', os.path.join(work_dir, 'logs'))
    runtime_dir = config.get('CTI_RUNTIME_DIR', os.path.join(work_dir, 'runtime'))
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)

    # 日志和 API URL 设置到模块级
    global log, ILINK_BASE_URL
    log = setup_logging(log_dir)

    # 加载/获取登录态
    state = load_state()
    account_id = state.get("default_account", "")
    accounts = state.get("accounts", {})

    if reset_login or not account_id or account_id not in accounts:
        print("需要扫码登录...")
        login_result = do_login()
        if not login_result:
            print("登录失败")
            return
        if login_result.get("already_connected"):
            print("已连接，使用现有凭据")
        else:
            account_id = login_result.get("ilink_bot_id", "default")
            accounts[account_id] = {
                "token": login_result["bot_token"],
                "baseurl": login_result.get("baseurl", ILINK_BASE_URL),
                "ilink_user_id": login_result.get("ilink_user_id", ""),
                "get_updates_buf": "",
                "typing_ticket": "",
            }
            state["default_account"] = account_id
            state["accounts"] = accounts
            save_state(state)

    account = accounts.get(account_id, {})
    token = account.get("token", "")
    base_url = account.get("baseurl", ILINK_BASE_URL)
    ilink_user_id = account.get("ilink_user_id", "")
    get_updates_buf = account.get("get_updates_buf", "")
    typing_ticket = account.get("typing_ticket", "")

    if not token:
        print("未找到登录凭据，请用 --reset 重新登录")
        return

    # 更新 base URL（可能因 IDC 重定向变化）
    ILINK_BASE_URL = base_url

    # 通知服务端启动
    try:
        notify_start(token)
    except Exception as e:
        log.warn(f"notifyStart failed (ignored): {e}")

    # 获取 typing_ticket
    if not typing_ticket and ilink_user_id:
        try:
            cfg = get_config(token, ilink_user_id)
            typing_ticket = cfg.get("typing_ticket", "")
            if typing_ticket:
                account["typing_ticket"] = typing_ticket
                accounts[account_id] = account
                state["accounts"] = accounts
                save_state(state)
        except:
            pass

    print("\n" + "=" * 60)
    print("WeChat ClawBot <-> Claude 桥接系统")
    print(f"  API: {ILINK_BASE_URL}")
    print(f"  账号: {account_id}")
    print(f"  用户: {ilink_user_id}")
    print(f"  工作目录: {work_dir}")
    print(f"  日志目录: {log_dir}")
    print()
    print("运行模式:")
    print("  - 直接发消息 → Claude 执行并回复")
    print("  - 发 '重置' 清空会话上下文")
    print("按 Ctrl+C 停止")
    print("=" * 60)
    print()

    # 状态变量
    conv_mode = False
    cooldown_until = 0
    recent_hashes = {}
    next_timeout_ms = DEFAULT_LONG_POLL_TIMEOUT_MS
    consecutive_failures = 0
    abort_flag = threading.Event()

    # 写运行时状态
    status_path = os.path.join(runtime_dir, 'status.json')
    def write_status(running=True, **extra):
        try:
            s = {"running": running, "account_id": account_id, "ilink_user_id": ilink_user_id,
                 "conv_mode": conv_mode, "timestamp": time.time(), **extra}
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False)
        except:
            pass

    write_status()

    print("正在启动消息监听...\n")

    try:
        while not abort_flag.is_set():
            try:
                log.debug(f"getUpdates: buf_len={len(get_updates_buf)}, timeout_ms={next_timeout_ms}")
                resp = get_updates(token, get_updates_buf, next_timeout_ms)

                # 处理长轮询超时（正常）
                if resp.get("ret", 0) != 0:
                    errcode = resp.get("errcode", 0)
                    if errcode == SESSION_EXPIRED_ERRCODE:
                        log.error("会话过期(errcode=-14)，暂停60分钟后重试")
                        print("\n⚠ 会话过期，请重新扫码登录 (python wechat_clawbot_bridge.py --reset)")
                        abort_flag.set()
                        break
                    consecutive_failures += 1
                    log.warn(f"getUpdates errcode={errcode} ({consecutive_failures}/3)")
                    if consecutive_failures >= 3:
                        log.error("连续3次失败，等待30秒后重试")
                        time.sleep(30)
                        consecutive_failures = 0
                    else:
                        time.sleep(2)
                    continue

                consecutive_failures = 0

                # 更新服务器建议的轮询间隔
                if resp.get("longpolling_timeout_ms"):
                    next_timeout_ms = resp["longpolling_timeout_ms"]

                # 保存 cursor
                new_buf = resp.get("get_updates_buf", "")
                if new_buf and new_buf != get_updates_buf:
                    get_updates_buf = new_buf
                    account["get_updates_buf"] = new_buf
                    accounts[account_id] = account
                    state["accounts"] = accounts
                    save_state(state)

                # 处理消息
                msgs = resp.get("msgs", [])
                for msg in msgs:
                    from_user = msg.get("from_user_id", "")
                    context_token = msg.get("context_token", "")

                    # 更新 ilink_user_id
                    if from_user and from_user != ilink_user_id:
                        ilink_user_id = from_user
                        account["ilink_user_id"] = from_user
                        accounts[account_id] = account
                        state["accounts"] = accounts
                        save_state(state)

                    text = extract_text(msg.get("item_list", []))
                    if not text:
                        continue

                    log.info(f"收到: {text[:100]} from={from_user}")

                    # 冷却期检查
                    now = time.time()
                    if now < cooldown_until:
                        continue

                    # 哈希去重（同一条消息30秒内不处理两次）
                    cmd_hash = hashlib.md5(text.encode()).hexdigest()
                    if cmd_hash in recent_hashes and now < recent_hashes[cmd_hash]:
                        continue

                    # 系统消息过滤
                    if text.startswith("(已") or text.startswith("酷狗:"):
                        continue

                    # 处理特殊命令
                    cmd_lower = text.lower().strip()

                    # 重置会话
                    if cmd_lower in ('重置', '重置会话', '新会话', 'reset', 'new'):
                        reset_session()
                        send_message(token, from_user, "✅ 会话已重置", context_token)
                        cooldown_until = time.time() + 3
                        recent_hashes[cmd_hash] = time.time() + 30
                        continue

                    # 全部消息交给 Claude 处理
                    cmd = text

                    # 标记已处理
                    recent_hashes[cmd_hash] = time.time() + 30
                    cooldown_until = time.time() + 3

                    # 发送"正在输入"
                    if typing_ticket:
                        try:
                            send_typing(token, from_user, typing_ticket, 1)
                        except:
                            pass

                    # 先尝试本地调度
                    handled, result = dispatch_local(cmd, work_dir)
                    if handled:
                        send_message(token, from_user, result, context_token)
                    else:
                        # 调用 Claude
                        print(f"\n🤖 执行: {cmd[:80]}...")
                        full_prompt = SYSTEM_PROMPT + "\n\n---\n用户消息:\n" + cmd
                        response = call_claude(full_prompt, config)

                        # 处理回复中的文件标记
                        files_to_send = []
                        clean_lines = []
                        for line in response.split('\n'):
                            if line.startswith('__FILE__:'):
                                fpath = line[8:].strip()
                                if os.path.isfile(fpath):
                                    files_to_send.append(fpath)
                            else:
                                clean_lines.append(line)
                        clean_text = '\n'.join(clean_lines)

                        # 发送文本回复
                        if clean_text.strip():
                            send_message(token, from_user, clean_text.strip(), context_token)

                        # 发送文件（TODO: 需要通过 CDN 上传）
                        for fp in files_to_send:
                            print(f"  -> 文件待发送: {fp}")
                            # CDN 上传较复杂，先发文件路径告知用户
                            fname = os.path.basename(fp)
                            send_message(token, from_user, f"[文件] {fname}\n(文件位于: {fp})", context_token)

                    # 取消"正在输入"
                    if typing_ticket:
                        try:
                            send_typing(token, from_user, typing_ticket, 2)
                        except:
                            pass

                    write_status(last_message=text[:100])

                # 清理过期哈希
                now = time.time()
                for h in list(recent_hashes):
                    if recent_hashes[h] < now:
                        del recent_hashes[h]

            except requests.ConnectionError as e:
                consecutive_failures += 1
                log.error(f"连接失败 ({consecutive_failures}/3): {e}")
                if consecutive_failures >= 3:
                    time.sleep(30)
                    consecutive_failures = 0
                else:
                    time.sleep(2)

            except Exception as e:
                log.error(f"循环异常: {e}", exc_info=True)
                time.sleep(2)

    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        abort_flag.set()
        write_status(running=False)
        kill_my_claude()
        try:
            notify_stop(token)
        except:
            pass
        print("桥接已停止")


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(
        description="WeChat ClawBot Bridge — 基于腾讯 iLink Bot API 的微信 ↔ Claude 桥接"
    )
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--reset', action='store_true', help='重新扫码登录')
    args = parser.parse_args()

    config = load_config(args.config)

    # 单实例检查
    lock_file = os.path.join(config.get('CTI_RUNTIME_DIR',
        os.path.join(config['CTI_WORK_DIR'], 'runtime')), 'clawbot_bridge.pid')
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)

    if os.path.isfile(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0400, False, old_pid)  # PROCESS_QUERY_INFORMATION
            if h:
                kernel32.CloseHandle(h)
                print(f"桥接已在运行 (PID: {old_pid})")
                print("如需重启，请先终止旧进程")
                sys.exit(1)
        except (ValueError, OSError):
            pass

    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))

    try:
        while True:
            try:
                run_bridge(config, reset_login=args.reset)
                # reset_login 只在第一次有效
                args.reset = False
            except Exception as e:
                log.error(f"桥接崩溃: {e}", exc_info=True)
                print(f"\n桥接异常退出: {e}")
            print("\n3 秒后自动重启...")
            time.sleep(3)
    finally:
        try:
            os.remove(lock_file)
        except:
            pass


if __name__ == '__main__':
    main()
