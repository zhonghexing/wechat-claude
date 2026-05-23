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

try:
    import uiautomation as auto
except ImportError:
    print("请先安装: pip install uiautomation")
    sys.exit(1)

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ---------- 配置 ----------
CLAUDE_CLI = r"C:\Users\zhx\Desktop\CC\nodejs\node-v20.11.0-win-x64\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
WORK_DIR = r"D:\claude自动"
CHECK_INTERVAL = 0.3
MAX_RESPONSE_LENGTH = 500
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
LOCK_FILE = os.path.join(WORK_DIR, ".bridge_lock")
SW_RESTORE = 9
SW_SHOW = 5

# ---------- 状态机 ----------
STATE_IDLE = "idle"            # 静默监控中
STATE_ACTIVE = "active"        # 指令模式，每条消息当指令执行
STATE_EXECUTE = "execute"      # 执行中

# ---------- 单实例锁 ----------

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, old_pid)
            if handle:
                kernel32.CloseHandle(handle)
                print(f"桥接已在运行中 (PID: {old_pid})，无需重复启动。")
                return False
        except:
            pass
        os.remove(LOCK_FILE)
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
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


def send_message(wechat, message):
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

    # 用剪贴板粘贴，避免 SendKeys 逐字打字丢字符
    subprocess.run('clip', input=message, text=True, shell=True,
                   creationflags=CREATE_NO_WINDOW)
    auto.SendKeys('{Ctrl}a')
    time.sleep(0.02)
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.03)
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

    # 酷狗音乐播放
    if ('酷狗' in cmd_lower or 'kugou' in cmd_lower) and '播放' in cmd_lower:
        import re as _re
        # 提取歌名: "播放XXX" 或 "播放 XXX"
        m = _re.search(r'播放\s*(.+?)(?:\s*$)', cmd)
        if m:
            song = m.group(1).strip()
            song = _re.sub(r'[歌曲音乐]', '', song).strip()
        else:
            song = cmd.split('播放')[-1].strip()

        if song:
            try:
                r = subprocess.run(
                    ['python', '-u', os.path.join(WORK_DIR, 'kugou_play.py'), song],
                    capture_output=True, text=True, timeout=30,
                    cwd=WORK_DIR, encoding='utf-8', errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                )
                return True, f"酷狗: {song}\n{r.stdout.strip()[-300:] if r.stdout else '(无输出)'}"
            except Exception as e:
                return True, f"酷狗播放失败: {e}"

    return False, None


# ---------- Claude CLI ----------

# 会话状态：记录是否已有活跃会话，支持 --continue 保持上下文连贯
_session_started = False


def reset_session():
    """重置会话，下次调用将开始新会话"""
    global _session_started
    _session_started = False


def call_claude(prompt):
    """后台调用 Claude，自动保持会话连贯"""
    global _session_started
    try:
        if _session_started:
            args = [CLAUDE_CLI, '--continue', '-p', prompt, '--permission-mode', 'bypassPermissions', '--effort', 'low']
        else:
            args = [CLAUDE_CLI, '-p', prompt, '--permission-mode', 'bypassPermissions', '--effort', 'low']
            _session_started = True

        # 最小化启动 Claude
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6  # SW_MINIMIZE
        result = subprocess.run(
            args,
            capture_output=True, text=True, timeout=300,
            cwd=WORK_DIR, encoding='utf-8', errors='replace',
            creationflags=CREATE_NEW_CONSOLE,
            startupinfo=startupinfo,
        )
        output = result.stdout.strip()
        if result.stderr:
            stderr_short = result.stderr.strip()[:200]
            if stderr_short:
                output += f"\n[stderr: {stderr_short}]"
        return output[:MAX_RESPONSE_LENGTH] if output else f"(无输出, exit={result.returncode})"
    except subprocess.TimeoutExpired:
        return "(Claude 执行超时，5分钟未完成)"
    except FileNotFoundError:
        return f"(未找到 Claude CLI: {CLAUDE_CLI})"
    except Exception as e:
        return f"(调用失败: {e})"


# ---------- 主循环 ----------

