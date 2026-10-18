"""
配置模块：加载 config.json，提供全局配置常量。
"""

import json
import os
import logging
import threading

# ============ 本机流量绕行代理 ============
# 桥接只跟本机通信（WeFlow: 127.0.0.1:5031 / AstrBot: 127.0.0.1:11229）。
# 若系统设置了 HTTP_PROXY / ALL_PROXY（如 Clash、V2Ray、公司代理），
# requests 与 websockets 会把 127.0.0.1 的回环流量也发给代理，
# 典型症状是 "proxy rejected connection: HTTP 502" 或 "无法连接 WeFlow"。
# 这里在最早加载的模块里强制把本机地址加入 NO_PROXY，保证本机直连。
_LOCAL_BYPASS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _ensure_local_no_proxy() -> None:
    for key in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(key, "")
        items = [x.strip() for x in cur.split(",") if x.strip()]
        for host in _LOCAL_BYPASS:
            if host not in items:
                items.append(host)
        os.environ[key] = ",".join(items)


_ensure_local_no_proxy()

# ============ 配置 ============

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()

WE_FLOW_BASE_URL = config["weflow_base_url"]
ACCESS_TOKEN = config["access_token"]
ASTRBOT_ATTACHMENTS = config.get("astrbot_attachments", "")
BOT_NICKNAMES = config["bot_nicknames"]
BOT_WXID = config.get("bot_wxid", "")
# 发送方式已固定为 UIA 纯键盘模拟
BUFFER_SECONDS = config.get("buffer_seconds", 5)
WEB_PORT = config.get("web_port", 8766)
GROUP_REPLY_MODE = config.get("group_reply_mode", "mention")  # "mention" / "all"
# 仅这些群在 all 模式下全量推送（留空 = 所有群全量）；其他群退回"只理 @ 和句首斜杠"
ALL_MODE_GROUPS = config.get("all_mode_groups", [])

# AstrBot OneBot 连接配置（bridge 作为 WebSocket 客户端连 AstrBot 的 aiocqhttp 服务端）
ASTRBOT_OB_URL = config.get("astrbot_ob_url", "ws://127.0.0.1:19777")

# 图片描述配置（支持 ollama 或 openai 兼容 API）
IMAGE_CAPTION_PROVIDER = config.get("image_caption_provider", "ollama")  # "ollama" / "openai"
IMAGE_CAPTION_MODEL = config.get("image_caption_model", "llava:7b")
IMAGE_CAPTION_API_KEY = config.get("image_caption_api_key", "")
IMAGE_CAPTION_API_BASE = config.get("image_caption_api_base", "")
IMAGE_CAPTION_PROMPT = config.get("image_caption_prompt", "请用中文简短描述这张图片的内容")

# Ollama 图片描述配置（provider=ollama 时使用）
OLLAMA_BASE_URL = config.get("ollama_base_url", "http://127.0.0.1:61000")
OLLAMA_TIMEOUT = config.get("ollama_timeout", 60)

# ============ 日志 ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("bridge.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ob11-bridge")
