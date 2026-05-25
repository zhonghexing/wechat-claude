"""
微信 ClawBot 桥接系统 — 基于腾讯 iLink Bot API
通过微信 ClawBot 插件远程控制 Claude Code 执行任务 + 操作电脑。

与旧 wechat_bridge.py 的区别:
  - 无需 UIAutomation 操控桌面微信（不再依赖微信 UI）
  - 使用 HTTP 长轮询接收消息（不再轮询 UI 控件）
  - 服务端 cursor 天然去重（不再需要五层客户端去重）
  - QR 码扫码登录（不依赖桌面微信已登录）
  - 腾讯官方合法通道（有法律条款背书）

前置条件:
  - 微信需安装 ClawBot 插件（微信设置 → 插件 → ClawBot）
  - Python 3.8+, requests 库

用法:
  python wechat_clawbot_bridge.py              # 首次运行扫码登录
  python wechat_clawbot_bridge.py --config config.env
  python wechat_clawbot_bridge.py --reset      # 重新扫码登录
"""

import sys
import os
import io
import time
import json
import re
import hashlib
import base64
import struct
import subprocess
import logging
import logging.handlers
import argparse
import threading
import tempfile
from pathlib import Path

# ---------- 第三方依赖检查 ----------
try:
    import requests
except ImportError:
    print("请先安装: pip install requests")
    sys.exit(1)

# 可选依赖
try:
    import pyautogui as _pyautogui_available
    PYAUTOGUI = True
except ImportError:
    PYAUTOGUI = False

# ---------- 编码修复 ----------
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ---------- 常量 ----------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = 0x020404  # 2.4.4 → major<<16 | minor<<8 | patch
ILINK_BOT_TYPE = "3"
DEFAULT_BOT_AGENT = "ClaudeCode/1.0"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
SESSION_EXPIRED_ERRCODE = -14
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clawbot_state.json")

# ---------- 配置加载 ----------
DEFAULT_CONFIG = {
    'CTI_WORK_DIR': os.path.dirname(os.path.abspath(__file__)),
    'CTI_CLAUDE_CLI': r"C:\Users\zhx\Desktop\CC\nodejs\node-v20.11.0-win-x64\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
    'CTI_LOG_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'),
    'CTI_RUNTIME_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime'),
    'CTI_MAX_RESPONSE_LENGTH': '500',
    'CTI_CLAUDE_EFFORT': 'low',
    'CTI_CLAUDE_TIMEOUT': '300',
    'CTI_CLAUDE_PERMISSION_MODE': 'bypassPermissions',
    'CTI_CHECK_INTERVAL': '0.5',
}


def parse_env_file(filepath):
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
    config = dict(DEFAULT_CONFIG)
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
        config.update(parse_env_file(config_path))
    for key in config:
        env_val = os.environ.get(key)
        if env_val is not None:
            config[key] = env_val
    return config


# ---------- 日志 ----------
def setup_logging(log_dir, level=logging.INFO):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'clawbot_bridge.log')
    logger = logging.getLogger('clawbot_bridge')
    logger.setLevel(level)
    logger.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding='utf-8'
    )
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(ch)
    return logger


log = logging.getLogger('clawbot_bridge')


# ---------- 状态持久化 ----------
def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or '.', exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- iLink API 客户端 ----------
def random_wechat_uin():
    u32 = struct.unpack('>I', os.urandom(4))[0]
    return base64.b64encode(str(u32).encode()).decode()


