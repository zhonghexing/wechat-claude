"""
微信 ↔ Claude 桥接系统
通过微信文件传输助手远程控制 Claude Code 执行任务。

通信协议:
  发送 "claude" 进入指令模式
  之后每条消息作为独立指令执行
  发送 "claude" / "exit" / "退出" 退出指令模式

用法:
  python wechat_bridge.py                          # 默认监控文件传输助手
  python wechat_bridge.py "联系人名"                # 监控指定联系人
"""

import sys
import time
import io
import subprocess
import os
import re
import ctypes
import hashlib
import json
import logging
import logging.handlers
import argparse

try:
    import uiautomation as auto
except ImportError:
    print("请先安装: pip install uiautomation")
    sys.exit(1)

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ---------- Win32 常量 ----------
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
SW_RESTORE = 9
SW_SHOW = 5

# ---------- 配置加载 ----------
DEFAULT_CONFIG = {
    'CTI_CHAT_NAME': '文件传输助手',
    'CTI_WORK_DIR': os.path.dirname(os.path.abspath(__file__)),
    'CTI_CLAUDE_CLI': r"C:\Users\zhx\Desktop\CC\nodejs\node-v20.11.0-win-x64\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
    'CTI_WECHAT_DATA_DIR': r"C:\Users\zhx\xwechat_files\wxid_tam2qey51fy722_9836",
    'CTI_LOG_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'),
    'CTI_RUNTIME_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime'),
    'CTI_CHECK_INTERVAL': '0.3',
    'CTI_MAX_RESPONSE_LENGTH': '500',
    'CTI_CLAUDE_EFFORT': 'low',
    'CTI_CLAUDE_TIMEOUT': '300',
    'CTI_CLAUDE_PERMISSION_MODE': 'bypassPermissions',
}


def parse_env_file(filepath):
    """解析 .env 格式文件，返回 dict"""
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
    """加载配置：环境变量 > config.env 文件 > 默认值"""
    config = dict(DEFAULT_CONFIG)

    # 自动查找 config.env
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
        file_config = parse_env_file(config_path)
        config.update(file_config)

    # 环境变量覆盖
    for key in config:
        env_val = os.environ.get(key)
        if env_val is not None:
            config[key] = env_val

    return config


def setup_logging(log_dir, level=logging.INFO):
    """配置双输出日志：控制台 + 文件（自动轮转，最多 5 个 × 1MB）"""
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'bridge.log')

    # 根 logger
    logger = logging.getLogger('wechat_bridge')
    logger.setLevel(level)
    logger.handlers.clear()

    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)

    # 文件轮转
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5,
                                               encoding='utf-8')
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)

    return logger


