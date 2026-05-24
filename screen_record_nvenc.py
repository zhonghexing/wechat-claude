"""
屏幕录制 — NVIDIA NVENC 硬件编码，高画质低CPU占用
用法:
  python screen_record_nvenc.py                          # 录制10秒
  python screen_record_nvenc.py -d 30                    # 录制30秒
  python screen_record_nvenc.py -d 60 -o my_video.mp4    # 指定输出文件
  python screen_record_nvenc.py -q high -d 120           # 高质量录制2分钟
"""

import subprocess, os, sys, time, argparse

# 用 imageio-ffmpeg 自带的 FFmpeg（带 NVENC 支持）
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = None

QUALITY_PRESETS = {
    # NVENC 硬编码 + main profile + yuv420p = WeChat video_item 兼容
    "medium":  "h264_nvenc -preset p4 -cq 23 -rc vbr -b:v 8M -profile:v main -pix_fmt yuv420p",
    "high":    "h264_nvenc -preset p5 -cq 18 -rc vbr -b:v 20M -profile:v main -pix_fmt yuv420p",
    "ultra":   "h264_nvenc -preset p6 -cq 15 -rc vbr -b:v 50M -profile:v main -pix_fmt yuv420p",
}

def record_screen(duration=10, output=None, quality="high"):
    if not FFMPEG or not os.path.isfile(FFMPEG):
        return False, "FFmpeg 未安装: pip install imageio-ffmpeg"

    if output is None:
        ts = int(time.time())
        output = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              f"_record_{ts}.mp4")

    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])

    cmd = f'"{FFMPEG}" -y -f gdigrab -framerate 30 -i desktop ' \
          f'-c:v {preset} ' \
          f'-movflags +faststart -t {duration} "{output}"'

    try:
        subprocess.run(cmd, shell=True, check=True,
                       capture_output=True, text=True, timeout=duration + 30)
        if os.path.isfile(output) and os.path.getsize(output) > 0:
            sz = os.path.getsize(output) / (1024 * 1024)
            return True, f"录制完成: {output} ({sz:.1f}MB)"
        return False, "录制失败：输出文件为空"
    except subprocess.TimeoutExpired:
        return False, "录制超时"
    except Exception as e:
        return False, f"录制失败: {e}"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="屏幕录制 — NVENC硬件编码")
    parser.add_argument('-d', '--duration', type=int, default=10, help='录制时长(秒)')
    parser.add_argument('-o', '--output', help='输出文件路径')
    parser.add_argument('-q', '--quality', default='high',
                        choices=['medium', 'high', 'ultra'], help='画质')
    args = parser.parse_args()

    print(f"录制 {args.duration}s ({args.quality} 画质)...")
    ok, msg = record_screen(args.duration, args.output, args.quality)
    print(msg)
    sys.exit(0 if ok else 1)