def build_headers(token=None):
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_post(endpoint, body=None, token=None, timeout=15):
    url = f"{ILINK_BASE_URL}/{endpoint}"
    headers = build_headers(token)
    log.debug(f"POST {endpoint} body={json.dumps(body or {}, ensure_ascii=False)[:200]}")
    try:
        resp = requests.post(url, json=body or {}, headers=headers, timeout=timeout)
        if not resp.ok:
            log.error(f"{endpoint} HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.text else {}
    except requests.Timeout:
        log.debug(f"{endpoint}: timeout after {timeout}s")
        return {}
    except Exception as e:
        log.error(f"{endpoint}: {e}")
        return {}


def api_get(endpoint, timeout=35):
    url = f"{ILINK_BASE_URL}/{endpoint}"
    headers = build_headers()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        return resp.json() if resp.text else {}
    except requests.Timeout:
        return {"status": "wait"}
    except Exception as e:
        log.warn(f"GET {endpoint}: {e}")
        return {"status": "wait"}


# ---------- QR 码登录 ----------
def get_qr_code(token_list=None):
    """获取登录二维码，返回 {qrcode, qrcode_img_content}"""
    body = {"local_token_list": token_list or []}
    return api_post("ilink/bot/get_bot_qrcode?bot_type=" + ILINK_BOT_TYPE, body)


def poll_qr_status(qrcode, verify_code=None):
    """长轮询扫码状态"""
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={qrcode}"
    if verify_code:
        endpoint += f"&verify_code={verify_code}"
    return api_get(endpoint, timeout=35)


def display_qr_terminal(qrcode_url):
    """在终端显示二维码和备用链接"""
    try:
        import qrcode as _qr
        qr = _qr.QRCode()
        qr.add_data(qrcode_url)
        qr.print_ascii()
    except ImportError:
        pass
    print(f"\n若二维码无法显示，请访问以下链接:\n{qrcode_url}\n")


def do_login():
    """执行 QR 码登录流程，返回 {bot_token, ilink_bot_id, ilink_user_id, baseurl}"""
    # 尝试复用已有 token
    state = load_state()
    known_tokens = []
    for account_id, data in state.get("accounts", {}).items():
        if data.get("token"):
            known_tokens.append(data["token"])

    print("正在获取登录二维码...")
    result = get_qr_code(known_tokens[-10:] if known_tokens else [])
    qrcode = result.get("qrcode")
    qrcode_url = result.get("qrcode_img_content")

    if not qrcode or not qrcode_url:
        print(f"获取二维码失败: {result}")
        return None

    print("\n" + "=" * 50)
    print("请用手机微信扫描以下二维码登录:")
    print("=" * 50)
    display_qr_terminal(qrcode_url)

    print("等待扫码...")
    scanned_printed = False
    refresh_count = 0
    pending_verify = None

    while True:
        status = poll_qr_status(qrcode, pending_verify)
        st = status.get("status", "wait")
        log.debug(f"QR status: {st}")

        if st == "wait":
            if not scanned_printed:
                print(".", end="", flush=True)
        elif st == "scaned":
            pending_verify = None
            if not scanned_printed:
                print("\n正在验证...")
                scanned_printed = True
        elif st == "need_verifycode":
            code = input("\n请输入手机微信显示的数字: ").strip()
            pending_verify = code
            continue
        elif st == "expired":
            refresh_count += 1
            if refresh_count > 3:
                print("\n二维码多次过期，请重试")
                return None
            print(f"\n二维码已过期，正在刷新 ({refresh_count}/3)...")
            result = get_qr_code(known_tokens[-10:] if known_tokens else [])
            qrcode = result.get("qrcode")
            qrcode_url = result.get("qrcode_img_content")
            if qrcode and qrcode_url:
                display_qr_terminal(qrcode_url)
                scanned_printed = False
                print("请重新扫描...")
        elif st == "verify_code_blocked":
            print("\n多次输入错误，请稍后再试")
            refresh_count += 1
            if refresh_count > 3:
                return None
        elif st == "binded_redirect":
            print("\n已连接过此 ClawBot，无需重复连接")
            return {"already_connected": True}
        elif st == "confirmed":
            print("\n✅ 登录成功！")
            return {
                "bot_token": status.get("bot_token"),
                "ilink_bot_id": status.get("ilink_bot_id"),
                "ilink_user_id": status.get("ilink_user_id"),
                "baseurl": status.get("baseurl") or ILINK_BASE_URL,
            }

        time.sleep(1)


# ---------- 消息收发 ----------
def get_updates(token, get_updates_buf="", timeout_ms=DEFAULT_LONG_POLL_TIMEOUT_MS):
    body = {
        "get_updates_buf": get_updates_buf,
        "base_info": {
            "channel_version": "2.4.4",
            "bot_agent": DEFAULT_BOT_AGENT,
        }
    }
    return api_post("ilink/bot/getupdates", body, token=token, timeout=max(timeout_ms // 1000, 10))


def send_message(token, to_user_id, text, context_token=None):
    """发送文本消息"""
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"clawbot-{os.urandom(8).hex()}",
            "message_type": 2,  # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context_token or "",
        },
        "base_info": {
            "channel_version": "2.4.4",
            "bot_agent": DEFAULT_BOT_AGENT,
        }
    }
    return api_post("ilink/bot/sendmessage", body, token=token)


def send_typing(token, ilink_user_id, typing_ticket, status=1):
    """发送'正在输入'状态 (1=typing, 2=cancel)"""
    body = {
        "ilink_user_id": ilink_user_id,
        "typing_ticket": typing_ticket,
        "status": status,
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    return api_post("ilink/bot/sendtyping", body, token=token, timeout=10)


# ---------- CDN 媒体上传 (AES-128-ECB 加密) ----------

def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密（无填充，数据长度必须是 16 的倍数）"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _pad_pkcs7(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _unpad_pkcs7(data: bytes) -> bytes:
    """移除 PKCS7 填充（返回去除尾部填充的原始数据）"""
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        return data  # 非 PKCS7，原样返回
    return data[:-pad_len]


def _download_cdn_media(media: dict, base_url: str) -> bytes | None:
    """从 CDN 下载加密媒体 → AES-128-ECB 解密 → 返回原始 bytes"""
    filekey = media.get("filekey", "")
    param = media.get("encrypt_query_param", "")
    aeskey_str = media.get("aeskey", "")
    if not filekey or not param or not aeskey_str:
        missing = []
        if not filekey: missing.append("filekey")
        if not param: missing.append("encrypt_query_param")
        if not aeskey_str: missing.append("aeskey")
        log.warning(f"_download_cdn_media: 缺少字段 {missing}, media_keys={list(media.keys())}")
        return None

    # aeskey 兼容 hex（32 字符）和 base64（24 字符）两种编码
    aes_key = None
    if len(aeskey_str) == 32:
        try:
            aes_key = bytes.fromhex(aeskey_str)
        except ValueError:
            pass
    if aes_key is None:
        try:
            aes_key = base64.b64decode(aeskey_str)
        except Exception:
            pass
    if aes_key is None or len(aes_key) != 16:
        log.error(f"_download_cdn_media: 无法解析 aeskey (len={len(aeskey_str)})")
        return None

    # 构造下载 URL
    cdn_base = base_url.replace("ilinkai.weixin.qq.com", "cdn.ilinkai.weixin.qq.com")
    dl_url = f"{cdn_base}/download?encrypted_query_param={param}&filekey={filekey}"

    try:
        resp = requests.get(dl_url, timeout=30)
        if resp.status_code != 200:
            log.error(f"CDN download failed: {resp.status_code}")
            return None
        encrypted = resp.content
    except Exception as e:
        log.error(f"CDN download exception: {e}")
        return None

    # AES-128-ECB 解密 + 去填充
    try:
        decrypted = _aes_ecb_decrypt(encrypted, aes_key)
        raw = _unpad_pkcs7(decrypted)
        log.info(f"CDN download OK: filekey={filekey}, size={len(raw)}")
        return raw
    except Exception as e:
        log.error(f"AES decrypt failed: {e}")
        return None


def _download_and_save_image(media: dict, work_dir: str, base_url: str) -> str | None:
    """下载 CDN 图片并保存到本地，返回文件路径"""
    raw = _download_cdn_media(media, base_url)
    if raw is None:
        return None

    ts = int(time.time())
    rand = os.urandom(4).hex()
    filename = f"_wechat_img_{ts}_{rand}.png"
    filepath = os.path.join(work_dir, filename)
    try:
        with open(filepath, 'wb') as f:
            f.write(raw)
        log.info(f"图片已保存: {filepath}")
        return filepath
    except Exception as e:
        log.error(f"保存图片失败: {e}")
        return None


def _upload_file_to_cdn(filepath: str, token: str, to_user_id: str,
                         media_type: int, base_url: str) -> dict | None:
    """
    CDN 上传流程：AES-128-ECB 加密 → getUploadUrl → HTTP POST → 返回 CDN 引用。
    media_type: 1=IMAGE, 2=VIDEO, 4=VOICE, 3=FILE
    返回: {filekey, aeskey(hex), file_size, file_size_ciphertext, download_param}
    """
    import hashlib as _hashlib

    with open(filepath, 'rb') as f:
        raw_data = f.read()

    raw_size = len(raw_data)
    raw_md5_hex = _hashlib.md5(raw_data).digest().hex()  # hex 格式！

    # AES-128-ECB 加密（PKCS7 填充，Node.js createCipheriv 行为）
    aes_key = os.urandom(16)
    padded = _pad_pkcs7(raw_data, 16)
    encrypted = _aes_ecb_encrypt(padded, aes_key)
    enc_size = len(encrypted)

    # getUploadUrl — aeskey 用 hex，rawfilemd5 用 hex
    filekey = os.urandom(16).hex()
    aeskey_hex = aes_key.hex()

    req = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": raw_size,
        "rawfilemd5": raw_md5_hex,
        "filesize": enc_size,
        "aeskey": aeskey_hex,
        "no_need_thumb": True,
    }
    resp = api_post("ilink/bot/getuploadurl", req, token=token, timeout=15)
    upload_url = resp.get("upload_full_url", "")
    upload_param = resp.get("upload_param", "")

    if not upload_url and not upload_param:
        log.error(f"getUploadUrl failed (no URL): {resp}")
        return None
    if not upload_url:
        # fallback: 拼接 CDN URL
        cdn_base = base_url.replace("ilinkai.weixin.qq.com", "cdn.ilinkai.weixin.qq.com")
        upload_url = f"{cdn_base}/upload?encrypted_query_param={upload_param}&filekey={filekey}"
        log.debug(f"CDN upload URL fallback via upload_param")

    # HTTP POST 上传加密文件（Content-Type: application/octet-stream）
    try:
        put_resp = requests.post(upload_url, data=encrypted,
                                 headers={"Content-Type": "application/octet-stream"},
                                 timeout=30)
        if put_resp.status_code not in (200, 201, 204):
            log.error(f"CDN POST failed: {put_resp.status_code} {put_resp.text[:200]}")
            return None
        # 下载参数从响应头 x-encrypted-param 获取
        download_param = put_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            log.error("CDN response missing x-encrypted-param header")
            return None
    except Exception as e:
        log.error(f"CDN POST exception: {e}")
        return None

    log.info(f"CDN upload OK: {filepath} ({raw_size} bytes) -> {filekey}")
    return {
        "filekey": filekey,
        "aeskey": aeskey_hex,
        "file_size": raw_size,
        "file_size_ciphertext": enc_size,
        "download_param": download_param,
    }


def send_image_message(token, to_user_id, filepath, context_token=None):
    """上传图片到 CDN 并通过微信发送"""
    result = _upload_file_to_cdn(filepath, token, to_user_id, 1, ILINK_BASE_URL)
    if not result:
        log.error(f"send_image_message: CDN upload failed for {filepath}")
        return False

    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"clawbot-{os.urandom(8).hex()}",
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token or "",
            "item_list": [{
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": result["download_param"],
                        "aes_key": base64.b64encode(result["aeskey"].encode()).decode(),
                        "encrypt_type": 1,
                    },
                    "mid_size": result["file_size_ciphertext"],
                }
            }],
        },
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    resp = api_post("ilink/bot/sendmessage", body, token=token)
    log.info(f"send_image_message: {filepath} -> WeChat, resp={resp}")
    return True


