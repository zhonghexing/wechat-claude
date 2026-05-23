"""
微信消息发送工具
用法: python wechat_send.py "你的消息内容"
默认发送给"文件传输助手"，也支持指定联系人。
"""

import sys
import time
import io
import uiautomation as auto

# 修复 Windows 控制台编码问题
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def find_wechat_window():
    """查找微信主窗口（兼容中英文版本）"""
    for name in ['微信', 'Weixin', 'WeChat']:
        wechat = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
        if wechat.Exists(0, 0.5):
            return wechat
    print("[ERROR] 未找到微信窗口，请确认微信已打开")
    return None


def find_child_by_class(parent, class_name):
    """在控件树中递归查找指定类名的子控件"""
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


def find_child_by_autoid(parent, auto_id):
    """在控件树中递归查找指定 AutomationId 的子控件"""
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


def find_child_by_name_class(parent, name, class_name):
    """在控件树中递归查找指定名称和类名的子控件"""
    try:
        for child in parent.GetChildren():
            if child.Name == name and child.ClassName == class_name:
                return child
            result = find_child_by_name_class(child, name, class_name)
            if result:
                return result
    except:
        pass
    return None


def is_chat_already_open(wechat, chat_name):
    """检查指定聊天是否已在主窗口中打开（避免双击产生独立窗口）"""
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.5):
        return False
    input_field = find_child_by_autoid(detail, 'chat_input_field')
    if input_field and chat_name in (input_field.Name or ''):
        return True
    return False


def click_chat(wechat, chat_name="文件传输助手"):
    """在聊天列表中点击指定联系人，如果已打开则不做任何操作"""
    # 先检查是否已在主窗口中打开
    if is_chat_already_open(wechat, chat_name):
        print(f"「{chat_name}」已在当前窗口打开，无需切换")
        return True

    # 确保在"微信"聊天标签页
    chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
    if chat_tab.Exists(0, 0.5):
        chat_tab.Click()
        time.sleep(0.3)

    # 如果切换标签后聊天已加载，检查一下
    if is_chat_already_open(wechat, chat_name):
        print(f"「{chat_name}」已在当前窗口打开，无需切换")
        return True

    # 查找会话列表中的指定聊天
    auto_id = f"session_item_{chat_name}"
    session_list = wechat.Control(AutomationId='session_list')
    if session_list.Exists(0, 1):
        cell = find_child_by_autoid(session_list, auto_id)
        if cell:
            cell.Click()
            time.sleep(1.5)
            print(f"已打开「{chat_name}」聊天窗口")
            return True

    # 列表中直接没找到，尝试搜索
    print(f"列表中未直接找到「{chat_name}」，尝试搜索...")
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
                print(f"已打开「{chat_name}」聊天窗口")
                return True

    print(f"未找到「{chat_name}」")
    return False


def send_message(wechat, message):
    """在当前聊天窗口发送消息"""
    # 通过遍历树找到 ChatMessagePage
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 1):
        print("未找到 ChatDetailView")
        return False

    chat_page = find_child_by_class(detail, 'mmui::ChatMessagePage')
    if not chat_page:
        print("聊天页面未加载")
        return False

    # 在 ChatMessagePage 中找输入框
    input_field = find_child_by_autoid(chat_page, 'chat_input_field')
    if not input_field:
        print("未找到输入框")
        return False

    # 点击输入框获取焦点
    try:
        input_field.Click()
    except:
        rect = input_field.BoundingRectangle
        auto.Click(rect.left + 50, rect.top + 10)
    time.sleep(0.3)

    # 清空已有内容
    auto.SendKeys('{Ctrl}a')
    time.sleep(0.1)

    # 输入消息内容
    auto.SendKeys(message)
    time.sleep(0.2)
    print(f"消息已输入: {message}")

    # 优先使用 Enter 发送（更可靠）
    auto.SendKeys('{Enter}')
    print("消息已发送")
    return True


def main():
    if len(sys.argv) < 2:
        print("用法: python wechat_send.py <消息内容> [联系人名称]")
        print('示例: python wechat_send.py "你好，这是一条测试消息"')
        print('示例: python wechat_send.py "你好" "文件传输助手"')
        sys.exit(1)

    message = sys.argv[1]
    chat_name = sys.argv[2] if len(sys.argv) > 2 else "文件传输助手"

    print(f"微信消息发送")
    print(f"  目标: {chat_name}")
    print(f"  内容: {message}")
    print()

    wechat = find_wechat_window()
    if not wechat:
        sys.exit(1)

    wechat.SetActive()
    time.sleep(0.3)

    if not click_chat(wechat, chat_name):
        sys.exit(1)

    if send_message(wechat, message):
        print()
        print("发送完成!")
    else:
        print("发送失败")
        sys.exit(1)


if __name__ == '__main__':
    main()
