"""发送剪贴板中的图片到微信联系人"""
import sys
import time
import io
import uiautomation as auto

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def find_wechat_window():
    wechat = auto.WindowControl(Name='微信', searchDepth=1)
    if not wechat.Exists(0, 1):
        print("[ERROR] 未找到微信窗口")
        return None
    return wechat


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


def is_chat_already_open(wechat, chat_name):
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 0.5):
        return False
    input_field = find_child_by_autoid(detail, 'chat_input_field')
    if input_field and chat_name in (input_field.Name or ''):
        return True
    return False


def click_chat(wechat, chat_name="文件传输助手"):
    if is_chat_already_open(wechat, chat_name):
        print(f"「{chat_name}」已在当前窗口打开，无需切换")
        return True

    chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
    if chat_tab.Exists(0, 0.5):
        chat_tab.Click()
        time.sleep(0.3)

    if is_chat_already_open(wechat, chat_name):
        print(f"「{chat_name}」已在当前窗口打开，无需切换")
        return True

    auto_id = f"session_item_{chat_name}"
    session_list = wechat.Control(AutomationId='session_list')
    if session_list.Exists(0, 1):
        cell = find_child_by_autoid(session_list, auto_id)
        if cell:
            cell.Click()
            time.sleep(1.5)
            print(f"已打开「{chat_name}」聊天窗口")
            return True

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


def paste_and_send(wechat):
    """在当前聊天窗口粘贴剪贴板内容并发送"""
    detail = wechat.Control(ClassName='mmui::ChatDetailView')
    if not detail.Exists(0, 1):
        print("未找到 ChatDetailView")
        return False

    chat_page = find_child_by_class(detail, 'mmui::ChatMessagePage')
    if not chat_page:
        print("聊天页面未加载")
        return False

    input_field = find_child_by_autoid(chat_page, 'chat_input_field')
    if not input_field:
        print("未找到输入框")
        return False

    try:
        input_field.Click()
    except:
        rect = input_field.BoundingRectangle
        auto.Click(rect.left + 50, rect.top + 10)
    time.sleep(0.3)

    auto.SendKeys('{Ctrl}v')
    time.sleep(0.5)
    print("图片已粘贴到输入框")

    auto.SendKeys('{Enter}')
    time.sleep(0.3)
    print("图片已发送")
    return True


def main():
    chat_name = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"

    wechat = find_wechat_window()
    if not wechat:
        sys.exit(1)

    wechat.SetActive()
    time.sleep(0.3)

    if not click_chat(wechat, chat_name):
        sys.exit(1)

    if paste_and_send(wechat):
        print("发送完成!")
    else:
        print("发送失败")
        sys.exit(1)


if __name__ == '__main__':
    main()