def send_file_message(token, to_user_id, filepath, context_token=None):
    """上传文件到 CDN 并通过微信发送（自动判断视频用 video_item）"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm'):
        return _send_video_message(token, to_user_id, filepath, context_token)
    fname = os.path.basename(filepath)
    result = _upload_file_to_cdn(filepath, token, to_user_id, 3, ILINK_BASE_URL)
    if not result:
        log.error(f"send_file_message: CDN upload failed for {filepath}")
        return False
    body = {
        "msg": {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": f"clawbot-{os.urandom(8).hex()}",
            "message_type": 2, "message_state": 2,
            "context_token": context_token or "",
            "item_list": [{
                "type": 4,
                "file_item": {
                    "media": {
                        "encrypt_query_param": result["download_param"],
                        "aes_key": base64.b64encode(result["aeskey"].encode()).decode(),
                        "encrypt_type": 1,
                    },
                    "file_name": fname,
                    "len": str(result["file_size"]),
                }
            }],
        },
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    resp = api_post("ilink/bot/sendmessage", body, token=token)
    log.info(f"send_file_message: {filepath} -> WeChat, resp={resp}")
    return True


def _send_video_message(token, to_user_id, filepath, context_token=None):
    """发送视频 — 用 video_item (type=5)，微信原生播放器"""
    result = _upload_file_to_cdn(filepath, token, to_user_id, 2, ILINK_BASE_URL)  # VIDEO=2
    if not result:
        log.error(f"_send_video_message: CDN upload failed for {filepath}")
        return False
    body = {
        "msg": {
            "from_user_id": "", "to_user_id": to_user_id,
            "client_id": f"clawbot-{os.urandom(8).hex()}",
            "message_type": 2, "message_state": 2,
            "context_token": context_token or "",
            "item_list": [{
                "type": 5,
                "video_item": {
                    "media": {
                        "encrypt_query_param": result["download_param"],
                        "aes_key": base64.b64encode(result["aeskey"].encode()).decode(),
                        "encrypt_type": 1,
                    },
                    "video_size": result["file_size_ciphertext"],
                }
            }],
        },
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    resp = api_post("ilink/bot/sendmessage", body, token=token)
    log.info(f"_send_video_message: {filepath} -> WeChat, resp={resp}")
    return True


def get_config(token, ilink_user_id, context_token=None):
    body = {
        "ilink_user_id": ilink_user_id,
        "context_token": context_token or "",
        "base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT},
    }
    return api_post("ilink/bot/getconfig", body, token=token, timeout=10)


def notify_start(token):
    body = {"base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT}}
    return api_post("ilink/bot/msg/notifystart", body, token=token, timeout=10)


def notify_stop(token):
    body = {"base_info": {"channel_version": "2.4.4", "bot_agent": DEFAULT_BOT_AGENT}}
    return api_post("ilink/bot/msg/notifystop", body, token=token, timeout=10)


# ---------- 消息解析 ----------
def extract_text(item_list, work_dir=".", base_url=ILINK_BASE_URL):
    """从 item_list 中提取纯文本，图片自动下载到本地"""
    if not item_list:
        return ""
    for item in item_list:
        if item.get("type") == 1:  # TEXT
            return item.get("text_item", {}).get("text", "")
        if item.get("type") == 3:  # VOICE (含语音转文字)
            text = item.get("voice_item", {}).get("text", "")
            if text:
                return f"[语音] {text}"
        if item.get("type") == 2:  # IMAGE
            image_item = item.get("image_item", {})
            log.info(f"[DEBUG] IMAGE item keys={list(image_item.keys())}, raw={str(image_item)[:300]}")
            media = image_item.get("media", {})
            if media:
                log.info(f"[DEBUG] media keys={list(media.keys())}, filekey={media.get('filekey','')[:30]}")
            fp = _download_and_save_image(media, work_dir, base_url)
            if fp:
                return f"[图片] __FILE__:{fp}"
            return "[图片]"
        if item.get("type") == 5:  # VIDEO
            return "[视频]"
        if item.get("type") == 4:  # FILE
            return f"[文件] {item.get('file_item', {}).get('file_name', '')}"
    return ""


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


# ---------- 本地指令调度 ----------
def do_screenshot(work_dir):
    """用 PowerShell 高DPI感知 + GDI CopyFromScreen 截图，物理分辨率全像素捕获"""
    ts = int(time.time())
    rand = os.urandom(4).hex()
    path = os.path.join(work_dir, f"_screenshot_{ts}_{rand}.png")
    # 清理旧截图（只清理非当前文件）
    for f in os.listdir(work_dir):
        if f.startswith("_screenshot") and f.endswith(".png") and f != os.path.basename(path):
            try:
                os.remove(os.path.join(work_dir, f))
            except:
                pass

    ps_name = f"_clawbot_scr_{rand}.ps1"
    ps_path = os.path.join(tempfile.gettempdir(), ps_name)
    # SetProcessDPIAware → Bounds 返回物理像素（解决高DPI缩放模糊）
    with open(ps_path, 'w', encoding='utf-8-sig') as f:
        f.write(f'''Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DPI {{
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
}}
"@
[DPI]::SetProcessDPIAware() | Out-Null
Start-Sleep -Milliseconds 200
$s = [System.Windows.Forms.Screen]::PrimaryScreen
$b = New-Object System.Drawing.Bitmap($s.Bounds.Width, $s.Bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($b)
$g.CopyFromScreen($s.Bounds.X, $s.Bounds.Y, 0, 0, $s.Bounds.Size)
$g.Dispose()
$b.Save("{path}", [System.Drawing.Imaging.ImageFormat]::Png)
$b.Dispose()
''')

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 6
    r = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps_path],
        capture_output=True, text=True, timeout=15,
        cwd=work_dir, encoding='utf-8', errors='replace',
        creationflags=0x00000010,
        startupinfo=startupinfo,
    )
    try:
        os.remove(ps_path)
    except:
        pass

    if r.returncode == 0 and os.path.isfile(path):
        return path
    log.error(f"截图失败: {r.stderr[:200]}")
    return None


def _send_via_uia(filepath, chat_name='文件传输助手'):
    """通过桌面微信 UIAutomation + CF_HDROP 剪贴板发送文件"""
    try:
        import uiautomation as auto
        import ctypes
    except ImportError:
        log.warn("uiautomation 未安装，无法通过桌面微信发送")
        return False

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
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    hMem = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
    ptr = kernel32.GlobalLock(hMem)
    df = DROPFILES()
    df.pFiles = df_size
    df.fWide = 1
    ctypes.memmove(ptr, ctypes.addressof(df), df_size)
    ctypes.memmove(ptr + df_size, filedata, len(filedata))
    kernel32.GlobalUnlock(ctypes.c_void_p(hMem))
    user32.SetClipboardData(CF_HDROP, ctypes.c_void_p(hMem))
    user32.CloseClipboard()

    # 找到微信窗口并打开聊天
    wechat = None
    for name in ['微信', 'Weixin', 'WeChat']:
        w = auto.WindowControl(Name=name, ClassName='mmui::MainWindow', searchDepth=1)
        if w.Exists(0, 0.5):
            wechat = w
            break
    if not wechat:
        return False

    # 确保窗口可见
    if ctypes.windll.user32.IsIconic(wechat.NativeWindowHandle):
        ctypes.windll.user32.ShowWindow(wechat.NativeWindowHandle, 9)
    wechat.SetActive()
    time.sleep(0.2)

    # 关闭已打开的独立聊天窗口
    standalone = auto.WindowControl(Name=chat_name)
    if standalone.Exists(0, 0.3):
        try:
            close_btn = standalone.Control(Name='关闭')
            if close_btn.Exists(0, 0.3):
                close_btn.Click()
                time.sleep(0.1)
        except:
            pass

    # 检查是否已打开
    def _chat_open():
        inp = wechat.Control(AutomationId='chat_input_field')
        return inp.Exists(0, 0.3) and chat_name in (inp.Name or '')

    if not _chat_open():
        # 点击微信标签页
        chat_tab = wechat.Control(Name='微信', ClassName='mmui::XTabBarItem')
        if chat_tab.Exists(0, 0.2):
            chat_tab.Click()
            time.sleep(0.1)

        if not _chat_open():
            cell = wechat.Control(AutomationId=f"session_item_{chat_name}")
            if cell.Exists(0, 1):
                cell.Click()
                time.sleep(0.5)
                # 点出独立窗口？关掉重来
                if not _chat_open():
                    s2 = auto.WindowControl(Name=chat_name)
                    if s2.Exists(0, 0.3):
                        try:
                            s2.Control(Name='关闭').Click()
                        except:
                            pass
                        time.sleep(0.3)
                        wechat.SetActive()
                        c2 = wechat.Control(AutomationId=f"session_item_{chat_name}")
                        if c2.Exists(0, 1):
                            c2.Click()
                            time.sleep(0.8)
            else:
                log.warn(f"UIA 未找到联系人: {chat_name}")
                return False

    # Ctrl+V + Enter 发送
    auto.SendKeys('{Ctrl}v')
    time.sleep(0.2)
    auto.SendKeys('{Enter}')
    return True


def dispatch_local(cmd, work_dir):
    """本地快捷操作（秒级响应，不走 Claude），返回 (handled, result)"""
    cmd_lower = cmd.lower().strip()

    # 截图 — 本地处理，秒级响应。但"删截图/清理截图"等管理操作交给 Claude 思考
    SCREENSHOT_KW = ['截图', '截屏', '截个图', '屏幕截图', 'screenshot']
    is_screenshot = any(kw in cmd_lower for kw in SCREENSHOT_KW)
    # 排除非截屏意图：删截图、清理截图、移动截图等
    SCREENSHOT_NEGATIVE = ['删', '清理', '清除', '去掉', '移出', '移动', '整理', '管理', '多余']
    is_negative = any(kw in cmd for kw in SCREENSHOT_NEGATIVE)
    needs_analysis = any(kw in cmd for kw in ['分析', '识别', '图片里', '图片中', '图里', '上面写了', '看看'])
    if is_screenshot and not needs_analysis and not is_negative:
        # 前置操作：最小化窗口
        if '最小化' in cmd or '隐藏' in cmd:
            import ctypes
            ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)  # Win
            ctypes.windll.user32.keybd_event(0x4D, 0, 0, 0)  # M
            ctypes.windll.user32.keybd_event(0x4D, 0, 2, 0)  # M up
            ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)  # Win up
            time.sleep(0.5)
        path = do_screenshot(work_dir)
        if not path:
            return True, "截图失败，请重试"
        sz = os.path.getsize(path)
        # 如果用户提到"文件传输助手"，走桌面微信 UIA 粘贴发送
        if '文件传输助手' in cmd:
            ok = _send_via_uia(path, chat_name='文件传输助手')
            if ok:
                return True, f"已截图并通过文件传输助手发送 ({sz//1024}KB)"
            else:
                return True, f"截图完成但UIA发送失败，通过ClawBot发送:\n__FILE__:{path}"
        return True, f"截图完成 ({sz//1024}KB)\n__FILE__:{path}"

    # 酷狗音乐播放
    if ('酷狗' in cmd_lower or 'kugou' in cmd_lower) and '播放' in cmd_lower:
        MIXED_OPS = ['然后', '接着', '截图', '发给', '发送', '发图片', '并且', '还有', '以及', '之后', '完了', '后再']
        if any(kw in cmd for kw in MIXED_OPS):
            return False, None

        m = re.search(r'播放\s*(.+?)(?:\s*stop)?\s*$', cmd, re.IGNORECASE)
        if m:
            song = m.group(1).strip()
            song = re.sub(r'[歌曲音乐]', '', song).strip()
        else:
            song = cmd.split('播放')[-1].strip()

        if song:
            try:
                kugou_path = os.path.join(work_dir, 'kugou_play.py')
                r = subprocess.run(
                    ['python', '-u', kugou_path, song],
                    capture_output=True, text=True, timeout=30,
                    cwd=work_dir, encoding='utf-8', errors='replace',
                    creationflags=0x08000000,
                )
                return True, f"酷狗: {song}\n{r.stdout.strip()[-300:] if r.stdout else '(无输出)'}"
            except Exception as e:
                return True, f"酷狗播放失败: {e}"

    return False, None


# ---------- Claude 调用 ----------
_session_started = False
_prime_proc = None
_my_claude_pids = []


def track_claude_pid(pid):
    if pid and pid not in _my_claude_pids:
        _my_claude_pids.append(pid)


def untrack_claude_pid(pid):
    if pid in _my_claude_pids:
        _my_claude_pids.remove(pid)


def kill_my_claude():
    global _prime_proc
    if _prime_proc:
        try:
            _prime_proc.kill()
        except:
            pass
        _prime_proc = None
    for pid in list(_my_claude_pids):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0001, False, pid)
            if h:
                kernel32.TerminateProcess(h, 0)
                kernel32.CloseHandle(h)
        except:
            pass
    _my_claude_pids.clear()


def reset_session():
    global _session_started, _prime_proc
    _session_started = False
    if _prime_proc:
        try:
            _prime_proc.kill()
        except:
            pass
        untrack_claude_pid(_prime_proc.pid)
        _prime_proc = None


def warmup_claude(config):
    """后台预热 Claude 进程，下次消息秒级响应"""
    global _session_started, _prime_proc
    if _prime_proc is not None and _prime_proc.poll() is None:
        return  # 已有预热在跑
    try:
        cli = config.get('CTI_CLAUDE_CLI', '')
        work_dir = config.get('CTI_WORK_DIR', '')
        model = config.get('CTI_CLAUDE_MODEL', '')
        effort = config.get('CTI_CLAUDE_EFFORT', 'low')
        perm = config.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions')
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6
        args = [cli, '-p', '回复OK', '--permission-mode', perm, '--effort', effort]
        if model:
            args += ['--model', model]
        _prime_proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=work_dir, creationflags=0x08000000, startupinfo=startupinfo,
        )
        track_claude_pid(_prime_proc.pid)
        log.debug("后台预热 Claude 进程...")
    except Exception as e:
        log.debug(f"预热失败: {e}")


def call_claude(prompt, config):
    """调用 Claude Code CLI，自动保持会话连贯"""
    global _session_started, _prime_proc

    cli = config.get('CTI_CLAUDE_CLI', '')
    work_dir = config.get('CTI_WORK_DIR', os.path.dirname(os.path.abspath(__file__)))
    model = config.get('CTI_CLAUDE_MODEL', '')
    effort = config.get('CTI_CLAUDE_EFFORT', 'low')
    perm = config.get('CTI_CLAUDE_PERMISSION_MODE', 'bypassPermissions')
    timeout_s = int(config.get('CTI_CLAUDE_TIMEOUT', '300'))
    max_len = int(config.get('CTI_MAX_RESPONSE_LENGTH', '500'))

    # 处理预热进程：已完成则复用，未完成则等待（不杀！）
    had_warmup = False
    if _prime_proc is not None:
        if _prime_proc.poll() is None:
            # 预热还在跑，等最多 15 秒
            try:
                _prime_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _prime_proc.kill()
                untrack_claude_pid(_prime_proc.pid)
                _prime_proc = None
        if _prime_proc is not None:
            _session_started = True
            had_warmup = True
            untrack_claude_pid(_prime_proc.pid)
            _prime_proc = None

    # 文件快照
    existing_files = {}
    try:
        for f in os.listdir(work_dir):
            fp = os.path.join(work_dir, f)
            if os.path.isfile(fp):
                existing_files[f] = os.path.getmtime(fp)
    except:
        pass

    try:
        if _session_started:
            args = [cli, '--continue', '-p', prompt, '--permission-mode', perm, '--effort', effort]
        else:
            args = [cli, '-p', prompt, '--permission-mode', perm, '--effort', effort]
            _session_started = True
        if model:
            args += ['--model', model]

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6

        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            cwd=work_dir,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — 更快
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

        # 检测新文件和被修改的媒体文件（用 mtime 比对），不发送脚本/工作文件
        MEDIA_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico',
                      '.mp4', '.mp3', '.wav', '.avi', '.mov', '.pdf',
                      '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar'}
        try:
            for f in os.listdir(work_dir):
                fp = os.path.join(work_dir, f)
                if not os.path.isfile(fp):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext not in MEDIA_EXTS:
                    continue
                prev_mtime = existing_files.get(f)
                if prev_mtime is None:
                    log.info(f"新文件: {f}")
                    output += f"\n__FILE__:{fp}"
                elif os.path.getmtime(fp) > prev_mtime + 1:
                    log.info(f"文件已更新: {f}")
                    output += f"\n__FILE__:{fp}"
        except:
            pass

        return output[:max_len + 500]
    except FileNotFoundError:
        return f"(未找到 Claude CLI: {cli})"
    except Exception as e:
        return f"(调用失败: {e})"


# ---------- Claude 系统提示词 ----------
SYSTEM_PROMPT = """# 系统角色：你的个人电脑助手
你现在拥有完全控制这台电脑的能力，可以通过ClawBot执行以下操作：
- 控制鼠标移动、点击、拖拽、滚动
- 模拟键盘输入、快捷键
- 截取屏幕、识别屏幕内容
- 执行Windows命令行(cmd/powershell)命令
- 读写本地文件、创建文件夹
- 打开、关闭、切换应用程序
- 查看系统信息、进程列表

