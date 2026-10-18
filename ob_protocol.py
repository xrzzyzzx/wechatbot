"""
OneBot v11 协议处理模块。

包括：
- make_message_event() — 构造 OneBot 消息事件 JSON
- push_event() — 通过 WebSocket 推送事件给 AstrBot
- _handle_ob_api() — 处理 AstrBot 发来的 API 请求（send_msg 等）
- _extract_text() — 从 OneBot message 段提取纯文本
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import logging

import requests

import state
import config

log = logging.getLogger("ob11-bridge")

# ---- @ 段跨调用暂存 + 真 @ 失败冷却 ----
# AstrBot 会把「@ 段」和「正文」拆成两次 API 调用发过来：
#   调用 1: [{type: "at", ...}]
#   调用 2: [{type: "text", text: "正文"}]
# 不暂存的话：调用 1 会单独吐出一条只有「@赵 」的废泡泡，
# 而且整套真 @ 流程（敲 @ → 找弹窗 → 失败 → 重试）要白跑两遍，日志跟着刷屏。
_pending_at = {"key": "", "prefix": "", "nick": "", "ts": 0.0}
_AT_STASH_TTL = 15.0       # 只含 @ 段的那次最多等 15 秒，等正文跟上来
_mention_cooldown_until = 0.0
_MENTION_COOLDOWN = 5.0    # 真 @ 失败后 5 秒内不再重试，直接走纯文字


def _reset_input_state():
    """把输入框恢复到干净状态：关掉可能残留的 @ 候选框，并清空草稿。

    为什么需要：连续两次 @ 挨得太近时，上一次的候选弹窗/半截草稿会留在输入框上，
    下一次再敲 @ 就弹不出成员列表——日志里表现为「第 N 次敲 @，输入框上方没变化」。
    所以每次动手 @ 之前，先把现场清干净，保证从零开始。
    """
    try:
        s = state.sender_instance
        s._activate()
        # 只在「确实有残留候选框」时才按 Esc —— 无脑按会把输入框的焦点也敲丢，
        # 焦点一丢，后面敲 @ 就是敲进空气，UIA 和像素会一起报「没弹窗」。
        try:
            rect = s._win_rect()
            if rect:
                l, t, r, b = rect
                if s._mention_popup_rects(l, t, r, b):
                    for _ in range(2):
                        s._press_escape()
                        time.sleep(0.25)
                    log.info("[OB11] 检测到残留候选框，已按 Esc 关掉")
        except Exception as _e:
            log.debug(f"[OB11] 残留候选框检查失败: {_e}")
        s._ctrl_letter('a')
        time.sleep(0.15)
        s._press(0x2E, 0.05)          # VK_DELETE
        time.sleep(0.35)
        log.info("[OB11] 已把输入框复位（清草稿）")
        return True
    except Exception as e:
        log.warning(f"[OB11] 复位输入框失败（继续尝试）: {e}")
        return False


def _click_input_box():
    """把焦点点回聊天输入框。

    真 @ 流程（点击聊天空白 → 点输入框 → 敲 @ → 找弹窗 → 撤退清空）跑完之后，
    输入框经常已经失焦；这时候再直接 Ctrl+V 粘正文，字会粘到聊天区、
    成员列表甚至空气里，群里就「什么都看不到」。
    所以退回普通发送之前，必须先把焦点点回输入框。
    """
    try:
        s = state.sender_instance
        rect = s._win_rect()
        if not rect:
            log.warning("[OB11] 点回输入框失败：拿不到窗口矩形")
            return False
        l, t, r, b = rect
        x = l + int((r - l) * 0.55)
        # 0.18 是量出来的：窗口底往上 18% 正对输入框中心；
        # 原来的 0.09 会落在输入框和表情工具栏之间的缝里，一点就把表情面板点开，
        # 表情面板一开，@ 成员列表就不会弹了。
        y = b - int((b - t) * 0.18)
        s._activate()
        s._click_at(x, y)
        time.sleep(0.30)
        log.info(f"[OB11] 已把焦点点回输入框 @({x},{y})")
        return True
    except Exception as e:
        log.warning(f"[OB11] 点回输入框异常: {e}")
        return False


async def _handle_ob_api(data: dict):
    """处理 AstrBot 发来的 API 请求。"""
    global _mention_cooldown_until
    action = data.get("action", "")
    params = data.get("params", {})
    echo = data.get("echo", "")
    log.info(f"[OB11] API: {action} echo={echo}")

    # 先回响应（必须在处理消息前回，否则 AstrBot 超时）
    resp_sent = False
    resp_data = {"status": "ok", "retcode": 0, "data": {}}
    if echo:
        resp_data["echo"] = echo
    if action == "get_login_info":
        # AstrBot 启动时会查询登录信息，返回机器人自身 ID 和昵称
        nickname = config.BOT_NICKNAMES[0] if config.BOT_NICKNAMES else "wechat_bot"
        resp_data["data"] = {"user_id": state._self_id_int, "nickname": nickname}
    # 如果 WS 暂时断连，等一会重试
    for retry in range(10):
        try:
            if state._ob_ws:
                await state._ob_ws.send(json.dumps(resp_data, ensure_ascii=False))
                resp_sent = True
                log.info(f"[OB11] 已回响应: {action}")
                break
            if retry < 9:
                await asyncio.sleep(0.5)
        except Exception as e:
            log.warning(f"[OB11] 回响应失败 (重试 {retry}/10): {e}")
            if retry < 9:
                await asyncio.sleep(0.5)
    if not resp_sent:
        log.warning(f"[OB11] 无法回响应（WS 未连接），消息仍尝试本地处理: {action}")

    if action in ("send_msg", "send_private_msg", "send_group_msg"):
        is_group = action == "send_group_msg"
        if action == "send_msg":
            # AstrBot 对群消息也常调用 send_msg，需按 params 里的 group_id 判断
            is_group = bool(params.get("group_id"))
        target_id = params.get("group_id" if is_group else "user_id", 0)
        message = params.get("message", [])
        contact = state._ob_id_to_contact.get(target_id, str(target_id))

        # 刚重启时「群号 → 群名」那张表是空的，会把数字群号当成微信联系人名去搜索，
        # 搜不到就切不过去，后面敲 @ 全部落空。这里用白名单里的群名兜底。
        if is_group and str(contact).isdigit():
            try:
                import nicks
                _fixed = nicks.group_name_of_id(int(contact))
                if _fixed:
                    log.info(f"[OB11] 群号 {contact} 反查为群名「{_fixed}」")
                    contact = _fixed
                    state._ob_id_to_contact[target_id] = _fixed
            except Exception as _e:
                log.warning(f"[OB11] 群号反查群名失败: {_e}")

        # ---- @ 段 → 可见文字「@群昵称」 ----
        # 微信的真 @ 得在输入框敲 @ 再从成员列表里挑人，纯键盘粘贴做不到，
        # 这里先退一步：把 at 段翻成文字，拼到第一条文字前面，不再单独发空消息。
        at_prefix = ""
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            qq = str(seg.get("data", {}).get("qq", "") or "")
            if not qq or qq == "all":
                continue
            nick = ""
            try:
                nick = state.id_to_group_nick.get(int(qq), "")
            except (TypeError, ValueError):
                nick = ""
            if not nick:
                try:
                    if state._self_id_int and int(qq) == state._self_id_int:
                        nick = config.BOT_NICKNAMES[0] if config.BOT_NICKNAMES else ""
                except (TypeError, ValueError):
                    pass
            if nick:
                at_prefix += f"@{nick}\u2005"
        if at_prefix:
            log.info(f"[OB11] @ 段已翻译为文字前缀: {at_prefix!r}")
        prefix_pending = bool(at_prefix)

        # 真 @ 的目标昵称（取第一个 @）
        mention_nick = ""
        if at_prefix:
            first = at_prefix.split("\u2005")[0].strip()
            mention_nick = first.lstrip("@").strip()

        # ---- 跨调用合并：只含 @ 段的那一次先攒着，等紧随其后的正文 ----
        has_body = any(
            isinstance(s, dict)
            and s.get("type") == "text"
            and (s.get("data", {}).get("text") or "").strip()
            for s in message
        )
        if at_prefix and not has_body:
            _pending_at.update(
                {"key": f"{action}:{target_id}", "prefix": at_prefix, "nick": mention_nick, "ts": time.time()}
            )
            log.info(f"[OB11] 只含 @ 段，先暂存等正文（不再单独发空泡泡）: {at_prefix!r}")
            return
        if (
            not at_prefix
            and _pending_at["key"] == f"{action}:{target_id}"
            and (time.time() - _pending_at["ts"]) < _AT_STASH_TTL
        ):
            at_prefix = _pending_at["prefix"]
            mention_nick = _pending_at["nick"]
            prefix_pending = True
            log.info(f"[OB11] 接上刚才暂存的 @ 前缀，与正文合并成一条: {at_prefix!r}")
        _pending_at.update({"key": "", "prefix": "", "nick": "", "ts": 0.0})

        # 逐段处理：文字和图片分别发送
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})

            if seg_type == "text":
                text = seg_data.get("text", "")
                # 纯空白段不算正文：AstrBot 常把「 ␣正文」开头那个空格拆成独立段，
                # 这个空段既不该单独发一条，也不该独吞掉 @ 前缀。
                if text.strip():
                    body = text
                    nick = mention_nick
                    use_prefix = prefix_pending
                    if prefix_pending:
                        prefix_pending = False

                    # 正文里直接写了「@某某」的（模型自己打的）优先：
                    # 那通常是主人点名要 @ 的人，比 AstrBot 的「自动 @ 发言人」更该兑现
                    if is_group and body:
                        n2, b2 = _split_plain_mention(body)
                        if n2:
                            nick, body, use_prefix = n2, b2, False

                    sent_mention = False
                    mention_attempted = bool(nick) and is_group
                    if nick and is_group and time.time() < _mention_cooldown_until:
                        log.info(
                            "[OB11] 真 @ 处于冷却期（还剩 %ds），这次直接走纯文字"
                            % int(_mention_cooldown_until - time.time())
                        )
                        nick = ""
                    if nick and is_group:
                        # 先把输入框复位，免得上一次 @ 的残留把这次的候选列表顶掉
                        await asyncio.to_thread(_reset_input_state)
                        try:
                            sent_mention = await asyncio.to_thread(
                                state.sender_instance.send_text_with_mention,
                                contact, nick, body,
                            )
                        except Exception as e:
                            log.warning(f"[OB11] @ 发送异常，退回普通文字: {e}")
                            sent_mention = False
                        if sent_mention:
                            log.info(f"[OB11] 真 @ 已发出 → {contact}: @{nick} {body[:40]}")
                        else:
                            _mention_cooldown_until = time.time() + _MENTION_COOLDOWN
                            log.warning(
                                f"[OB11] @{nick} 没成功，退回普通文字发送"
                                f"（{int(_MENTION_COOLDOWN)} 秒内不再重试真 @，省得刷屏）"
                            )
                        # 一次消息里 @ 只兑现一次：后面的文字段别再 @ 一遍，
                        # 否则一条消息会变成「@赵 」「@赵 正文」两个泡泡
                        mention_nick = ""

                    if sent_mention:
                        now = time.time()
                        state.sent_recently[body] = now
                        for k in [k for k, ts in state.sent_recently.items() if now - ts > 120]:
                            state.sent_recently.pop(k, None)
                        continue

                    # 真 @ 撤退之后输入框一般是失焦的：先点回输入框，
                    # 否则正文会粘不到输入框里，群里就「看不到这条消息」。
                    if mention_attempted:
                        await asyncio.to_thread(_click_input_box)

                    full = ((at_prefix if use_prefix else "") + body)
                    ok = await asyncio.to_thread(
                        state.sender_instance.send_text, contact, full
                    )
                    if not ok:
                        # 发送器明确返回失败（按键压根没送到微信）。
                        # 这里绝对不能记进 sent_recently：否则群里真有人发了
                        # 一模一样的文字，会被当成「自己刚发的」而被忽略。
                        log.error(f"[OB11✗] 文字发送失败（未送达）{contact}: {full[:50]}")
                        continue      # 原来的代码丢掉了返回值，会谎报成功
                    log.info(
                        f"[OB11] 文字按键已发出 → {contact}: {full[:50]}"
                        f"（键盘方案无法回读，不保证已送达）"
                    )
                    # 记录最近发出的文本，供 bridge_core 做自回复去重
                    now = time.time()
                    state.sent_recently[full] = now
                    # 顺带清理超过 120 秒的旧条目，防止字典膨胀
                    for k in [k for k, ts in state.sent_recently.items() if now - ts > 120]:
                        state.sent_recently.pop(k, None)

            elif seg_type == "image":
                file_val = seg_data.get("file", "")
                if not file_val:
                    continue

                img_path = None
                is_tmp_file = False  # 仅 base64 解码生成的临时文件才删

                # AstrBot 通过 aiocqhttp 发图片时用 base64:// 格式
                if file_val.startswith("base64://"):
                    try:
                        # 解码 + 写文件在线程池执行，避免大图卡死事件循环
                        b64_data = file_val[9:]
                        img_path = await asyncio.to_thread(_decode_base64_image, b64_data)
                        if img_path:
                            is_tmp_file = True
                            log.info(f"[OB11] 图片已解码: {os.path.basename(img_path)}")
                    except Exception as e:
                        log.warning(f"[OB11] base64 图片解码失败: {e}")
                else:
                    # 文件名模式：在附件目录找
                    if config.ASTRBOT_ATTACHMENTS:
                        candidates = [
                            os.path.join(config.ASTRBOT_ATTACHMENTS, file_val),
                            os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images", file_val),
                        ]
                        for p in candidates:
                            if os.path.exists(p):
                                img_path = p
                                break
                        if not img_path:
                            log.warning(f"[OB11] 图片文件未找到: {file_val}")

                if img_path:
                    try:
                        # 使用线程池执行同步的 UIA 发送，避免阻塞事件循环
                        ok = await asyncio.to_thread(
                            state.sender_instance.send_image, contact, img_path
                        )
                        if ok:
                            log.info(f"[OB11] 图片按键已发出 → {contact}（不保证已送达）")
                        else:
                            log.error(f"[OB11✗] 图片发送失败（未送达）{contact}")
                    finally:
                        # 仅删除 base64 解码生成的临时文件，避免误删附件目录中的用户文件
                        if is_tmp_file:
                            try:
                                os.unlink(img_path)
                            except Exception:
                                pass

            elif seg_type == "face":
                await asyncio.to_thread(state.sender_instance.send_text, contact, "[表情]")
                log.info(f"[OB11] 表情已发送至 {contact}")

            # 其他类型（record, video 等）忽略

    else:
        log.debug(f"[OB11] 未处理 API: {action}")

    # 注意：API 响应已在函数开头统一发送，此处不再重复


def _split_plain_mention(text: str):
    """正文里直接写「@某某」时，把昵称摘出来，返回 (昵称, 剩余正文)。

    只认「在群里发过言的成员昵称」，避免把普通的 @ 当成人名。
    """
    try:
        names = [n for n in set(state.id_to_group_nick.values()) if n]
    except Exception:
        return "", text
    names.sort(key=len, reverse=True)
    for n in names:
        token = f"@{n}"
        i = text.find(token)
        if i >= 0:
            rest = (text[:i] + text[i + len(token):]).strip()
            return n, rest
    return "", text


def _extract_text(message: list) -> str:
    """从 OneBot message 段中提取可发送的文本。"""
    text_parts = []
    for seg in message:
        if isinstance(seg, dict):
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "image":
                text_parts.append("[图片]")
            elif t == "face":
                text_parts.append("[表情]")
            elif t == "record":
                text_parts.append("[语音]")
            elif t == "video":
                text_parts.append("[视频]")
            elif t == "reply":
                if d.get("text"):
                    text_parts.append(f'"{d["text"]}"')
            elif t == "at":
                text_parts.append(f"@{d.get('qq', d.get('name', ''))}")
            else:
                # 其他未知类型也尝试提取文本
                text_parts.append(d.get("text", ""))
    return "".join(text_parts).strip()


# ============ OneBot 协议处理 ============


def make_message_event(message_type: str, user_id: int, message: list,
                       group_id: int = 0, group_name: str = "",
                       nickname: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": state._self_id_int,
        "post_type": "message",
    }
    if message_type == "group":
        event["message_type"] = "group"
        event["group_id"] = group_id
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
        event["group_name"] = group_name or str(group_id)
    else:
        event["message_type"] = "private"
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
    return event


def push_event(event: dict) -> bool:
    """通过 WebSocket 客户端连接向 AstrBot 推送事件。"""
    if not state._ob_ws or not state._ob_ws_loop:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            state._ob_ws.send(json.dumps(event, ensure_ascii=False)),
            state._ob_ws_loop,
        )
        future.result(timeout=5)
        return True
    except Exception as e:
        log.warning(f"[OB11] 推送事件失败: {e}")
        return False


def _decode_base64_image(b64_data: str) -> str | None:
    """在线程池中执行：解码 base64 图片并保存为临时文件。"""
    import tempfile
    img_data = base64.b64decode(b64_data)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(img_data)
    tmp.close()
    return tmp.name