def run_bridge(chat_name="文件传输助手"):
    if not acquire_lock():
        return

    wechat = None
    state = STATE_IDLE
    last_fingerprint = None      # cell 完整 Name（含时间戳），用于新消息检测
    last_processed_text = None   # 上次处理的消息正文，防止重复处理

    print("=" * 60)
    print("WeChat <-> Claude 桥接系统")
    print(f"  监控对象: {chat_name}")
    print(f"  收到指令时自动弹出 Claude 终端窗口")
    print()
    print("状态机: IDLE → (收到claude) → ACTIVE → (每条消息=指令) → EXECUTE → ACTIVE")
    print("按 Ctrl+C 停止")
    print("=" * 60)
    print()

    def mark_processed(wechat, chat_name):
        """更新基准，防止自己的回复被误判为新消息"""
        nonlocal last_fingerprint, last_processed_text
        time.sleep(0.15)
        fp = get_cell_fingerprint(wechat, chat_name)
        text = get_last_message_text(wechat, chat_name)
        if fp:
            last_fingerprint = fp
        if text:
            last_processed_text = text

    # 初始化 last_fingerprint，跳过启动前的旧消息
    tmp_wechat = get_wechat()
    if tmp_wechat:
        last_fingerprint = get_cell_fingerprint(tmp_wechat, chat_name)

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

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
            if current == last_processed_text:
                continue

            print(f"[{time.strftime('%H:%M:%S')}] [{state}] 收到: {current}")

            # --- IDLE 状态：等待 claude 进入指令模式 ---
            if state == STATE_IDLE:
                if is_claude_start(current) and not extract_command(current):
                    # 单独一条 "claude" → 进入指令模式
                    state = STATE_ACTIVE
                    print(f"  -> 进入指令模式")

                    ensure_wechat_ready(wechat)
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已进入指令模式，每条消息将作为指令执行。发送 claude 或 exit 退出)")
                    mark_processed(wechat, chat_name)

            # --- ACTIVE 状态：每条消息 = 一条指令 ---
            elif state == STATE_ACTIVE:
                # 退出指令模式
                if is_claude_start(current) and not extract_command(current):
                    print(f"  -> 退出指令模式，回到 IDLE")
                    ensure_wechat_ready(wechat)
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已退出指令模式)")
                    mark_processed(wechat, chat_name)
                    state = STATE_IDLE
                    continue

                cmd_lower = current.lower().strip()
                if cmd_lower in ('exit', '退出', 'quit'):
                    print(f"  -> 退出指令模式，回到 IDLE")
                    ensure_wechat_ready(wechat)
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(已退出指令模式)")
                    mark_processed(wechat, chat_name)
                    state = STATE_IDLE
                    continue

                # 检查是否是重置指令
                if current.strip() in ('重置', '重置会话', '新会话', 'reset', 'new'):
                    reset_session()
                    ensure_wechat_ready(wechat)
                    open_chat(wechat, chat_name)
                    send_message(wechat, "(会话已重置，下次指令将开始新对话)")
                    mark_processed(wechat, chat_name)
                    print(f"  -> 会话已重置")
                    continue

                # 执行指令
                state = STATE_EXECUTE
                session_tag = "[新会话]" if not _session_started else "[延续]"
                print(f"  -> {session_tag} 指令: {current}")

                handled, local_result = dispatch_local(current)
                if handled:
                    result = local_result
                    print(f"  -> 本地指令: {result[:60]}")
                else:
                    result = call_claude(current)

                ensure_wechat_ready(wechat)
                open_chat(wechat, chat_name)
                time.sleep(0.05)
                send_message(wechat, result)
                mark_processed(wechat, chat_name)

                print(f"  -> 已回复，继续等待指令")
                state = STATE_ACTIVE

            # --- EXECUTE 状态不应该持续到下一轮 ---
            elif state == STATE_EXECUTE:
                state = STATE_IDLE

    except KeyboardInterrupt:
        print()
        print("桥接系统已停止")
        raise
    except Exception as e:
        print(f"运行出错(3秒后自动重启): {e}")
        import traceback
        traceback.print_exc()
        release_lock()
        time.sleep(3)
        # 递归重启
        try:
            run_bridge(chat_name)
        except KeyboardInterrupt:
            pass
        return
    finally:
        release_lock()


def main():
    chat_name = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"
    try:
        run_bridge(chat_name)
    finally:
        release_lock()


if __name__ == '__main__':
    main()