你的工具箱:
- Bash工具: 执行任何Windows命令、Python脚本、PowerShell脚本
- 发送消息/文件: **默认通过 ClawBot 自动回复到你的微信**（无需手动操作）
  - 响应会自动通过 ClawBot 发回，文件用 __FILE__: 标记即可自动上传
- 备用通道（仅当你说"通过文件传输助手"时使用）:
  python D:\\claude自动\\wechat_send_to.py text "内容" ["联系人"]  发文字（走桌面微信UIA）
  python D:\\claude自动\\wechat_send_to.py file "路径" ["联系人"]  发文件（走桌面微信UIA）
- 截图: 用户说"截图"时直接回复文字告知截图完成，桥接自动处理并发送图片，无需你手动操作
- 录屏: python D:\\claude自动\\screen_record_nvenc.py -d <秒数> -q <medium|high|ultra>（NVENC硬件编码，高清）
- 酷狗: "酷狗播放XXX"自动播放
- 快捷键: Win+D=桌面 Win+M=最小化全部
- 锁屏: rundll32.exe user32.dll,LockWorkStation
- 关机: shutdown /s /t 60 取消: shutdown /a

## 重要规则 — 关于文件发送
1. **禁止发送脚本文件**：绝对不要把你创建的辅助工具脚本(.py .ps1 .bat)通过 __FILE__: 发给用户。这些是自己用的工具，不是给用户的交付物。
2. **只发交付物**：只有用户明确要求的文件（图片、视频、文档等）才用 __FILE__: 标记发送。不要自作主张发代码文件。
3. **代码改完自己测试**：修改了桥接代码后，自己运行 python D:\\claude自动\\wechat_clawbot_bridge.py 测试是否正常，确认无误后告知用户即可，不要把代码文件发给用户。
4. **默认用 ClawBot 通道**：用 __FILE__: 标记自动走 CDN 发送，不需额外操作。除非用户明确说"通过文件传输助手"，否则不要用 wechat_send_to.py。

