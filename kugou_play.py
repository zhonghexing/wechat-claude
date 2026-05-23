"""
酷狗音乐搜索播放（使用校准坐标，自适应窗口大小）
用法: python kugou_play.py "歌名"
"""
import sys, io, time, json, os, subprocess
import uiautomation as auto
import pyautogui

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

CALIB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kugou_positions.json')


def load_positions():
    if not os.path.exists(CALIB_FILE):
        print(f'[错误] 未找到校准文件 {CALIB_FILE}，请先运行 kugou_calibrate.py')
        return None
    with open(CALIB_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def pct_to_abs(win_left, win_top, win_w, win_h, pct_x, pct_y):
    """比例坐标转绝对屏幕坐标"""
    return (win_left + int(win_w * pct_x / 100),
            win_top + int(win_h * pct_y / 100))


def is_kugou_running():
    w = auto.WindowControl(Name='酷狗音乐', searchDepth=1)
    return w if w.Exists(0, 0.5) else None


def launch_kugou():
    subprocess.Popen(['D:/KGMusic/KuGou.exe'])
    for _ in range(10):
        time.sleep(1)
        w = is_kugou_running()
        if w:
            return w
    return None


def search_and_play(song_name):
    calib = load_positions()
    if not calib:
        return False

    # 确保酷狗运行
    w = is_kugou_running()
    if not w:
        print('酷狗未运行，正在启动...')
        w = launch_kugou()
        if not w:
            print('[错误] 无法启动酷狗')
            return False

    w.SetActive()
    time.sleep(0.3)
    r = w.BoundingRectangle
    win_w, win_h = r.width(), r.height()

    def click_point(key):
        info = calib[key]
        x, y = pct_to_abs(r.left, r.top, win_w, win_h, info['pct_x'], info['pct_y'])
        pyautogui.click(x, y)
        return x, y

    def dblclick_point(key):
        info = calib[key]
        x, y = pct_to_abs(r.left, r.top, win_w, win_h, info['pct_x'], info['pct_y'])
        pyautogui.doubleClick(x, y)
        return x, y

    # 1. 点击搜索框
    x, y = click_point('search_box')
    time.sleep(0.3)
    print(f'1. 点击搜索框 ({x},{y})')

    # 2. 粘贴歌名
    subprocess.run(['powershell', '-Command', f'Set-Clipboard -Value "{song_name}"'], capture_output=True)
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.1)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.5)
    print(f'2. 粘贴: {song_name}')

    # 3. 点击搜索按钮
    x, y = click_point('search_btn')
    time.sleep(3)
    print(f'3. 点击搜索按钮 ({x},{y})')

    # 4. 双击歌名即自动播放
    x, y = dblclick_point('first_song')
    time.sleep(1)
    print(f'4. 双击歌名播放 ({x},{y})')

    print(f'完成！{song_name} 应该正在播放')
    return True


def main():
    if len(sys.argv) < 2:
        print('用法: python kugou_play.py "歌名"')
        print('示例: python kugou_play.py "恋人"')
        sys.exit(1)

    song = sys.argv[1]
    search_and_play(song)


if __name__ == '__main__':
    main()