def write_runtime_status(runtime_dir, **kwargs):
    """写入运行时状态 JSON"""
    os.makedirs(runtime_dir, exist_ok=True)
    status = {
        'pid': os.getpid(),
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    status.update(kwargs)
    path = os.path.join(runtime_dir, 'status.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except:
        pass

# 消息类型
MSG_TEXT = 'text'
MSG_IMAGE = 'image'
MSG_FILE = 'file'

# ---------- 状态机 ----------
STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_EXECUTE = "execute"

# ---------- 单实例锁 + 旧进程清理 ----------

# 运行时全局变量（由 run_bridge 初始化）
_G = {}

# 当前桥接实例启动后才创建的 Claude 子进程 PID（用于正常退出时精确清理）
_my_claude_pids = []


def c(path_key):
    """从 _G 配置中获取路径"""
    return _G.get(path_key, '')


def cleanup_stale_claude():
    """启动时：杀死上次桥接残留的所有 Claude 子进程"""
    pid_file = c('CLAUDE_PID_FILE')
    if not pid_file or not os.path.exists(pid_file):
        return
    killed = 0
    try:
        with open(pid_file, 'r') as f:
            pids = [line.strip() for line in f if line.strip()]
        kernel32 = ctypes.windll.kernel32
        for pid_str in pids:
            try:
                pid = int(pid_str)
                h = kernel32.OpenProcess(0x0001, False, pid)
                if h:
                    kernel32.TerminateProcess(h, 0)
                    kernel32.CloseHandle(h)
                    killed += 1
            except:
                pass
    except:
        pass
    try:
        os.remove(pid_file)
    except:
        pass
    if killed:
        print(f"已清理 {killed} 个残留 Claude 子进程")


def track_claude_pid(pid):
    """记录 Claude 子进程 PID，以便异常退出后清理"""
    _my_claude_pids.append(pid)
    pid_file = c('CLAUDE_PID_FILE')
    if pid_file:
        try:
            with open(pid_file, 'a') as f:
                f.write(f"{pid}\n")
        except:
            pass


def untrack_claude_pid(pid):
    """Claude 正常退出后移除 PID 记录"""
    if pid in _my_claude_pids:
        _my_claude_pids.remove(pid)


def kill_my_claude():
    """退出时：清理当前桥接实例创建的所有 Claude 子进程"""
    for pid in _my_claude_pids:
        try:
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0001, False, pid)
            if h:
                kernel32.TerminateProcess(h, 0)
                kernel32.CloseHandle(h)
        except:
            pass
    _my_claude_pids.clear()
    pid_file = c('CLAUDE_PID_FILE')
    if pid_file:
        try:
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except:
            pass


def acquire_lock():
    """获取单实例锁，如发现旧桥接进程则自动杀死并接管"""
    lock_file = c('LOCK_FILE')
    if not lock_file:
        return True
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0001, False, old_pid)
            if h:
                kernel32.TerminateProcess(h, 0)
                kernel32.CloseHandle(h)
                print(f"已终止旧桥接进程 (PID: {old_pid})")
                time.sleep(0.5)
        except:
            pass
        try:
            os.remove(lock_file)
        except:
            pass
    cleanup_stale_claude()
    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    kill_my_claude()
    lock_file = c('LOCK_FILE')
    if lock_file:
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except:
            pass


# ---------- 窗口管理 ----------

def ensure_window_visible(control):
    try:
        hwnd = control.NativeWindowHandle
        if not hwnd:
            return False
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.05)
        if ctypes.windll.user32.GetForegroundWindow() != hwnd:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
            time.sleep(0.01)
            ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.01)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
        time.sleep(0.03)
        return True
    except:
        return False


def ensure_wechat_ready(wechat):
    if not wechat or not wechat.Exists(0, 0.2):
        return False
    try:
        ensure_window_visible(wechat)
        wechat.SetActive()
        time.sleep(0.05)
        return True
    except:
        return False


# ---------- UIA 工具 ----------

def find_child_by_autoid(parent, auto_id):
    try:
        for child in parent.GetChildren():
            if child.AutomationId == auto_id:
                return child
            result = find_child_by_autoid(child, auto_id)
            if result:
                return result
    except:
        pass
    return None


def find_child_by_class(parent, class_name):
    try:
        for child in parent.GetChildren():
            if child.ClassName == class_name:
                return child
            result = find_child_by_class(child, class_name)
            if result:
                return result
    except:
        pass
    return None


# ---------- 微信操作 ----------

def get_wechat():
    for name in ['微信', 'Weixin', 'WeChat']:
        w = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
        if w.Exists(0, 0.5):
            return w
    root = auto.GetRootControl()
    for child in root.GetChildren():
        if child.ClassName == 'mmui::MainWindow' and child.Name in ('微信', 'Weixin', 'WeChat'):
            return child
    return None


def get_last_message_text(wechat, chat_name):
    import re as _re
    info = get_chat_cell_info(wechat, chat_name)
    if info:
        parts = info['name'].split('\n')
        for part in parts[1:]:
            part = part.strip()
            if not part:
                continue
            if _re.match(r'^\[\d+条\]$', part):
                continue
            if _re.match(r'^\d{1,2}:\d{2}$', part):
                continue
            return part
    return None


def get_cell_fingerprint(wechat, chat_name):
    """获取会话 cell 的完整指纹（含时间戳），用于可靠的新消息检测。

    不能只用消息正文对比，因为用户可能连续发送相同内容（如多次 "claude"），
    正文相同但时间戳不同，用完整 Name 才能区分。
    """
    info = get_chat_cell_info(wechat, chat_name)
    if info:
        return info['name']
    return None