## 核心工作原则
1. **闭环执行**：任何任务都必须形成"规划→执行→验证→调试→完成"的完整闭环
2. **自我调试**：如果执行失败，不要向用户求助，自己分析原因，尝试不同的解决方案，最多重试5次
3. **最小干预**：尽量自己完成所有步骤，只有遇到无法解决的致命问题时才向用户报告
4. **安全第一**：绝对不能删除系统文件、格式化磁盘、修改注册表/密码、下载运行未知exe
5. **进度汇报**：每完成一个重要步骤简要汇报，遇到问题说明原因和尝试的方案

## 执行流程
1. **任务分解**：把任务分解成最多5个清晰的子任务，按顺序编号
2. **环境检查**：先检查需要的软件、文件、环境是否存在
3. **分步执行**：一个一个执行子任务，每执行完一个验证结果
4. **错误处理**：失败就分析原因，尝试至少3种不同方案，全部失败才报告用户
5. **最终验证**：全面验证任务是否达到预期效果
6. **结果总结**：简要汇报完成情况

## 输出格式
- 步骤用数字编号，重要信息加粗
- 命令用代码块包裹
- 不要多余寒暄，直接汇报进度和结果

## 特殊指令
- "继续"=执行下一步 "停止"=立即停止 "检查"=重新验证 "重试"=重做上一步

