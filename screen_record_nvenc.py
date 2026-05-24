"""
屏幕录制 — NVIDIA NVENC 硬件编码，高画质低CPU占用
用法:
  python screen_record_nvenc.py                          # 录制10秒, HEVC
  python screen_record_nvenc.py -d 30                    # 录制30秒
  python screen_record_nvenc.py -d 60 -o my_video.mp4    # 指定输出文件
  python screen_record_nvenc.py -q high -d 120           # 高质量录制2分钟
  python screen_record_nvenc.py --codec h264             # 强制 H.264 (微信兼容)
"""

import subprocess, os, sys, time, argparse

# 用 imageio-ffmpeg 自带的 FFmpeg（带 NVENC 支持）
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = None

# ── HEVC (hevc_nvenc) 预设 — 默认，兼容 WMP + 微信 ─────────────────────
# HEVC 没有 H.264 的 pic_struct_present_flag 协议违规问题
# dpb_size=3: 2K(14400MB)×4=57600MB < Level 5.0(69632MB) ✓
HEVC_PRESETS = {
    # level 不设 (auto): NVENC SDK 根据分辨率/帧率/码率自动选合理值
    # dpb_size=3: 2K(14400MB)×4=57600MB, HEVC Level 4 (122880MB) / 5 (245760MB) 均满足
    "medium":  "hevc_nvenc -preset p6 -cq 25 -rc vbr -b:v 12M -maxrate:v 18M "
               "-profile:v main -pix_fmt yuv420p "
               "-rc-lookahead 20 -g 150 -dpb_size 3 -aud 1 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 0",
    "high":    "hevc_nvenc -preset p7 -cq 20 -rc vbr -b:v 30M -maxrate:v 45M "
               "-profile:v main -pix_fmt yuv420p "
               "-rc-lookahead 32 -g 150 -dpb_size 3 -aud 1 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 1",
    "ultra":   "hevc_nvenc -preset p7 -cq 16 -rc vbr -b:v 80M -maxrate:v 120M "
               "-profile:v main -pix_fmt yuv420p "
               "-rc-lookahead 48 -g 150 -dpb_size 3 -aud 1 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 2",
}

# ── H.264 (h264_nvenc) 预设 — 兼容微信 video_item ─────────────────────
# WMP 无法播放: NVENC SDK 底层设 pic_struct_present_flag=1 但不生成
# pic_timing SEI, H.264 协议违规, FFmpeg BSF 无法修复
# weighted_pred=0 + b_ref_mode=0: Media Foundation 不支持 B 帧高级特性
# h264_metadata BSF: 注入 VUI 色彩元数据 (NVENC 无 color_primaries 选项)
#   +faststart 二次处理时更新 avcC box
H264_PRESETS = {
    "medium":  "h264_nvenc -preset p6 -cq 22 -rc vbr -b:v 12M -maxrate:v 18M "
               "-profile:v main -level:v 5.0 -pix_fmt yuv420p "
               "-rc-lookahead 20 -g 150 -dpb_size 2 -aud 1 "
               "-weighted_pred 0 -b_ref_mode 0 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 0",
    "high":    "h264_nvenc -preset p7 -cq 17 -rc vbr -b:v 30M -maxrate:v 45M "
               "-profile:v main -level:v 5.1 -pix_fmt yuv420p "
               "-rc-lookahead 32 -g 150 -dpb_size 3 -aud 1 "
               "-weighted_pred 0 -b_ref_mode 0 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 1",
    "ultra":   "h264_nvenc -preset p7 -cq 13 -rc vbr -b:v 80M -maxrate:v 120M "
               "-profile:v main -level:v 5.1 -pix_fmt yuv420p "
               "-rc-lookahead 48 -g 150 -dpb_size 3 -aud 1 "
               "-weighted_pred 0 -b_ref_mode 0 "
               "-spatial-aq 1 -temporal-aq 1 -multipass 2",
}

# H.264 专用: BSF 注入色彩元数据 (HEVC 不需要)
H264_COLOR_BSF = ('h264_metadata=colour_primaries=1:'
                  'transfer_characteristics=1:matrix_coefficients=1')


def record_screen(duration=10, output=None, quality="high", codec="hevc", fps=30):
    if not FFMPEG or not os.path.isfile(FFMPEG):
        return False, "FFmpeg 未安装: pip install imageio-ffmpeg"

    if output is None:
        ts = int(time.time())
        output = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              f"_record_{ts}.mp4")

    if codec == "h264":
        preset = H264_PRESETS.get(quality, H264_PRESETS["high"])
        bsf = f' -bsf:v {H264_COLOR_BSF}'
        # 2K@60 需 Level 5.1, 30fps 可用 5.0
        if fps >= 50 and 'level:v 5.0' in preset:
            preset = preset.replace('level:v 5.0', 'level:v 5.1')
    else:
        preset = HEVC_PRESETS.get(quality, HEVC_PRESETS["high"])
        bsf = ''

    # GOP = 5秒关键帧间隔
    gop = fps * 5
    preset = preset.replace('-g 150', f'-g {gop}')

    cmd = f'"{FFMPEG}" -y -f gdigrab -framerate {fps} -i desktop ' \
          f'-c:v {preset}{bsf} ' \
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
    parser.add_argument('--codec', default='hevc', choices=['hevc', 'h264'],
                        help='编码格式 (默认 hevc)')
    parser.add_argument('--fps', type=int, default=30, help='帧率 (默认 30, 最高 60)')
    args = parser.parse_args()

    fps = max(1, min(args.fps, 60))
    print(f"录制 {args.duration}s ({args.quality} 画质, {args.codec}, {fps}fps)...")
    ok, msg = record_screen(args.duration, args.output, args.quality, args.codec, fps)
    print(msg)
    sys.exit(0 if ok else 1)
