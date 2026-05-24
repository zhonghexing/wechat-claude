"""
微信消息/文件发送工具 — 通过桌面微信 UIAutomation 发送
用法:
  python wechat_send_to.py text "消息内容"               # 发文字到文件传输助手
  python wechat_send_to.py text "消息" "联系人"           # 发文字到指定联系人
  python wechat_send_to.py file "文件路径"                # 发文件到文件传输助手
  python wechat_send_to.py file "文件路径" "联系人"       # 发文件到指定联系人
"""
import sys, time, io, os, ctypes

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import uiautomation as auto


def find_wechat():
    for name in ['微信', 'Weixin', 'WeChat']:
        w = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
        if w.Exists(0, 0.5):
            return w
    return None


def open_chat(wechat, chat_name):
    if ctypes.windll.user32.IsIconic(wechat.NativeWindowHandle):
        ctypes.windll.user32.ShowWindow(wechat.NativeWindowHandle, 9)
    wechat.SetActive()
    time.sleep(0.2)

    # 1. 关闭已打开的独立聊天窗口
    standalone = auto.WindowControl(Name=chat_name)
    if standalone.Exists(0, 0.3):
        try:
            close_btn = standalone.Control(Name='关闭')
            if close_btn.Exists(0, 0.3):
                close_btn.Click()
                time.sleep(0.1)
        except:
            pass

    # 2. 检查是否已在主窗口打开
    if _is_chat_open(wechat, chat_name):
        return True

    # 3. 点击微信标签页
    chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
    if chat_tab.Exists(0, 0.2):
        chat_tab.Click()
        time.sleep(0.08)

    if _is_chat_open(wechat, chat_name):
        return True

    # 4. 从 session_list 点击联系人
    cell = wechat.Control(AutomationId=f"session_item_{chat_name}")
    if cell.Exists(0, 1):
        cell.Click()
        time.sleep(0.5)
        # 如果点击后变成了独立窗口，关掉重来
        if not _is_chat_open(wechat, chat_name):
            standalone2 = auto.WindowControl(Name=chat_name)
            if standalone2.Exists(0, 0.3):
                try:
                    close_btn = standalone2.Control(Name='关闭')
                    if close_btn.Exists(0, 0.3):
                        close_btn.Click()
                except:
                    pass
                time.sleep(0.3)
                wechat.SetActive()
                cell2 = wechat.Control(AutomationId=f"session_item_{chat_name}")
                if cell2.Exists(0, 1):
                    cell2.Click()
                    time.sleep(0.8)
        return _is_chat_open(wechat, chat_name)

    return False


def _is_chat_open(wechat, chat_name):
    """检查聊天是否已在主窗口中打开"""
    # 检查输入框是否包含 chat_name
    def find_aid(parent, aid, depth=0):
        if depth > 20: return None
        try:
            if parent.AutomationId == aid: return parent
            for c in parent.GetChildren():
                r = find_aid(c, aid, depth+1)
                if r: return r
        except: pass
        return None
    inp = find_aid(wechat, 'chat_input_field')
    if inp and chat_name in (inp.Name or ''):
        return True
    return False


def send_text(chat_name, text):
    wechat = find_wechat()
    if not wechat:
        return False, "未找到微信窗口"
    if not open_chat(wechat, chat_name):
        return False, f"未找到联系人: {chat_name}"

    # 先定位 ChatMessagePage，再搜 chat_input_field
    def find_cls(parent, cls_name, depth=0):
        if depth > 20: return None
        try:
            if parent.ClassName == cls_name: return parent
        except:
            return None
        try:
            children = parent.GetChildren()
        except:
            children = []
        for c in children:
            r = find_cls(c, cls_name, depth+1)
            if r: return r
        return None
    chat_page = find_cls(wechat, 'mmui::ChatMessagePage')
    if not chat_page:
        return False, "未找到聊天页面"

    def find_aid(parent, aid, depth=0):
        if depth > 15: return None
        try:
            if parent.AutomationId == aid: return parent
        except:
            return None
        try:
            children = parent.GetChildren()
        except:
            children = []
        for c in children:
            r = find_aid(c, aid, depth+1)
            if r: return r
        return None
    inp = find_aid(chat_page, 'chat_input_field')
    if not inp:
        return False, "未找到输入框"

    inp.Click()
    time.sleep(0.05)
    # 用剪贴板发送（UTF-8 安全）
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    size = (len(text) + 1) * 2
    hMem = kernel32.GlobalAlloc(0x0002, size)
    ptr = kernel32.GlobalLock(hMem)
    ctypes.cdll.msvcrt.wcscpy(ctypes.c_void_p(ptr), text)
    kernel32.GlobalUnlock(hMem)
    user32.SetClipboardData(CF_UNICODETEXT, hMem)
    user32.CloseClipboard()

    auto.SendKeys('{Ctrl}a')
    time.sleep(0.02)
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.15)
    auto.SendKeys('{Enter}')
    return True, "发送成功"


def send_file(chat_name, filepath):
    filepath = os.path.abspath(filepath)
    if not os.path.isfile(filepath):
        return False, f"文件不存在: {filepath}"

    wechat = find_wechat()
    if not wechat:
        return False, "未找到微信窗口"
    if not open_chat(wechat, chat_name):
        return False, f"未找到联系人: {chat_name}"

    # CF_HDROP 写剪贴板
    CF_HDROP = 15
    class DROPFILES(ctypes.Structure):
        _fields_ = [("pFiles", ctypes.c_uint),
                     ("pt", ctypes.c_long * 2),
                     ("fNC", ctypes.c_int),
                     ("fWide", ctypes.c_int)]

    filedata = (filepath + '\0\0').encode('utf-16-le')
    df_size = ctypes.sizeof(DROPFILES)
    total = df_size + len(filedata)

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    hMem = kernel32.GlobalAlloc(0x0002, total)
    ptr = kernel32.GlobalLock(hMem)
    df = DROPFILES()
    df.pFiles = df_size
    df.fWide = 1
    ctypes.memmove(ptr, ctypes.addressof(df), df_size)
    ctypes.memmove(ptr + df_size, filedata, len(filedata))
    kernel32.GlobalUnlock(hMem)
    user32.SetClipboardData(CF_HDROP, hMem)
    user32.CloseClipboard()

    auto.SendKeys('{Ctrl}v')
    time.sleep(0.3)
    auto.SendKeys('{Enter}')
    return True, f"文件已发送: {os.path.basename(filepath)}"


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python wechat_send_to.py text|file <内容> [联系人]")
        sys.exit(1)

    mode = sys.argv[1]
    content = sys.argv[2]
    chat = sys.argv[3] if len(sys.argv) > 3 else '文件传输助手'

    if mode == 'text':
        ok, msg = send_text(chat, content)
    elif mode == 'file':
        ok, msg = send_file(chat, content)
    else:
        print(f"未知模式: {mode}")
        sys.exit(1)

    print(msg)
    sys.exit(0 if ok else 1)