现在等待任务指令。
"""


# ---------- 主循环 ----------
def run_bridge(config, reset_login=False):
    global _session_started

    work_dir = config.get('CTI_WORK_DIR', os.path.dirname(os.path.abspath(__file__)))
    log_dir = config.get('CTI_LOG_DIR', os.path.join(work_dir, 'logs'))
    runtime_dir = config.get('CTI_RUNTIME_DIR', os.path.join(work_dir, 'runtime'))
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)

    # 日志和 API URL 设置到模块级
    global log, ILINK_BASE_URL
    log = setup_logging(log_dir)

    # 加载/获取登录态
    state = load_state()
    account_id = state.get("default_account", "")
    accounts = state.get("accounts", {})

    if reset_login or not account_id or account_id not in accounts:
        print("需要扫码登录...")
        login_result = do_login()
        if not login_result:
            print("登录失败")
            return
        if login_result.get("already_connected"):
            print("已连接，使用现有凭据")
        else:
            account_id = login_result.get("ilink_bot_id", "default")
            accounts[account_id] = {
                "token": login_result["bot_token"],
                "baseurl": login_result.get("baseurl", ILINK_BASE_URL),
                "ilink_user_id": login_result.get("ilink_user_id", ""),
                "get_updates_buf": "",
                "typing_ticket": "",
            }
            state["default_account"] = account_id
            state["accounts"] = accounts
            save_state(state)

    account = accounts.get(account_id, {})
    token = account.get("token", "")
    base_url = account.get("baseurl", ILINK_BASE_URL)
    ilink_user_id = account.get("ilink_user_id", "")
    get_updates_buf = account.get("get_updates_buf", "")
    typing_ticket = account.get("typing_ticket", "")

    if not token:
        print("未找到登录凭据，请用 --reset 重新登录")
        return

    # 更新 base URL（可能因 IDC 重定向变化）
    ILINK_BASE_URL = base_url

    # 通知服务端启动
    try:
        notify_start(token)
    except Exception as e:
        log.warn(f"notifyStart failed (ignored): {e}")

    # 获取 typing_ticket
    if not typing_ticket and ilink_user_id:
        try:
            cfg = get_config(token, ilink_user_id)
            typing_ticket = cfg.get("typing_ticket", "")
            if typing_ticket:
                account["typing_ticket"] = typing_ticket
                accounts[account_id] = account
                state["accounts"] = accounts
                save_state(state)
        except:
            pass

    print("\n" + "=" * 60)
    print("WeChat ClawBot <-> Claude 桥接系统")
    print(f"  API: {ILINK_BASE_URL}")
    print(f"  账号: {account_id}")
    print(f"  用户: {ilink_user_id}")
    print(f"  工作目录: {work_dir}")
    print(f"  日志目录: {log_dir}")
    print()
    print("运行模式:")
    print("  - 直接发消息 → Claude 执行并回复")
    print("  - 发 '重置' 清空会话上下文")
    print("按 Ctrl+C 停止")
    print("=" * 60)
    print()

    # 状态变量
    conv_mode = False
    cooldown_until = 0
    recent_hashes = {}
    next_timeout_ms = DEFAULT_LONG_POLL_TIMEOUT_MS
    consecutive_failures = 0
    abort_flag = threading.Event()

    # 写运行时状态
    status_path = os.path.join(runtime_dir, 'status.json')
    def write_status(running=True, **extra):
        try:
            s = {"running": running, "account_id": account_id, "ilink_user_id": ilink_user_id,
                 "conv_mode": conv_mode, "timestamp": time.time(), **extra}
            with open(status_path, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False)
        except:
            pass

    write_status()

    print("正在启动消息监听...\n")
    print("后台预热 Claude (首条消息秒级响应)...")
    warmup_claude(config)

    try:
        while not abort_flag.is_set():
            try:
                log.debug(f"getUpdates: buf_len={len(get_updates_buf)}, timeout_ms={next_timeout_ms}")
                resp = get_updates(token, get_updates_buf, next_timeout_ms)

                # 处理长轮询超时（正常）
                if resp.get("ret", 0) != 0:
                    errcode = resp.get("errcode", 0)
                    if errcode == SESSION_EXPIRED_ERRCODE:
                        log.error("会话过期(errcode=-14)，暂停60分钟后重试")
                        print("\n⚠ 会话过期，请重新扫码登录 (python wechat_clawbot_bridge.py --reset)")
                        abort_flag.set()
                        break
                    consecutive_failures += 1
                    log.warn(f"getUpdates errcode={errcode} ({consecutive_failures}/3)")
                    if consecutive_failures >= 3:
                        log.error("连续3次失败，等待30秒后重试")
                        time.sleep(30)
                        consecutive_failures = 0
                    else:
                        time.sleep(2)
                    continue

                consecutive_failures = 0

                # 更新服务器建议的轮询间隔
                if resp.get("longpolling_timeout_ms"):
                    next_timeout_ms = resp["longpolling_timeout_ms"]

                # 保存 cursor
                new_buf = resp.get("get_updates_buf", "")
                if new_buf and new_buf != get_updates_buf:
                    get_updates_buf = new_buf
                    account["get_updates_buf"] = new_buf
                    accounts[account_id] = account
                    state["accounts"] = accounts
                    save_state(state)

                # 处理消息
                msgs = resp.get("msgs", [])
                for msg in msgs:
                    from_user = msg.get("from_user_id", "")
                    context_token = msg.get("context_token", "")

                    # 更新 ilink_user_id
                    if from_user and from_user != ilink_user_id:
                        ilink_user_id = from_user
                        account["ilink_user_id"] = from_user
                        accounts[account_id] = account
                        state["accounts"] = accounts
                        save_state(state)

                    text = extract_text(msg.get("item_list", []), work_dir, ILINK_BASE_URL)
                    if not text:
                        continue

                    log.info(f"收到: {text[:100]} from={from_user} msg_id={msg.get('message_id','?')}")

                    # 用 message_id 去重（服务端 at-least-once 投递，需客户端幂等）
                    msg_id = msg.get('message_id')
                    dedup_key = str(msg_id) if msg_id is not None else hashlib.md5(text.encode()).hexdigest()
                    if dedup_key in recent_hashes:
                        log.debug(f"跳过重复: key={dedup_key[:20]}")
                        continue
                    recent_hashes[dedup_key] = time.time() + 60

                    # 冷却期检查
                    now = time.time()
                    if now < cooldown_until:
                        continue

                    # 系统消息过滤
                    if text.startswith("(已") or text.startswith("酷狗:"):
                        continue

                    # 处理特殊命令
                    cmd_lower = text.lower().strip()

                    # 重置会话
                    if cmd_lower in ('重置', '重置会话', '新会话', 'reset', 'new'):
                        reset_session()
                        send_message(token, from_user, "✅ 会话已重置", context_token)
                        cooldown_until = time.time() + 3
                        recent_hashes[dedup_key] = time.time() + 60
                        continue

                    # 全部消息交给 Claude 处理
                    cmd = text

                    # 标记已处理
                    recent_hashes[dedup_key] = time.time() + 60
                    cooldown_until = time.time() + 3

                    log.info(f"处理: {cmd[:80]}...")

                    # 发送"正在输入"
                    if typing_ticket:
                        try:
                            send_typing(token, from_user, typing_ticket, 1)
                        except:
                            pass

                    def _send_response(text):
                        """发送回复，自动处理 __FILE__ 标记的文件上传（自动去重）"""
                        files = []
                        seen = set()
                        clean = []
                        for line in text.split('\n'):
                            if line.startswith('__FILE__:'):
                                fp = line[9:].strip()
                                if fp in seen:
                                    continue
                                seen.add(fp)
                                if os.path.isfile(fp):
                                    files.append(fp)
                                    log.info(f"发送文件: {fp}")
                                else:
                                    clean.append(line)
                            else:
                                clean.append(line)
                        clean_text = '\n'.join(clean)
                        if clean_text.strip():
                            send_message(token, from_user, clean_text.strip(), context_token)
                        for fp in files:
                            ext = os.path.splitext(fp)[1].lower()
                            if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico'):
                                send_image_message(token, from_user, fp, context_token)
                            else:
                                send_file_message(token, from_user, fp, context_token)

                    # 先尝试本地调度
                    handled, result = dispatch_local(cmd, work_dir)
                    if handled:
                        log.info(f"本地: {result[:80]}")
                        _send_response(result)
                    else:
                        # 快速模式跳过"处理中…"，直接调 Claude
                        quick_mode = config.get('CTI_QUICK_MODE', '0') == '1'
                        if not quick_mode:
                            send_message(token, from_user, "处理中…", context_token)
                        # 调用 Claude
                        log.info(f"调用Claude...")
                        full_prompt = SYSTEM_PROMPT + "\n\n---\n用户消息:\n" + cmd
                        response = call_claude(full_prompt, config)
                        log.info(f"Claude返回 {len(response)}字符")
                        _send_response(response)

                        # 后台预热 Claude，下次秒回
                        warmup_claude(config)

                    # 取消"正在输入"
                    if typing_ticket:
                        try:
                            send_typing(token, from_user, typing_ticket, 2)
                        except:
                            pass

                    write_status(last_message=text[:100])

                # 清理过期哈希
                now = time.time()
                for h in list(recent_hashes):
                    if recent_hashes[h] < now:
                        del recent_hashes[h]

            except requests.ConnectionError as e:
                consecutive_failures += 1
                log.error(f"连接失败 ({consecutive_failures}/3): {e}")
                if consecutive_failures >= 3:
                    time.sleep(30)
                    consecutive_failures = 0
                else:
                    time.sleep(2)

            except Exception as e:
                log.error(f"循环异常: {e}", exc_info=True)
                time.sleep(2)

    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        abort_flag.set()
        write_status(running=False)
        kill_my_claude()
        try:
            notify_stop(token)
        except:
            pass
        print("桥接已停止")


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(
        description="WeChat ClawBot Bridge — 基于腾讯 iLink Bot API 的微信 ↔ Claude 桥接"
    )
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--reset', action='store_true', help='重新扫码登录')
    args = parser.parse_args()

    config = load_config(args.config)

    # 单实例检查
    lock_file = os.path.join(config.get('CTI_RUNTIME_DIR',
        os.path.join(config['CTI_WORK_DIR'], 'runtime')), 'clawbot_bridge.pid')
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)

    if os.path.isfile(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x0400, False, old_pid)  # PROCESS_QUERY_INFORMATION
            if h:
                kernel32.CloseHandle(h)
                print(f"桥接已在运行 (PID: {old_pid})")
                print("如需重启，请先终止旧进程")
                sys.exit(1)
        except (ValueError, OSError):
            pass

    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))

    try:
        while True:
            try:
                run_bridge(config, reset_login=args.reset)
                # reset_login 只在第一次有效
                args.reset = False
            except Exception as e:
                log.error(f"桥接崩溃: {e}", exc_info=True)
                print(f"\n桥接异常退出: {e}")
            print("\n3 秒后自动重启...")
            time.sleep(3)
    finally:
        try:
            os.remove(lock_file)
        except:
            pass


if __name__ == '__main__':
    main()
