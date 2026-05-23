"""
酷狗音乐 UI 校准工具
鼠标悬停 → 按 F8 记录 → 保存比例坐标（自适应窗口大小）
"""
import sys, io, time, json, os, ctypes
import uiautomation as auto

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

CALIB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kugou_positions.json')

POINTS = [
    ('search_box',  '1/4: 搜索框中间'),
    ('search_btn',  '2/4: 搜索按钮/放大镜图标'),
    ('first_song',  '3/4: 搜索结果第一首歌的歌名'),
    ('play_btn',    '4/4: 第一首歌的播放按钮'),
]


def get_mouse_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def is_f8_pressed():
    return ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000 != 0


def main():
    print('=' * 50)
    print('酷狗 UI 位置校准（比例坐标，自适应窗口大小）')
    print('鼠标悬停到目标位置 → 按 F8 记录')
    print('=' * 50)
    print()

    w = auto.WindowControl(Name='酷狗音乐', searchDepth=1)
    if not w.Exists(0, 2):
        print('[错误] 未找到酷狗窗口，请先打开酷狗音乐')
        return

    r = w.BoundingRectangle
    win_w, win_h = r.width(), r.height()
    print(f'酷狗窗口: {win_w}x{win_h}')
    print()

    positions = {'window_w': win_w, 'window_h': win_h}

    for key, desc in POINTS:
        print(f'>>> {desc}')
        print('    鼠标悬停后按 F8...')

        last_state = False
        while True:
            time.sleep(0.1)
            current_state = is_f8_pressed()
            if current_state and not last_state:
                break
            last_state = current_state

        abs_x, abs_y = get_mouse_pos()
        rel_x = abs_x - r.left
        rel_y = abs_y - r.top
        pct_x = round(rel_x / win_w * 100, 1)
        pct_y = round(rel_y / win_h * 100, 1)

        positions[key] = {
            'rel_x': rel_x, 'rel_y': rel_y,
            'pct_x': pct_x, 'pct_y': pct_y,
        }
        print(f'    已记录: 相对({rel_x},{rel_y})  比例({pct_x}%, {pct_y}%)')
        print()
        time.sleep(0.5)

    with open(CALIB_FILE, 'w', encoding='utf-8') as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)
    print(f'校准完成！已保存到 {CALIB_FILE}')

    print()
    print('=== 比例坐标 ===')
    for key, info in positions.items():
        if key in ('window_w', 'window_h'):
            continue
        desc = {k: d for k, d in POINTS}.get(key, key)
        print(f'  {desc}: {info["pct_x"]}% x {info["pct_y"]}%')


if __name__ == '__main__':
    main()