def get_message_type(wechat, chat_name):
    """检测最新消息的类型: MSG_TEXT / MSG_IMAGE / MSG_FILE，返回 (text, type)"""
    text = get_last_message_text(wechat, chat_name)
    if not text:
        return None, None
    if text == '[图片]':
        return text, MSG_IMAGE
    if text == '[文件]':
        return text, MSG_FILE
    return text, MSG_TEXT


def get_chat_cell_info(wechat, chat_name):
    session_list = wechat.Control(AutomationId='session_list')
    if not session_list.Exists(0, 0.3):
        return None
    cell = find_child_by_autoid(session_list, f"session_item_{chat_name}")
    if cell:
        try:
            return {'name': cell.Name, 'rect': cell.BoundingRectangle}
        except:
            pass
    return None


def is_chat_open(wechat, chat_name):
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.2):
        return False
    input_field = find_child_by_autoid(detail, 'chat_input_field')
    if input_field and chat_name in (input_field.Name or ''):
        return True
    return False


def open_chat(wechat, chat_name):
    ensure_wechat_ready(wechat)

    # 关闭独立窗口
    standalone = auto.WindowControl(Name=chat_name)
    if standalone.Exists(0, 0.3):
        if 'mmui' in (standalone.ClassName or '') or 'QWidget' in (standalone.ClassName or ''):
            try:
                close_btn = standalone.Control(Name='关闭')
                if close_btn.Exists(0, 0.3):
                    close_btn.Click()
                    time.sleep(0.1)
            except:
                pass

    if is_chat_open(wechat, chat_name):
        return True

    chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
    if chat_tab.Exists(0, 0.2):
        chat_tab.Click()
        time.sleep(0.08)

    if is_chat_open(wechat, chat_name):
        return True

    session_list = wechat.Control(AutomationId='session_list')
    if session_list.Exists(0, 0.3):
        cell = find_child_by_autoid(session_list, f"session_item_{chat_name}")
        if cell:
            cell.Click()
            time.sleep(0.5)
            if not is_chat_open(wechat, chat_name):
                standalone = auto.WindowControl(Name=chat_name)
                if standalone.Exists(0, 0.3):
                    close_btn = standalone.Control(Name='关闭')
                    if close_btn.Exists(0, 0.3):
                        close_btn.Click()
                    time.sleep(0.3)
                    ensure_wechat_ready(wechat)
                    cell = wechat.Control(AutomationId=f"session_item_{chat_name}")
                    if cell.Exists(0, 1):
                        cell.Click()
                        time.sleep(0.8)
            return is_chat_open(wechat, chat_name)
    return False


