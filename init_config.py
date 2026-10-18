"""
首次配置向导
============

回答几个问题，自动生成 config.json。
不依赖任何第三方库，也不 import config（那会要求 config.json 已存在）。

用法：
    python init_config.py
或双击「首次配置.bat」。
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")

# Windows 控制台默认可能不是 UTF-8，中文字符会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def ask(prompt, default="", required=False):
    """问一个问题。required=True 时不允许空答案。"""
    warned = False
    while True:
        hint = f"（默认 {default}）" if default else ""
        raw = input(f"{prompt}{hint}: ").strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            print("  这一项不能留空，请重新输入。")
            continue
        if "\ufffd" in raw and not warned:
            warned = True
            print(
                "  ⚠ 读到的内容里有乱码字符。建议改用「start.bat」启动向导"
                "（它会先把控制台切成 UTF-8），或者直接手改 config.json。"
            )
            print("  再输入一次（仍乱码就照原样存下来，之后手改 config.json 即可）：")
            continue
        return raw


def ask_yes(prompt, default=False):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "是")


def main():
    print("=" * 56)
    print("  WeChat ↔ AstrBot 桥接 · 首次配置")
    print("=" * 56)
    print("说明：带 * 的是必填项。填错可以随时重跑本向导，或直接改 config.json。\n")

    if os.path.exists(CONFIG_PATH) and not ask_yes(
        "config.json 已存在，要覆盖吗？", default=False
    ):
        print("已取消，原有 config.json 未改动。")
        return

    cfg = {}

    print("── WeFlow ─────────────────────────────────")
    cfg["weflow_base_url"] = ask(
        "* WeFlow API 地址", default="http://127.0.0.1:5031"
    )
    cfg["access_token"] = ask(
        "* WeFlow Access Token（在 WeFlow 设置里生成/查看）", required=True
    )

    print("\n── 机器人自己的微信 ───────────────────────")
    cfg["bot_wxid"] = ask(
        "* 机器人微信的 wxid", required=True
    )
    nicks_raw = ask(
        "* 机器人微信的昵称（多个用英文逗号隔开）", required=True
    )
    cfg["bot_nicknames"] = [x.strip() for x in nicks_raw.replace("，", ",").split(",") if x.strip()]

    print("\n── AstrBot 连接 ───────────────────────────")
    cfg["astrbot_ob_url"] = ask(
        "* AstrBot aiocqhttp 地址", default="ws://127.0.0.1:11229/ws"
    )
    cfg["astrbot_attachments"] = ask(
        "  AstrBot attachments 目录（可留空）", default=""
    )

    print("\n── 网页控制面板 ───────────────────────────")
    cfg["web_port"] = int(ask("  面板端口", default="8766") or 8766)
    cfg["buffer_seconds"] = int(ask("  消息合并秒数", default="5") or 5)

    print("\n── 群里怎么触发回复 ───────────────────────")
    print("  mention = 只有 @机器人、句首斜杠、或命中唤醒词才回（推荐）")
    print("  all     = 群里所有消息都送 AI（机器人会主动插话）")
    mode = ask("  回复模式 mention/all", default="mention")
    cfg["group_reply_mode"] = mode if mode in ("mention", "all") else "mention"

    if cfg["group_reply_mode"] == "all":
        groups_raw = ask("  只让哪些群全量回复（多个用英文逗号隔开，留空=所有群）")
        cfg["all_mode_groups"] = [
            x.strip() for x in groups_raw.replace("，", ",").split(",") if x.strip()
        ]
    else:
        cfg["all_mode_groups"] = []

    wake_raw = ask("  默认唤醒词（多个用英文逗号隔开，可留空）")
    cfg["wake_words"] = [
        x.strip() for x in wake_raw.replace("，", ",").split(",") if x.strip()
    ]

    print("\n── 图片描述（把群里的图片转成文字给 AI 看）──")
    if ask_yes("  要启用图片描述吗？", default=False):
        provider = ask("  后端 ollama 还是 openai 兼容接口？", default="ollama")
        cfg["image_caption_provider"] = "openai" if provider.startswith("open") else "ollama"
        if cfg["image_caption_provider"] == "ollama":
            cfg["image_caption_model"] = ask("  模型名", default="llava:7b")
            cfg["ollama_base_url"] = ask("  Ollama 地址", default="http://127.0.0.1:11434")
            cfg["ollama_timeout"] = 60
            cfg["image_caption_api_key"] = ""
            cfg["image_caption_api_base"] = ""
        else:
            cfg["image_caption_model"] = ask("  模型名", required=True)
            cfg["image_caption_api_base"] = ask(
                "  API base_url", default="https://api.openai.com/v1"
            )
            cfg["image_caption_api_key"] = ask("  API Key", required=True)
            cfg["ollama_base_url"] = "http://127.0.0.1:11434"
            cfg["ollama_timeout"] = 60
        cfg["image_caption_prompt"] = ask(
            "  图片描述提示词", default="请用中文简短描述这张图片的内容"
        )
    else:
        cfg.update(
            {
                "image_caption_provider": "ollama",
                "image_caption_model": "llava:7b",
                "image_caption_api_key": "",
                "image_caption_api_base": "",
                "image_caption_prompt": "请用中文简短描述这张图片的内容",
                "ollama_base_url": "http://127.0.0.1:11434",
                "ollama_timeout": 60,
            }
        )

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)

    print("\n" + "=" * 56)
    print(f"✅ 已写入 {CONFIG_PATH}")
    print("=" * 56)
    print("下一步：")
    print("  1. 确认 WeFlow 已登录微信并开启 API 服务（端口应与上面一致）")
    print("  2. 确认 AstrBot 已加好 aiocqhttp 平台，端口应与上面一致")
    print("  3. 双击「启动.bat」")
    print("  4. 浏览器打开 http://127.0.0.1:%d 查看面板" % cfg["web_port"])
    print()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        sys.exit(1)
