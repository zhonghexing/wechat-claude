"""
微信自动回复工具
监控指定联系人的新消息，自动回复预设内容。

用法:
  python wechat_reply.py                      # 监控文件传输助手，使用默认回复
  python wechat_reply.py "联系人名"            # 监控指定联系人
  python wechat_reply.py "联系人名" "回复内容"  # 自定义回复内容
"""

import sys
import time
import io
import uiautomation as auto

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

DEFAULT_REPLY = "收到！这是自动回复。[由Python脚本发送]"


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


def get_chat_cell_info(wechat, chat_name):
    """获取聊天列表中指定联系人的 Cell 信息（用于检测新消息）"""
    session_list = wechat.Control(AutomationId='session_list')
    if not session_list.Exists(0, 0.5):
        return None
    cell = find_child_by_autoid(session_list, f"session_item_{chat_name}")
    if cell:
        try:
            return {
                'name': cell.Name,
                'rect': cell.BoundingRectangle,
            }
        except:
            pass
    return None


def is_chat_open(wechat, chat_name):
    """检查指定聊天是否在主窗口中打开"""
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.3):
        return False
    input_field = find_child_by_autoid(detail, 'chat_input_field')
    if input_field and chat_name in (input_field.Name or ''):
        return True
    return False


def open_chat(wechat, chat_name):
    """打开指定聊天（如果已打开则不重复操作）"""
    if is_chat_open(wechat, chat_name):
        return True

    # 确保在微信标签页
    chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
    if chat_tab.Exists(0, 0.5):
        chat_tab.Click()
        time.sleep(0.3)

    if is_chat_open(wechat, chat_name):
        return True

    # 点击聊天列表中的联系人
    auto_id = f"session_item_{chat_name}"
    session_list = wechat.Control(AutomationId='session_list')
    if session_list.Exists(0, 1):
        cell = find_child_by_autoid(session_list, auto_id)
        if cell:
            cell.Click()
            time.sleep(1.5)
            return True

    # 搜索
    search_field = wechat.Control(Name='搜索', ClassName='mmui::XValidatorTextEdit')
    if search_field.Exists(0, 1):
        search_field.Click()
        time.sleep(0.3)
        auto.SendKeys('{Ctrl}a')
        time.sleep(0.1)
        auto.SendKeys(chat_name)
        time.sleep(0.8)
        session_list = wechat.Control(AutomationId='session_list')
        if session_list.Exists(0, 1):
            cell = find_child_by_autoid(session_list, auto_id)
            if cell:
                cell.Click()
                time.sleep(1.5)
                return True
    return False


def send_reply(wechat, message):
    """在当前聊天窗口发送回复消息"""
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.5):
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
    time.sleep(0.3)

    auto.SendKeys('{Ctrl}a')
    time.sleep(0.1)
    auto.SendKeys(message)
    time.sleep(0.2)
    auto.SendKeys('{Enter}')
    return True


def get_last_message_text(wechat, chat_name):
    """
    尝试通过聊天列表中的预览文本获取最新消息内容。
    ChatSessionCell 的 Name 属性包含最新消息的预览。
    """
    info = get_chat_cell_info(wechat, chat_name)
    if info:
        # Name 格式: "联系人名\n[视频] \n昨天 21:41\n" 或 "联系人名\n消息内容\n时间\n"
        parts = info['name'].split('\n')
        if len(parts) >= 2:
            # 第二个部分通常是最后一条消息的预览
            return parts[1].strip()
    return None


def monitor_and_reply(chat_name, reply_message, check_interval=3):
    """
    监控指定联系人的新消息并自动回复。

    工作原理：
    1. 打开聊天窗口
    2. 定期检查聊天列表中该联系人的预览文本
    3. 如果预览文本发生变化（说明有新消息），自动发送回复
    """
    print(f"监控模式启动")
    print(f"  目标: {chat_name}")
    print(f"  回复: {reply_message}")
    print(f"  检测间隔: {check_interval} 秒")
    print()
    print("按 Ctrl+C 停止监控...")
    print()

    # 查找微信窗口（兼容中英文）
    wechat = None
    for name in ['微信', 'Weixin', 'WeChat']:
        w = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
        if w.Exists(0, 0.5):
            wechat = w
            break
    if not wechat:
        print("[ERROR] 未找到微信窗口")
        return

    wechat.SetActive()
    time.sleep(0.3)

    if not open_chat(wechat, chat_name):
        print(f"[ERROR] 无法打开「{chat_name}」聊天")
        return

    # 记录当前最新消息文本（作为基准）
    last_seen = get_last_message_text(wechat, chat_name)
    print(f"当前最新消息预览: {last_seen}")
    print()

    reply_count = 0
    try:
        while True:
            time.sleep(check_interval)

            # 刷新窗口状态
            if not wechat.Exists(0, 0.5):
                wechat = None
                for name in ['微信', 'Weixin', 'WeChat']:
                    w = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
                    if w.Exists(0, 0.5):
                        wechat = w
                        break
                wechat.SetActive()
                time.sleep(0.3)

            wechat.SetActive()
            time.sleep(0.2)

            # 检查是否有新消息
            current = get_last_message_text(wechat, chat_name)
            if current and current != last_seen:
                print(f"[{time.strftime('%H:%M:%S')}] 检测到新消息: {current}")

                # 确保在正确的聊天窗口
                if not is_chat_open(wechat, chat_name):
                    open_chat(wechat, chat_name)
                    time.sleep(1)

                # 发送回复
                if send_reply(wechat, reply_message):
                    reply_count += 1
                    print(f"  -> 已回复 (第{reply_count}次): {reply_message}")
                    # 更新基准为回复内容，避免对自己的消息再次触发
                    time.sleep(0.5)
                    last_seen = get_last_message_text(wechat, chat_name)
                else:
                    print(f"  -> 回复发送失败")

    except KeyboardInterrupt:
        print()
        print(f"监控结束，共回复 {reply_count} 次")


def main():
    if len(sys.argv) < 2:
        chat_name = "文件传输助手"
        reply = DEFAULT_REPLY
    elif len(sys.argv) == 2:
        chat_name = sys.argv[1]
        reply = DEFAULT_REPLY
    else:
        chat_name = sys.argv[1]
        reply = sys.argv[2]

    monitor_and_reply(chat_name, reply)


if __name__ == '__main__':
    main()