def _set_clipboard_unicode(text):
    """用 Win32 CF_UNICODETEXT 写剪贴板，避免 clip.exe 的 GBK 编码问题"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 64 位 Windows 上必须声明 restype/argtypes，否则句柄当 32 位 int 会溢出
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    size = (len(text) + 1) * 2
    hMem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    ptr = kernel32.GlobalLock(hMem)
    ctypes.cdll.msvcrt.wcscpy(ctypes.c_void_p(ptr), text)
    kernel32.GlobalUnlock(hMem)
    user32.SetClipboardData(CF_UNICODETEXT, hMem)
    user32.CloseClipboard()


def capture_images_from_chat(wechat):
    """在聊天窗口中找到最新图片并截图保存，返回路径列表"""
    import pyautogui as _pg
    if not ensure_wechat_ready(wechat):
        return []
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.3):
        return []
    # 滚动到底部
    auto.SendKeys('{End}')
    time.sleep(0.3)
    # 递归查找 ImageControl
    all_images = []
    def _collect(ctrl, depth=0):
        if depth > 25:
            return
        try:
            if ctrl.ControlTypeName == 'ImageControl':
                rect = ctrl.BoundingRectangle
                w, h = rect.width(), rect.height()
                if 30 < w < 2000 and 30 < h < 2000:
                    all_images.append(ctrl)
            for child in ctrl.GetChildren():
                _collect(child, depth + 1)
        except:
            pass
    _collect(detail)
    if not all_images:
        return []
    recent = all_images[-3:]  # 最多 3 张
    saved = []
    ts = int(time.time())
    for i, ctrl in enumerate(recent):
        rect = ctrl.BoundingRectangle
        try:
            img = _pg.screenshot(region=(rect.left, rect.top,
                                         max(rect.width(), 1), max(rect.height(), 1)))
            path = os.path.join(c('WORK_DIR'), f"_wechat_img_{ts}_{i}.png")
            img.save(path)
            saved.append(path)
        except Exception as _e:
            print(f"  [WARN] 截图失败: {_e}")
    return saved


def find_latest_files(n=3):
    """查找微信文件存储目录中最新的 n 个文件"""
    month = time.strftime('%Y-%m')
    file_dir = os.path.join(c('WECHAT_FILE_DIR'), month)
    if not os.path.isdir(file_dir):
        return []
    files = []
    for f in os.listdir(file_dir):
        fp = os.path.join(file_dir, f)
        if os.path.isfile(fp):
            files.append((os.path.getmtime(fp), fp))
    files.sort(key=lambda x: x[0], reverse=True)
    return [f[1] for f in files[:n]]


def _set_clipboard_file(filepath):
    """用 Win32 CF_HDROP 写剪贴板，比 PowerShell 快 10 倍（省掉进程启动）"""
    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002

    class DROPFILES(ctypes.Structure):
        _fields_ = [("pFiles", ctypes.c_uint),
                     ("pt", ctypes.c_long * 2),
                     ("fNC", ctypes.c_int),
                     ("fWide", ctypes.c_int)]

    abs_path = os.path.abspath(filepath)
    filedata = (abs_path + '\0\0').encode('utf-16-le')
    df_size = ctypes.sizeof(DROPFILES)
    total = df_size + len(filedata)

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]

    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    hMem = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
    ptr = kernel32.GlobalLock(hMem)
    df = DROPFILES()
    df.pFiles = df_size
    df.fWide = 1
    ctypes.memmove(ptr, ctypes.addressof(df), df_size)
    ctypes.memmove(ptr + df_size, filedata, len(filedata))
    kernel32.GlobalUnlock(hMem)
    user32.SetClipboardData(CF_HDROP, hMem)
    user32.CloseClipboard()


def send_file_to_wechat(wechat, filepath):
    """通过剪贴板发送文件到微信聊天（Win32 CF_HDROP，无 PowerShell 启动延迟）"""
    if not ensure_wechat_ready(wechat):
        return False
    _set_clipboard_file(filepath)
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.3):
        return False
    chat_page = find_child_by_class(detail, 'mmui::ChatMessagePage')
    if not chat_page:
        return False
    inp = find_child_by_autoid(chat_page, 'chat_input_field')
    if not inp:
        return False
    try:
        inp.Click()
    except:
        r = inp.BoundingRectangle
        auto.Click(r.left + 50, r.top + 10)
    time.sleep(0.05)
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.1)
    auto.SendKeys('{Enter}')
    return True
_sent_cache = {}  # msg_hash → expire_time
_SENT_CACHE_TTL = 3


def send_message(wechat, message):
    # 3 秒内完全相同的文本不重复发送（终极去重）
    msg_hash = hashlib.md5(message.encode()).hexdigest()
    now = time.time()
    if msg_hash in _sent_cache and now < _sent_cache[msg_hash]:
        return False
    _sent_cache[msg_hash] = now + _SENT_CACHE_TTL
    # 清理过期条目
    for h in list(_sent_cache):
        if _sent_cache[h] < now:
            del _sent_cache[h]

    if not ensure_wechat_ready(wechat):
        return False
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.3):
        return False
    chat_page = find_child_by_class(detail, 'mmui::ChatMessagePage')
    if not chat_page:
        return False
    input_field = find_child_by_autoid(chat_page, 'chat_input_field')
    if not input_field:
        return False
    try:
        input_field.Click()
    except:
        rect = input_field.BoundingRectangle
        auto.Click(rect.left + 50, rect.top + 10)
    time.sleep(0.05)

    # 用 Win32 CF_UNICODETEXT 写剪贴板，支持全部 Unicode 字符
    # 不能用 clip.exe——它在中文 Windows 上是 GBK 编码，遇到 ↔ 等字符会崩溃
    _set_clipboard_unicode(message)
    auto.SendKeys('{Ctrl}a')
    time.sleep(0.02)
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.15)

    # 微信可能"粘贴即发送"，此时输入框已被清空，不需要再按 Enter
    # 否则会发两遍：一遍由粘贴触发，一遍由 Enter 触发
    try:
        remaining = (input_field.Name or '').strip()
    except:
        remaining = '?'
    if remaining and remaining != '?':
        auto.SendKeys('{Enter}')
    return True


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


def is_valid_command(text):
    """确认消息是一条有效的 claude 指令（有 claude 开头 + stop 结尾 + 中间有内容）"""
    if not is_claude_start(text):
        return False, None
    if not is_claude_stop(text):
        return False, None
    cmd = extract_command(text)
    if not cmd:
        return False, None
    return True, cmd


# ---------- 本地指令调度 ----------

def dispatch_local(cmd):
    """
    检查命令是否匹配本地快捷操作（秒级响应，不走 Claude）
    返回 (handled, result_message)
    """
    cmd_lower = cmd.lower().strip()

    # 发送文件/图片
    if cmd_lower.startswith('发文件') or cmd_lower.startswith('发图片') or \
       cmd_lower.startswith('发送文件') or cmd_lower.startswith('发送图片'):
        import re as _re
        m = _re.search(r'^发(?:送)?(?:文件|图片)\s+(.+?)(?:\s*stop)?\s*$', cmd)
        if m:
            fname = m.group(1).strip()
            fpath = os.path.join(c('WORK_DIR'), fname)
            if not os.path.isfile(fpath):
                fpath = fname
            if os.path.isfile(fpath):
                return True, ('__SEND_FILE__', fpath)
            else:
                return True, f"文件不存在: {fname}"

    # 酷狗音乐播放 — 仅处理"纯播放"指令，混合其他操作的交给 Claude 完整理解
    if ('酷狗' in cmd_lower or 'kugou' in cmd_lower) and '播放' in cmd_lower:
        # 如果命令同时包含截图/发送/后续操作等，跳过本地处理，交给 Claude
        MIXED_OPS = ['然后', '接着', '截图', '发给', '发送', '发图片', '并且', '还有', '以及', '之后', '完了', '后再']
        if any(kw in cmd for kw in MIXED_OPS):
            return False, None

        import re as _re
        # 提取歌名: "播放XXX" 或 "播放 XXX"，截到句尾或 stop 前
        m = _re.search(r'播放\s*(.+?)(?:\s*stop)?\s*$', cmd, _re.IGNORECASE)
        if m:
            song = m.group(1).strip()
            song = _re.sub(r'[歌曲音乐]', '', song).strip()
        else:
            song = cmd.split('播放')[-1].strip()

        if song:
            try:
                r = subprocess.run(
                    ['python', '-u', os.path.join(c('WORK_DIR'), 'kugou_play.py'), song],
                    capture_output=True, text=True, timeout=30,
                    cwd=c('WORK_DIR'), encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
                return True, f"酷狗: {song}\n{r.stdout.strip()[-300:] if r.stdout else '(无输出)'}"
            except Exception as e:
                return True, f"酷狗播放失败: {e}"

    return False, None


# ---------- Claude CLI ----------

# 会话状态：记录是否已有活跃会话，支持 --continue 保持上下文连贯
_session_started = False
_prime_proc = None  # 后台预热进程


def reset_session():
    """重置会话，下次调用将开始新会话"""
    global _session_started, _prime_proc
    _session_started = False
    if _prime_proc:
        try:
            _prime_proc.kill()
        except:
            pass
        untrack_claude_pid(_prime_proc.pid)
        _prime_proc = None


def warmup_claude():
    """后台启动 Claude 预热会话，用户打字期间完成冷启动"""
    global _session_started, _prime_proc
    if _prime_proc is not None and _prime_proc.poll() is None:
        return
    try:
        cli = c('CLAUDE_CLI')
        effort = _G.get('CTI_CLAUDE_EFFORT', 'low')
        perm = _G.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions')
        args = [cli, '-p', '回复OK', '--permission-mode', perm, '--effort', effort]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6
        _prime_proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=c('WORK_DIR'), creationflags=CREATE_NEW_CONSOLE,
            startupinfo=startupinfo,
        )
        track_claude_pid(_prime_proc.pid)
    except:
        pass


def call_claude(prompt):
    """后台调用 Claude，自动保持会话连贯，自动检测 Claude 创建的新文件"""
    global _session_started, _prime_proc

    cli = c('CLAUDE_CLI')
    work_dir = c('WORK_DIR')
    effort = _G.get('CTI_CLAUDE_EFFORT', 'low')
    perm = _G.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions')
    timeout_s = int(_G.get('CTI_CLAUDE_TIMEOUT', '300'))
    max_len = int(_G.get('CTI_MAX_RESPONSE_LENGTH', '500'))

    # 检查后台预热：已完成则复用会话，未完成则杀掉
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

    # 记录执行前的文件快照
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
        startupinfo.wShowWindow = 6
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            cwd=work_dir, creationflags=CREATE_NEW_CONSOLE,
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
    except subprocess.TimeoutExpired:
        return "(Claude 执行超时)"
    except FileNotFoundError:
        return f"(未找到 Claude CLI: {cli})"
    except Exception as e:
        return f"(调用失败: {e})"


# ---------- 主循环 ----------

def run_bridge(chat_name=None, config=None):
    global _G

    # 加载配置
    if config is None:
        config = load_config()
    work_dir = config.get('CTI_WORK_DIR', os.path.dirname(os.path.abspath(__file__)))
    runtime_dir = config.get('CTI_RUNTIME_DIR', os.path.join(work_dir, 'runtime'))
    log_dir = config.get('CTI_LOG_DIR', os.path.join(work_dir, 'logs'))
    check_interval = float(config.get('CTI_CHECK_INTERVAL', '0.3'))
    chat_name = chat_name or config.get('CTI_CHAT_NAME', '文件传输助手')
    wechat_data_dir = config.get('CTI_WECHAT_DATA_DIR', '')

    # 设置 _G（供 c() 函数使用）
    _G = {
        'CLAUDE_CLI': config.get('CTI_CLAUDE_CLI', ''),
        'WORK_DIR': work_dir,
        'CTI_CLAUDE_EFFORT': config.get('CTI_CLAUDE_EFFORT', 'low'),
        'CTI_CLAUDE_PERMISSION_MODE': config.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions'),
        'CTI_CLAUDE_TIMEOUT': config.get('CTI_CLAUDE_TIMEOUT', '300'),
        'CTI_MAX_RESPONSE_LENGTH': config.get('CTI_MAX_RESPONSE_LENGTH', '500'),
        'LOCK_FILE': os.path.join(work_dir, '.bridge_lock'),
        'CLAUDE_PID_FILE': os.path.join(work_dir, '.claude_pids'),
        'WECHAT_FILE_DIR': os.path.join(wechat_data_dir, 'msg', 'file') if wechat_data_dir else '',
    }

    # 日志
    logger = setup_logging(log_dir)

    # 运行时状态
    write_runtime_status(runtime_dir, state='starting', chat_name=chat_name)
    _G['RUNTIME_DIR'] = runtime_dir

    if not acquire_lock():
        logger.error("无法获取单实例锁")
        return

    logger.info(f"桥接启动: chat={chat_name}, work_dir={work_dir}")

    wechat = None
    state = STATE_IDLE
    last_fingerprint = None
    last_processed_text = None
    cooldown_until = 0
    recent_hashes = {}

    print("=" * 60)
    print("WeChat <-> Claude 桥接系统")
    print(f"  监控对象: {chat_name}")
    print(f"  工作目录: {work_dir}")
    print(f"  日志目录: {log_dir}")
    print()
    print("状态机: IDLE → (收到claude) → ACTIVE → (每条消息=指令) → EXECUTE → ACTIVE")
    print("按 Ctrl+C 停止")
    print("=" * 60)
    print()

    def mark_processed(wechat, chat_name, sent_text=None, cmd_text=None):
        """更新基准，防止自己的回复被误判为新消息。

        sent_text: 刚发送的文本，直接记录不依赖微信 UI 回读。
        cmd_text:  收到的指令原文，用于内容级去重。
        """
        nonlocal last_fingerprint, last_processed_text, cooldown_until, recent_hashes
        if sent_text:
            last_processed_text = sent_text
        if cmd_text:
            recent_hashes[hashlib.md5(cmd_text.encode()).hexdigest()] = time.time() + 30
        time.sleep(2.0)
        fp = get_cell_fingerprint(wechat, chat_name)
        if fp:
            last_fingerprint = fp
        if not sent_text:
            text = get_last_message_text(wechat, chat_name)
            if text:
                last_processed_text = text
        cooldown_until = time.time() + 5

    def _send_result_with_files(text):
        """发送结果到微信，自动处理 __FILE__: 标记的文件，返回清洗后的纯文本"""
        files_to_send = []
        clean_lines = []
        for line in text.split('\n'):
            if line.startswith('__FILE__:'):
                fpath = line[8:].strip()
                if os.path.isfile(fpath):
                    files_to_send.append(fpath)
            else:
                clean_lines.append(line)
        clean_text = '\n'.join(clean_lines)

        for fp in files_to_send:
            print(f"  -> 发送文件: {fp}")
            send_file_to_wechat(wechat, fp)
            time.sleep(0.4)

        text_to_send = clean_text.strip()
        if text_to_send:
            send_message(wechat, text_to_send)
        return text_to_send

    # 初始化 last_fingerprint，跳过启动前的旧消息
    tmp_wechat = get_wechat()
    if tmp_wechat:
        last_fingerprint = get_cell_fingerprint(tmp_wechat, chat_name)

    try:
        while True:
            time.sleep(check_interval)

            # 连接微信
            if not wechat or not wechat.Exists(0, 0.3):
                wechat = get_wechat()
                if not wechat:
                    continue

            # 读取最新消息指纹（含时间戳，同一文本多次发送也能区分）
            fingerprint = get_cell_fingerprint(wechat, chat_name)
            if not fingerprint:
                continue
            if fingerprint == last_fingerprint:
                continue
            last_fingerprint = fingerprint

            # 提取消息正文
            current = get_last_message_text(wechat, chat_name)
            if not current:
                continue
            # 防止处理自己的回复（含微信截断情况）
            if last_processed_text:
                cmp_len = min(len(current), len(last_processed_text), 30)
                if current[:cmp_len] == last_processed_text[:cmp_len]:
                    continue

            # 防止把自己的系统消息当指令处理
            if current.startswith('(已') or current.startswith('酷狗:'):
                continue

            # 冷却期：忽略 Claude 脚本产生的回声消息
            # 指纹已在循环顶部更新，冷却结束后不会重复检测同一条
            if time.time() < cooldown_until:
                continue

            # 内容级去重：同一条消息 30 秒内不处理第二次
            cmd_hash = hashlib.md5(current.encode()).hexdigest()
            if cmd_hash in recent_hashes and time.time() < recent_hashes[cmd_hash]:
                continue
            # 清理过期哈希
            now = time.time()
            recent_hashes = {h: exp for h, exp in recent_hashes.items() if exp > now}

            print(f"[{time.strftime('%H:%M:%S')}] [{state}] 收到: {current}")
            logger.info(f"[{state}] 收到: {current}")

            # --- IDLE 状态：等待 claude 进入指令模式 ---
            if state == STATE_IDLE:
                if is_claude_start(current) and not extract_command(current):
                    state = STATE_ACTIVE
                    print(f"  -> 进入指令模式")

                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已进入指令模式，发送 claude/exit 退出)")
                    mark_processed(wechat, chat_name, "(已进入指令模式，发送 claude/exit 退出)", cmd_text=current)
                    warmup_claude()  # 后台预热 Claude，用户打字期间完成冷启动

            # --- ACTIVE 状态：每条消息 = 一条指令 ---
            elif state == STATE_ACTIVE:
                # 退出指令模式
                if is_claude_start(current) and not extract_command(current):
                    print(f"  -> 退出指令模式，回到 IDLE")
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已退出)")
                    mark_processed(wechat, chat_name, "(已退出)", cmd_text=current)
                    state = STATE_IDLE
                    continue

                cmd_lower = current.lower().strip()
                if cmd_lower in ('exit', '退出', 'quit'):
                    print(f"  -> 退出指令模式，回到 IDLE")
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已退出)")
                    mark_processed(wechat, chat_name, "(已退出)", cmd_text=current)
                    state = STATE_IDLE
                    continue

                # 检查是否是重置指令
                if current.strip() in ('重置', '重置会话', '新会话', 'reset', 'new'):
                    reset_session()
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已重置)")
                    mark_processed(wechat, chat_name, "(已重置)", cmd_text=current)
                    print(f"  -> 会话已重置")
                    continue

                # 图片消息
                if current == '[图片]':
                    state = STATE_EXECUTE
                    print(f"  -> 收到图片消息")
                    open_chat(wechat, chat_name)
                    img_paths = capture_images_from_chat(wechat)
                    if img_paths:
                        print(f"  -> 已截图 {len(img_paths)} 张: {img_paths}")
                        result = call_claude(f"用户发来了 {len(img_paths)} 张图片，"
                                            f"文件路径: {', '.join(img_paths)}。"
                                            f"请查看这些图片并回复用户。")
                    else:
                        result = "(未能获取图片，请确认图片已加载后重试)"
                    open_chat(wechat, chat_name)
                    clean = _send_result_with_files(result)
                    mark_processed(wechat, chat_name, clean or result, cmd_text=current)
                    state = STATE_ACTIVE
                    continue

                # 文件消息
                if current == '[文件]':
                    state = STATE_EXECUTE
                    print(f"  -> 收到文件消息")
                    time.sleep(0.5)
                    files = find_latest_files(3)
                    if files:
                        latest = files[0]
                        fname = os.path.basename(latest)
                        print(f"  -> 最新文件: {fname}")
                        result = call_claude(f"用户发来了一个文件: {fname}，"
                                            f"完整路径: {latest}。"
                                            f"请查看该文件并回复用户。")
                    else:
                        result = "(未能找到文件，请确认文件已下载后重试)"
                    open_chat(wechat, chat_name)
                    clean = _send_result_with_files(result)
                    mark_processed(wechat, chat_name, clean or result, cmd_text=current)
                    state = STATE_ACTIVE
                    continue

                # 执行指令
                state = STATE_EXECUTE
                session_tag = "[新会话]" if not _session_started else "[延续]"
                print(f"  -> {session_tag} 指令: {current}")

                handled, local_result = dispatch_local(current)
                if handled:
                    if isinstance(local_result, tuple) and local_result[0] == '__SEND_FILE__':
                        fpath = local_result[1]
                        open_chat(wechat, chat_name)
                        ok = send_file_to_wechat(wechat, fpath)
                        result = f"已发送: {os.path.basename(fpath)}" if ok else "发送失败"
                        print(f"  -> 发送文件: {fpath}")
                    else:
                        result = local_result
                        print(f"  -> 本地指令: {result[:60]}")
                else:
                    result = call_claude(current)

                open_chat(wechat, chat_name)
                clean = _send_result_with_files(result)
                mark_processed(wechat, chat_name, clean or result, cmd_text=current)
                print(f"  -> 已回复，继续等待指令")
                state = STATE_ACTIVE

            # --- EXECUTE 状态不应该持续到下一轮 ---
            elif state == STATE_EXECUTE:
                state = STATE_IDLE

    except KeyboardInterrupt:
        print()
        print("桥接系统已停止")
        logger.info("桥接系统已停止 (KeyboardInterrupt)")
        return
    except Exception as e:
        msg = f"运行出错(3秒后自动重启): {e}"
        print(msg)
        import traceback
        traceback.print_exc()
        logger.error(msg)
        logger.error(traceback.format_exc())
    finally:
        release_lock()
        write_runtime_status(runtime_dir, state='stopped', chat_name=chat_name)


def main():
    parser = argparse.ArgumentParser(description='WeChat <-> Claude 桥接系统')
    parser.add_argument('chat_name', nargs='?', default=None,
                        help='监控的微信联系人名称（默认: 文件传输助手）')
    parser.add_argument('--config', '-c', default=None,
                        help='配置文件路径（默认: 自动查找 config.env）')
    args = parser.parse_args()

    config = load_config(args.config)
    chat_name = args.chat_name or config.get('CTI_CHAT_NAME', '文件传输助手')

    while True:
        run_bridge(chat_name, config=config)
        print("桥接已退出，3秒后自动重启...")
        time.sleep(3)


if __name__ == '__main__':
    main()
