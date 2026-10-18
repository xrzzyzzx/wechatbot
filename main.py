"""
入口与生命周期管理模块。

负责桥接的启动、停止、主循环重连逻辑，以及命令行入口。
"""

import json
import logging
import os
import sys
import threading
import time

import requests

import state
import config
from senders import create_sender
from ob_client import _run_ob_client
from bridge_core import WeFlowBridge
from web_panel import WebHandler, PAGE
from http.server import HTTPServer

log = logging.getLogger("ob11-bridge")


# ============ 启动 / 停止 ============


def _start_bridge():
    with state.run_lock:
        if state.running:
            return
        state.running = True
    state.paused.clear()
    state.sender_instance = create_sender()

    if not state.ob_client_started:
        t = threading.Thread(target=_run_ob_client, daemon=True, name="ob11-client")
        t.start()
        state.ob_client_started = True

    state.bridge_thread = threading.Thread(target=_bridge_loop, daemon=True, name="bridge")
    state.bridge_thread.start()
    log.info("[Web] 已启动")


def _stop_bridge():
    with state.run_lock:
        state.running = False

    # 切断 SSE 长连接，让 _bridge_loop 的 listen_sse() 从阻塞中退出
    with state.bridge_lock:
        if state.bridge_instance and state.bridge_instance._sse_session:
            try:
                state.bridge_instance._sse_session.close()
                log.info("[Web] SSE 连接已断开")
            except Exception as e:
                log.warning(f"[Web] 断开 SSE 异常: {e}")

    # 关闭 WebSocket 连接，让 _ob_client_main 从 async for 中退出
    _ws = state._ob_ws
    _loop = state._ob_ws_loop
    if _ws:
        try:
            if _loop and _loop.is_running():
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _ws.close(), _loop
                )
                log.info("[Web] WebSocket 连接已关闭")
        except Exception as e:
            log.warning(f"[Web] 关闭 WebSocket 异常: {e}")

    state._ob_ws_ready.clear()

    # 重置启动标记，让下次 start 能重新拉起 WebSocket 客户端线程
    state.ob_client_started = False
    state._ob_ws_loop = None

    log.info("[Web] 已停止")


def _bridge_loop():
    import ctypes
    ctypes.windll.ole32.CoInitialize(None)

    if not config.ACCESS_TOKEN:
        log.error("❌ 未配置 access_token")
        state.running = False
        return

    log.info(f"Bridge | WeFlow: {config.WE_FLOW_BASE_URL} | OB11: {config.ASTRBOT_OB_URL} | 发送: uia")

    bridge = WeFlowBridge(state.sender_instance)
    with state.bridge_lock:
        state.bridge_instance = bridge

    try:
        r = requests.get(f"{config.WE_FLOW_BASE_URL}/api/v1/messages?limit=1&access_token={config.ACCESS_TOKEN}", timeout=5)
        if r.status_code == 200:
            log.info("✅ WeFlow API 正常")
        elif r.status_code == 401:
            log.error("❌ Access Token 无效")
            state.running = False
            return
    except requests.exceptions.ConnectionError:
        log.error("❌ 无法连接 WeFlow")
        state.running = False
        return

    while state.running:
        try:
            bridge.listen_sse()
        except Exception as e:
            log.error(f"SSE: {e}")
        if not state.running:
            break
        log.warning("⚠️ SSE 断开，10s 后重连")
        for _ in range(10):
            if not state.running:
                break
            time.sleep(1)

    with state.bridge_lock:
        state.bridge_instance = None


def start_web():
    server = HTTPServer(("127.0.0.1", config.WEB_PORT), WebHandler)
    log.info(f"Web: http://127.0.0.1:{config.WEB_PORT}")
    server.serve_forever()


# ============ 入口 ============

if __name__ == "__main__":
    # 从 config 初始化 state 中需要计算的值
    state._self_id_int = state._wxid_to_int(config.BOT_WXID or "wechat_bot")
    state.group_reply_mode = config.GROUP_REPLY_MODE
    state.all_mode_groups = config.ALL_MODE_GROUPS

    # 初始化运行时可变的图片描述配置
    state.image_caption_provider = config.IMAGE_CAPTION_PROVIDER
    state.image_caption_model = config.IMAGE_CAPTION_MODEL
    state.image_caption_api_key = config.IMAGE_CAPTION_API_KEY
    state.image_caption_api_base = config.IMAGE_CAPTION_API_BASE
    state.image_caption_prompt = config.IMAGE_CAPTION_PROMPT
    state.ollama_base_url = config.OLLAMA_BASE_URL
    state.ollama_timeout = config.OLLAMA_TIMEOUT

    # ---- 群成员昵称白名单：一个群一个文件，缺谁补谁 ----
    # 这张表决定「@ 某人时能不能翻出他的群昵称」。不预加载的话，
    # 桥接一重启就失忆，@ 谁都得等对方先开口。
    try:
        import nicks
        state.id_to_group_nick.update(nicks.ensure_all())
        state.group_policies = nicks.load_policies()
        state.nicks_module = nicks
        log.info(
            "[白名单] 各群回复策略: "
            + " | ".join(
                f"{g}: 全量={p.get('全量回复')} 唤醒={p.get('唤醒词')}"
                for g, p in list(state.group_policies.items())[:6]
            )
        )
    except Exception as _e:
        log.warning(f"[白名单] 初始化失败: {_e}")

    PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.pid")

    def pid_exists(pid):
        """判断 PID 是否仍在运行。

        原实现用 OpenProcess(0x0400 = PROCESS_QUERY_INFORMATION)：若目标进程
        以更高权限运行（管理员启动的桥接），普通权限下这个调用会 Access Denied，
        函数便误判为「进程已死」→ 删掉 pid 文件 → 允许双开。
        改为 PROCESS_QUERY_LIMITED_INFORMATION(0x1000)，该权限对提权进程也可用；
        仍失败时用 ERROR_ACCESS_DENIED 判定「进程确实存在」。
        """
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            # 打不开句柄：权限不足说明进程存在；进程不存在才是真的没了
            return ctypes.windll.kernel32.GetLastError() == ERROR_ACCESS_DENIED
        except Exception:
            # 无法判断时保守认为存在，宁可拒绝启动也不要双开
            return True

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid() and pid_exists(old_pid):
                log.error(
                    f"⚠️ bridge.pid 已存在，桥接似乎已在运行 (PID={old_pid})。\n"
                    f"    如确认该进程已退出，请删除: {PID_FILE}\n"
                    f"    如需强制启动，可先执行: taskkill /F /PID {old_pid}"
                )
                sys.exit(1)
            else:
                os.remove(PID_FILE)
        except (ValueError, OSError):
            os.remove(PID_FILE)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        log.info("=" * 50)
        log.info(" WeFlow 微信桥接 (OneBot v11)")
        log.info("=" * 50)
        log.info("Bridge 版本: 2026-06-03 OB11")
        _start_bridge()
        start_web()
    finally:
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
