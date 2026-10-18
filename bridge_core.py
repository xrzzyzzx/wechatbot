"""
桥接核心模块：WeFlowBridge 类。

职责：
1. 连接 WeFlow SSE 推送，接收微信消息
2. 消息缓冲合并（BUFFER_SECONDS）
3. 构造 OneBot 事件，推送给 AstrBot
4. 多层消息去重（rawid、内容、自回复）
"""

import json
import html
import logging
import os
import tempfile
import queue
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

import requests

import state
import config
from ob_protocol import push_event, make_message_event

log = logging.getLogger("ob11-bridge")


# ============ 桥接核心 ============


class WeFlowBridge:
    """WeFlow ↔ AstrBot 桥接器（OneBot v11 版）。"""

    def __init__(self, sender):
        self.sender = sender
        self.processed_ids = set()
        self.start_timestamp = int(time.time())
        self.pending_buffers = {}
        self.buffer_lock = threading.Lock()
        self.chat_histories = defaultdict(list)
        self.contact_map = {}
        self._sse_session = None
        self._pending_image = {}  # talkerId → {"caption": None|str, "event": threading.Event()}
        self._pending_mention_images = {}  # session_id → {"data": data, "time": timestamp} 先图后文暂存

    def should_ignore(self, data):
        content = data.get("content", "")
        msg_type = data.get("type", 0) or data.get("msgType", 0)
        if data.get("sourceName", "") in config.BOT_NICKNAMES:
            return True
        if config.BOT_WXID and data.get("talkerId", "") == config.BOT_WXID:
            return True
        if msg_type in (34,):  # 34=语音
            return True
        if content and "[语音]" in content:
            return True
        if not content or content.strip() == "":
            return True
        return False

    def _is_mentioned(self, data, group_name=""):
        """检测群消息是否 @ 了机器人、句首用了「/」，或者提到了本群的唤醒词。

        WeFlow SSE 推送不含 @ 结构字段，只能从 content 文本检测。
        """
        content = data.get("content", "")
        if not content:
            return False

        # 句首的「/」或「／」等同于 @：少打一个 @，也能叫我
        if re.match(r"^\s*[/／]", content):
            return True

        for nick in config.BOT_NICKNAMES:
            # 标准 @昵称（ASCII @）
            if f"@{nick}" in content:
                return True
            # 全角 @昵称（部分微信版本可能用全角符号）
            if f"＠{nick}" in content:
                return True

        # 日志：content 以 @ 开头但未匹配到任何昵称（便于排查）
        if (content.startswith("@") or content.startswith("＠")) and len(content) > 1:
            log.debug(f"⚠️ content 以@开头但未匹配昵称: content={content[:40]!r} nicknames={config.BOT_NICKNAMES}")

        # 本群自己的「唤醒词」：不 @ 也算被点名
        # （写在 nicks/<群名>.json 的「回复策略.唤醒词」里）
        policy = (state.group_policies or {}).get(group_name or "", {}) or {}
        if policy.get("提到就回", True):
            for w in (policy.get("唤醒词") or []):
                if w and w in content:
                    log.info(f"👂 命中本群唤醒词「{w}」")
                    return True

        return False

    def _in_all_mode_groups(self, group_name, session_id):
        """该群是否在"全回复限定群"名单里（写群名或 chatroom 会话号都认）。"""
        for item in state.all_mode_groups or []:
            s = str(item).strip()
            if s and (s == group_name or s == session_id):
                return True
        return False

    def _cleanup_pending_mention_images(self):

        """清理超过 15 秒的过期暂存图片/表情，避免一直无后续 @ 文字时内存只增不减。"""
        now = time.time()
        expired = [k for k, v in self._pending_mention_images.items() if now - v["time"] > 15]
        for k in expired:
            self._pending_mention_images.pop(k, None)

    # ---- 引用（reply/refermsg）解析 ----

    _REFER_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)

    @staticmethod
    def _xml_tag(block, tag):
        """从一段 XML 里取出某个标签的文本（兼容 CDATA），并做反转义。"""
        m = re.search(
            rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block or "", re.S
        )
        if not m:
            return ""
        return html.unescape(m.group(1)).strip()

    def _extract_refer(self, raw):
        """从一段原始内容（rawContent）里解析被引用的那条消息。

        返回 (prefix, info)：prefix 是拼在正文前的描述，info 是给日志用的字典。
        微信的 rawContent 里带 <refermsg> 块，含被引原文(content)、
        发言人(displayname)、时间(createtime) 和该消息的服务器 ID(svrid)。
        """
        text = str(raw or "")
        m = self._REFER_RE.search(text)
        if not m:
            return "", {}

        block = m.group(1)
        name = self._xml_tag(block, "displayname") or "某人"
        quoted = self._xml_tag(block, "content")
        svrid = self._xml_tag(block, "svrid")
        createtime = self._xml_tag(block, "createtime")

        # 被引用的可能是图/文件/链接等富文本，取出标题替代
        is_media = False
        media_marker = ""
        if quoted.startswith("<msg") or "<appmsg" in quoted or "<img" in quoted:
            is_media = True
            title = self._xml_tag(quoted, "title")
            label = f"[图片或附件]{title}" if title else "[图片或附件]"
            # 留个记号，稍后由 _inject_refer_image 换成图片描述
            if svrid:
                media_marker = f"{label}#{svrid}"
                quoted = media_marker
            else:
                quoted = label

        quoted = re.sub(r"\s+", " ", quoted).strip()
        if len(quoted) > 200:
            quoted = quoted[:200] + "…"

        when = ""
        if createtime.isdigit():
            try:
                when = datetime.fromtimestamp(int(createtime)).strftime("%m-%d %H:%M")
            except (ValueError, OSError, OverflowError):
                when = ""

        prefix = f"[引用 @{name}"
        if when:
            prefix += f" {when}"
        prefix += f"：{quoted or '（内容为空）'}] "
        if svrid:
            prefix += f"(被引消息ID {svrid}) "

        return prefix, {"name": name, "quoted": quoted, "svrid": svrid, "when": when,
                        "is_media": is_media, "media_marker": media_marker}

    def _caption_refer_image(self, talker, svrid):
        """按被引图片的编号取出图片并描述，供引用图片时使用。

        全程 try/except 兜底，失败返回 None，不影响消息正常流转。
        """
        try:
            r = requests.get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                params={"talker": talker, "media": "true", "limit": 400,
                        "access_token": config.ACCESS_TOKEN},
                timeout=10,
            )
            if r.status_code != 200:
                return None
            for m in r.json().get("messages") or []:
                if str(m.get("serverId")) != str(svrid):
                    continue
                if m.get("mediaType") not in ("image", "sticker", "emoji"):
                    log.info(f"🖼️ 被引用的是 {m.get('mediaType')}，不做图片描述")
                    return None

                # 优先用本地缓存，省一次下载
                path = str(m.get("mediaLocalPath") or "")
                if path and os.path.exists(path):
                    return caption_image_via_ollama(path)

                media_url = str(m.get("mediaUrl") or "")
                if not media_url:
                    return None
                sep = "&" if "?" in media_url else "?"
                resp = requests.get(
                    f"{media_url}{sep}access_token={config.ACCESS_TOKEN}", timeout=30
                )
                if resp.status_code != 200 or not resp.content:
                    return None
                tmp = os.path.join(tempfile.gettempdir(), f"refer_{svrid}.jpg")
                with open(tmp, "wb") as f:
                    f.write(resp.content)
                return caption_image_via_ollama(tmp)
        except Exception as e:
            log.warning(f"取被引图片失败（忽略）: {e}")
        return None

    def _inject_refer_image(self, buffer_key, version, talker, svrid, marker):
        """描述被引用的图片，并把它回填到缓冲里（替换掉占位记号）。"""
        try:
            caption = self._caption_refer_image(talker, svrid)
            text = f"[引用图: {caption}]" if caption else "[引用图: 未取到]"
            with self.buffer_lock:
                entry = self.pending_buffers.get(buffer_key)
                if entry and entry.get("timer_version") == version:
                    replaced = 0
                    new_msgs = []
                    for m in entry["messages"]:
                        if isinstance(m, str) and marker and marker in m:
                            new_msgs.append(m.replace(marker, text))
                            replaced += 1
                        else:
                            new_msgs.append(m)
                    entry["messages"] = new_msgs
                    if replaced:
                        log.info(f"🖼️ 引用图已回填: {text[:60]}")
                    else:
                        log.warning(f"🖼️ 引用图没找到记号，未回填 (marker={marker[:40]})")
        except Exception as e:
            log.warning(f"引用图处理异常（忽略）: {e}")

    def _fetch_raw_content(self, data):
        """推送里不带引用信息，回本地库按 rawid(serverId) 取回消息原文。

        全程 try/except 兜底：取不到就返回空，不影响消息正常流转。
        """
        rawid = str(data.get("rawid") or "")
        talker = data.get("sessionId") or data.get("talkerId") or ""
        if not rawid or not talker or not config.ACCESS_TOKEN:
            return ""
        try:
            r = requests.get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                params={"talker": talker, "limit": 8, "access_token": config.ACCESS_TOKEN},
                timeout=3,
            )
            if r.status_code != 200:
                return ""
            for m in r.json().get("messages") or []:
                if str(m.get("serverId")) == rawid:
                    return str(m.get("rawContent") or "")
        except Exception as e:  # 网络/解析任何异常都不该影响主流程
            log.debug(f"取消息原文失败（忽略）: {e}")
        return ""

    def add_to_buffer(self, data):
        """将消息加入缓冲区，等待合并后统一推送给 AstrBot。"""
        # Web 面板暂停时丢弃新消息
        if state.paused.is_set():
            log.info("⏸️ 已暂停处理，丢弃新消息")
            return

        content = data.get("content", "")
        source_name = data.get("sourceName", "") or data.get("talkerName", "") or "未知"

        # 先判断群聊/私聊（图片/表情分支也需要用到）
        session_id_data = data.get("sessionId", "") or source_name
        group_name_raw = data.get("groupName", "")
        is_group = (data.get("sessionType", "") == "group") or bool(group_name_raw) or "@chatroom" in session_id_data

        group_raw = group_name_raw or source_name
        base_name = re.sub(r'\s*\(\d+\)\s*$', '', group_raw).strip()

        # 这个群是不是"只理 @ / 句首斜杠 / 本群唤醒词"：
        #   ① 优先看这个群自己的「回复策略」（写在 nicks/<群名>.json 里）——
        #      全量回复=False → 只理点名；True → 闲聊也推给 AI
        #   ② 没有策略的老群，退回原来的全局规则
        group_policy = (state.group_policies or {}).get(base_name) or {}
        mention_only = False
        if is_group:
            if group_policy:
                mention_only = not bool(group_policy.get("全量回复", False))
            elif state.group_reply_mode == "mention":
                mention_only = True
            elif state.group_reply_mode == "all" and state.all_mode_groups:
                mention_only = not self._in_all_mode_groups(base_name, session_id_data)

        if content == "[图片]":
            # 图片消息（只理 @ 的群里需 @ 才处理）
            if is_group and mention_only and not self._is_mentioned(data, base_name):
                # 先图后文：暂存图片，等后续同人发 @ 文字时合并
                self._cleanup_pending_mention_images()
                self._pending_mention_images[session_id_data] = {"data": data, "time": time.time()}
                log.info(f"📸 暂存图片，等待关联 @ 文字 (session={session_id_data})")
                return
            threading.Thread(target=self.process_image_message,
                           args=(data,), daemon=True).start()
            return

        if content in ("[动画表情]", "[表情]"):
            # 表情包消息（只理 @ 的群里需 @ 才处理）
            if is_group and mention_only and not self._is_mentioned(data, base_name):
                # 先图后文：暂存表情，等后续同人发 @ 文字时合并
                self._cleanup_pending_mention_images()
                self._pending_mention_images[session_id_data] = {"data": data, "time": time.time()}
                log.info(f"😀 暂存表情，等待关联 @ 文字 (session={session_id_data})")
                return
            threading.Thread(target=self.process_emoji_message,
                           args=(data,), daemon=True).start()
            return

        now = time.time()
        if content and content in state.sent_recently and now - state.sent_recently[content] < 120:
            log.info(f"⏭️ 自回复去重跳过: {content[:30]}")
            return

        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")

        if is_group:
            mentioned = self._is_mentioned(data, base_name)
            if mention_only and not mentioned:
                log.debug(f"⏭️ 该群只理 @ / 句首斜杠，跳过: content={content[:40]}")
                return
            contact = base_name
            # 句首斜杠只是"叫我"的暗号，剥掉再交给我，免得当成 AstrBot 指令
            if mentioned:
                stripped = re.sub(r'^\s*[/／]\s*', '', content)
                if stripped != content:
                    if not stripped:
                        log.info("✂️ 只发了一个斜杠，没有正文，忽略")
                        return
                    log.info("✂️ 群聊用「/」唤醒，已剥掉句首斜杠")
                    content = stripped
        else:
            mentioned = False
            contact = source_name

        # 引用消息：推送里不带引用内容，回本地库按 rawid 取原文再解析。
        # 放在 @ 判断之后，群里没被 @ 的消息就不用白查一次库。
        raw_src = str(data.get("rawContent") or "")
        if "<refermsg" not in raw_src:
            raw_src = self._fetch_raw_content(data)
        refer_prefix, refer_info = self._extract_refer(raw_src)
        refer_media_svrid = ""
        refer_media_marker = ""
        if refer_prefix:
            content = refer_prefix + content
            log.info(
                f"🔗 引用已解析 ← @{refer_info.get('name')}:"
                f"{refer_info.get('quoted', '')[:40]}"
            )
            if refer_info.get("is_media") and refer_info.get("svrid"):
                refer_media_svrid = str(refer_info["svrid"])
                refer_media_marker = str(refer_info.get("media_marker") or "")

        if is_group and state.group_reply_mode == "batch":
            buffer_key = f"__batch__{base_name}"
        elif is_group and sender_in_group:
            buffer_key = f"{session_id_data}_{sender_in_group}"
        else:
            buffer_key = session_id_data

        with self.buffer_lock:
            if buffer_key not in self.pending_buffers:
                self.pending_buffers[buffer_key] = {
                    "messages": [],
                    "timer": None,
                    "timer_version": 0,
                    "processing": False,
                    "contact": contact,
                    "is_group": is_group,
                    "source_name": source_name,
                    "group_name": base_name if is_group else "",
                    "sender_in_group": sender_in_group if is_group else "",
                    "session_id_data": session_id_data,
                    "mentioned": mentioned if is_group else False,
                    "mention_only": mention_only,
                }
            entry = self.pending_buffers[buffer_key]
            if is_group and mentioned:
                # 这一批里只要有一条是在叫我，就按"被点名"处理
                entry["mentioned"] = True
            if is_group and state.group_reply_mode == "batch" and sender_in_group:
                entry["messages"].append(f'成员"{sender_in_group}"在群"{base_name}"中对你说：{content}')
            else:
                entry["messages"].append(content)

            if not entry["processing"]:
                if entry["timer"]:
                    entry["timer"].cancel()
                entry["timer_version"] += 1
                version = entry["timer_version"]

                # 检查是否有暂存的图片（先图后文场景）
                has_pending_image = False
                if is_group and mention_only:
                    cached = self._pending_mention_images.pop(session_id_data, None)
                    if cached and time.time() - cached["time"] < 15:
                        has_pending_image = True
                        log.info(f"📸 检测到关联图片，延长缓冲等待描述")
                        # 异步下载并描述图片，完成后注入 buffer
                        threading.Thread(
                            target=self._inject_cached_image,
                            args=(cached["data"].get("sessionId", session_id_data),
                                  buffer_key, version),
                            daemon=True,
                        ).start()

                # 引用里夹着图片：同样延长缓冲，等描述回来再回填
                if refer_media_svrid:
                    has_pending_image = True
                    log.info(f"🖼️ 引用里夹着图，延长缓冲等描述 (svrid={refer_media_svrid})")
                    threading.Thread(
                        target=self._inject_refer_image,
                        args=(buffer_key, version, session_id_data,
                              refer_media_svrid, refer_media_marker),
                        daemon=True,
                    ).start()

                buffer_delay = 15 if has_pending_image else config.BUFFER_SECONDS
                log.info(f"📩 收到来自 {contact} 的消息，等待 {buffer_delay}s 后统一推送")
                timer = threading.Timer(buffer_delay, lambda v=version, sid=buffer_key: self.process_sender(sid, v))
                timer.daemon = True
                timer.start()
                entry["timer"] = timer

    def process_sender(self, sender_id, version=None):
        """缓冲到期：通过 OneBot 事件推送给 AstrBot。"""
        with self.buffer_lock:
            if sender_id not in self.pending_buffers:
                return
            entry = self.pending_buffers[sender_id]
            if version is not None and entry.get("timer_version", 0) != version:
                return
            if not entry["messages"]:
                return
            msgs = entry["messages"].copy()
            entry["messages"] = []
            entry["processing"] = True
            if entry["timer"]:
                entry["timer"].cancel()
                entry["timer"] = None

        contact = entry.get("contact", sender_id)
        is_group = entry.get("is_group", False)
        combined = "\n".join(msgs)
        log.info(f"推送 {len(msgs)} 条消息 [{'群' if is_group else '私'}|{contact}]")

        # 构建 OneBot 事件（user_id 要用发言人身份，不能用群 sessionId）
        if is_group:
            sender_wxid = entry.get("session_id_data", "") + "_" + (entry.get("sender_in_group", "") or entry.get("source_name", ""))
        else:
            sender_wxid = entry.get("session_id_data", sender_id)
        user_id = state._wxid_to_int(sender_wxid)

        if is_group:
            group_id = state._wxid_to_int(entry.get("group_name", contact))
            sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "未知")

            # 记下「成员 user_id → 群昵称」，供 ob_protocol 把 at 段翻译成可见文字
            if sender_name and sender_name != "未知":
                state.id_to_group_nick[user_id] = sender_name
                # 顺手补进这个群自己的白名单文件（一个群一份，互不相通）
                try:
                    import nicks
                    nicks.remember(
                        entry.get("group_name", contact),
                        entry.get("session_id_data", ""),
                        user_id,
                        sender_name,
                    )
                except Exception:
                    pass

            if state.group_reply_mode == "batch":
                # 批处理模式：消息已预格式化好，直接使用
                formatted = combined
            else:
                # 去掉消息中的 @机器人 纯文本，换为 OneBot at 元素
                clean_text = combined
                for nick in config.BOT_NICKNAMES:
                    at_pattern = f"@{nick}"
                    if at_pattern in clean_text:
                        clean_text = clean_text.replace(at_pattern, "").strip()

                formatted = clean_text
                if sender_name:
                    formatted = f'{sender_name}在群{entry.get("group_name", contact)}中说：{clean_text}'

            # 消息段：被点名（这个群只理 @ 时必带；全量群里则看这条是否 @/斜杠叫了我）才带 at 标记
            if entry.get("mention_only") or entry.get("mentioned"):
                msg_segments = [
                    {"type": "at", "data": {"qq": str(state._self_id_int)}},
                    {"type": "text", "data": {"text": f" {formatted}"}},
                ]
            else:
                msg_segments = [
                    {"type": "text", "data": {"text": formatted}},
                ]
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=entry.get("group_name", contact),
                                       nickname=sender_name)
        else:
            sender_name = entry.get("source_name", contact)
            event = make_message_event("private", user_id,
                                       [{"type": "text", "data": {"text": combined}}],
                                       nickname=sender_name)

        # 记录 user_id → contact 映射，供 API 回复时查找
        if is_group:
            group_id = state._wxid_to_int(entry.get("group_name", contact))
            state._ob_id_to_contact[group_id] = contact
        else:
            state._ob_id_to_contact[user_id] = contact

        sent = push_event(event)
        if sent > 0:
            log.info(f"✅ 已推送至 {sent} 个 AstrBot 客户端 [{contact}]")
        else:
            log.warning(f"⚠️ 无 AstrBot 客户端在线 [{contact}]")

        with self.buffer_lock:
            if sender_id in self.pending_buffers:
                entry = self.pending_buffers[sender_id]
                entry["processing"] = False
                # 推送期间又有新消息进入缓冲区，重启定时推送避免滞留
                if entry["messages"]:
                    if entry["timer"]:
                        entry["timer"].cancel()
                    entry["timer_version"] += 1
                    version = entry["timer_version"]
                    log.info(f"📩 {contact} 缓冲期间有新消息，{config.BUFFER_SECONDS}s 后继续推送")
                    timer = threading.Timer(config.BUFFER_SECONDS, lambda v=version, sid=sender_id: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    entry["timer"] = timer

    def listen_sse(self):
        """连接 WeFlow SSE 推送。"""
        sse_url = f"{config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token={config.ACCESS_TOKEN}"
        # 日志中打码 access_token，避免泄露
        log.info(f"连接 WeFlow 推送服务: {config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token=***")
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

        try:
            self._sse_session = requests.get(sse_url, headers=headers, stream=True, timeout=None)
            if self._sse_session.status_code != 200:
                log.error(f"连接失败: HTTP {self._sse_session.status_code}")
                return
            log.info("✅ 已连接到 WeFlow 推送")

            for line in self._sse_session.iter_lines(decode_unicode=True):
                if not state.running:
                    break
                if not line:
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        msg_time = data.get("timestamp", 0)
                        if msg_time < self.start_timestamp:
                            continue
                        raw_id = data.get("rawid", "")
                        if raw_id in self.processed_ids:
                            continue
                        self.processed_ids.add(raw_id)
                        # 上限保护：超过 10000 时清理约一半，避免长跑内存膨胀
                        if len(self.processed_ids) > 10000:
                            self.processed_ids = set(list(self.processed_ids)[-5000:])
                        if not self.should_ignore(data):
                            if data.get("sessionType", "") == "group" or "@chatroom" in data.get("sessionId", ""):
                                content = data.get("content", "")
                                log.info(f"📩 群消息 [{data.get('sourceName','')}]: {content[:60]}")
                                if state.group_reply_mode == "mention":
                                    mentioned = any(f"@{n}" in content for n in config.BOT_NICKNAMES)
                                    log.info(f"   @={mentioned}")
                            else:
                                log.info(f"📩 收到: {data.get('sourceName','')} → {data.get('content','')[:50]}")
                            self.add_to_buffer(data)
                    except json.JSONDecodeError:
                        pass

        except requests.exceptions.ConnectionError:
            log.error("无法连接 WeFlow")
        except Exception as e:
            log.error(f"SSE 异常: {e}")
        finally:
            self._sse_session = None

    def _fetch_wechat_image(self, talker: str) -> str | None:
        """从 WeFlow REST API 获取最新图片并保存到本地"""
        try:
            url = f"{config.WE_FLOW_BASE_URL}/api/v1/messages"
            params = {
                "access_token": config.ACCESS_TOKEN,
                "talker": talker,
                "media": "true",
                "limit": 3,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                log.error(f"WeFlow 消息API: HTTP {resp.status_code}")
                return None

            data = resp.json()
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if not isinstance(messages, list):
                messages = []

            for msg in messages:
                if msg.get("mediaType") in ("image", "sticker", "emoji") and msg.get("mediaUrl"):
                    media_url = msg["mediaUrl"]
                    sep = "&" if "?" in media_url else "?"
                    dl_url = f"{media_url}{sep}access_token={config.ACCESS_TOKEN}"

                    img_resp = requests.get(dl_url, timeout=30)
                    if img_resp.status_code != 200:
                        continue

                    # 根据 Content-Type 确定扩展名
                    ct = img_resp.headers.get("Content-Type", "")
                    ext = ".jpg"
                    if "png" in ct: ext = ".png"
                    elif "gif" in ct: ext = ".gif"
                    elif "webp" in ct: ext = ".webp"

                    filename = f"wechat_{int(time.time())}{ext}"
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images")
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(img_resp.content)

                    log.info(f"✅ 微信图片已保存: {save_path}")
                    return save_path

            log.warning(f"消息列表无图片 mediaUrl (talker={talker})")
            return None
        except Exception as e:
            log.error(f"获取微信图片异常: {e}")
            return None

    def process_image_message(self, data):
        """处理图片消息：从 WeFlow 取图 → ollama 描述 → 注入缓冲区"""
        session_id = data.get("sessionId", "")
        source_name = data.get("sourceName", "") or "未知"
        group_name = data.get("groupName", "")
        rawid = data.get("rawid", "")

        log.info(f"🖼️ 收到图片: {source_name}" +
                 (f" (群:{group_name})" if group_name else ""))

        talker_id = data.get("talkerId", "") or data.get("sessionId", "")
        is_group = bool(group_name) or "@chatroom" in session_id

        # 与 add_to_buffer 相同规则的 buffer key：群聊为 session_id_发言人，私聊为 session_id
        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")
        session_id_data = data.get("sessionId", "") or source_name
        if is_group and sender_in_group:
            img_buffer_key = f"{session_id_data}_{sender_in_group}"
        else:
            img_buffer_key = session_id_data

        # 注册待处理的图片（ollama 完成前标记为 pending）
        img_event = threading.Event()
        self._pending_image[talker_id] = {"caption": None, "event": img_event}

        try:
            # 取图 + ollama 描述
            image_path = self._fetch_wechat_image(session_id)
            caption = None
            if image_path:
                caption = caption_image_via_ollama(image_path)

            caption_text = caption if caption else None
            if caption_text:
                log.info(f"📝 图片描述: {caption_text[:60]}...")
            else:
                log.info("⚠️ 图片描述失败")
                caption_text = "（图片内容无法描述）"

            # 注入图片描述到缓冲区
            with self.buffer_lock:
                self._pending_image[talker_id] = {"caption": caption_text, "event": img_event}

                # 批处理模式用群共享 key
                if is_group and state.group_reply_mode == "batch" and group_name:
                    g_base = re.sub(r'\s*\(\d+\)\s*$', '', group_name).strip()
                    batch_key = f"__batch__{g_base}"
                    if batch_key in self.pending_buffers:
                        entry = self.pending_buffers[batch_key]
                        entry["messages"].insert(0, f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]')
                        entry["image_ready"] = True
                        log.info(f"📝 图片已注入批处理队列")
                        return
                    # 没有文字排队，用 batch key 创建独立条目
                    self.pending_buffers[batch_key] = {
                        "messages": [f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]'],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name,
                        "is_group": True,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name,
                        "sender_in_group": source_name,
                    }
                    log.info(f"📩 图片无文本跟随，创建批处理图片条目")
                    version = 1
                    timer = threading.Timer(5, lambda v=version, sid=batch_key: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[batch_key]["timer"] = timer
                    self.pending_buffers[batch_key]["timer_version"] = version
                elif img_buffer_key in self.pending_buffers:
                    # 已有文本在排队，注入图片上下文
                    entry = self.pending_buffers[img_buffer_key]
                    entry["messages"].insert(0, f"[图片: {caption_text}]")
                    entry["image_ready"] = True
                    log.info(f"📝 图片已注入待处理文本队列")
                else:
                    # 没有文本排队，创建单条图片消息处理
                    log.info(f"📩 图片无文本跟随，直接处理")
                    self.pending_buffers[img_buffer_key] = {
                        "messages": [f"[图片: {caption_text}]"],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name if is_group and group_name else source_name,
                        "is_group": is_group,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name if is_group else "",
                        "sender_in_group": sender_in_group if is_group else "",
                    }
                    version = 1
                    timer = threading.Timer(2, lambda v=version, sid=img_buffer_key: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[img_buffer_key]["timer"] = timer
                    self.pending_buffers[img_buffer_key]["timer_version"] = version
        finally:
            # 确保 Event 被设置
            img_event.set()

    def process_emoji_message(self, data):
        """处理表情包消息：尝试下载图片并描述，失败则保留原文转发"""
        session_id = data.get("sessionId", "")
        source_name = data.get("sourceName", "") or "未知"
        group_name = data.get("groupName", "")
        content = data.get("content", "[表情]")

        log.info(f"😀 收到表情包: {source_name}" +
                 (f" (群:{group_name})" if group_name else ""))

        is_group = bool(group_name) or "@chatroom" in session_id
        # 与 add_to_buffer 相同规则的发言人 / session 标识
        sender_in_group = data.get("senderName", "") or data.get("sender", "") or data.get("sourceName", "")
        session_id_data = data.get("sessionId", "") or source_name

        # 尝试下载图片描述
        try:
            image_path = self._fetch_wechat_image(session_id)
            if image_path:
                caption = caption_image_via_ollama(image_path)
                if caption:
                    content = f"[表情: {caption}]"
                    log.info(f"😀 表情包已描述: {caption[:60]}...")
                else:
                    log.info("😀 表情包图片描述失败，保留原文")
            else:
                log.info("😀 表情包无可用图片，保留原文")

            # 直接注入缓冲区（不等待，立即推送）
            self.add_text_to_buffer(session_id_data, source_name, group_name,
                                    content, is_group, sender_in_group)
        except Exception as e:
            log.warning(f"😀 表情包处理异常: {e}")
            # 异常时也保底发送原文
            self.add_text_to_buffer(session_id_data, source_name, group_name,
                                    content, is_group, sender_in_group)

    def _inject_cached_image(self, session_id, buffer_key, version):
        """下载缓存图片 → 描述 → 注入到 buffer 条目（在缓冲计时器到期前完成）"""
        try:
            img_path = self._fetch_wechat_image(session_id)
            caption = None
            if img_path:
                caption = caption_image_via_ollama(img_path)
            text = f"[图片: {caption or '无法描述'}]"

            with self.buffer_lock:
                if buffer_key in self.pending_buffers:
                    entry = self.pending_buffers[buffer_key]
                    # 版本匹配才注入（版本变了说明被新消息重置过）
                    if entry.get("timer_version") == version:
                        entry["messages"].insert(0, text)
                        log.info(f"📸 缓存图片已注入: {text[:60]}")
                    else:
                        log.info(f"📸 缓存图片跳过（buffer 版本已变更）")
        except Exception as e:
            log.warning(f"📸 缓存图片处理异常: {e}")

    def add_text_to_buffer(self, session_id_data, source_name, group_name,
                           content, is_group, sender_in_group):
        """通用：将一段文本直接加入缓冲队列（供表情/图片等异步处理完后调用）"""
        with self.buffer_lock:
            # 与 add_to_buffer 相同规则的 buffer key
            if is_group and state.group_reply_mode == "batch":
                base_name = re.sub(r'\s*\(\d+\)\s*$', '', group_name or source_name).strip()
                buffer_key = f"__batch__{base_name}"
            elif is_group and sender_in_group:
                buffer_key = f"{session_id_data}_{sender_in_group}"
            else:
                buffer_key = session_id_data
            if buffer_key not in self.pending_buffers:
                self.pending_buffers[buffer_key] = {
                    "messages": [],
                    "timer": None,
                    "timer_version": 0,
                    "processing": False,
                    "contact": group_name if is_group and group_name else source_name,
                    "is_group": is_group,
                    "source_name": source_name,
                    "session_id_data": session_id_data,
                    "group_name": group_name if is_group else "",
                    "sender_in_group": sender_in_group if is_group else "",
                }
            entry = self.pending_buffers[buffer_key]
            entry["messages"].append(content)

            if not entry["processing"]:
                if entry["timer"]:
                    entry["timer"].cancel()
                entry["timer_version"] += 1
                version = entry["timer_version"]
                delay = 2  # 表情/图片单独推送，短缓冲
                timer = threading.Timer(delay, lambda v=version, sid=buffer_key: self.process_sender(sid, v))
                timer.daemon = True
                timer.start()
                entry["timer"] = timer
                entry["timer_version"] = version
def caption_image_via_ollama(image_path: str) -> str | None:
    """对图片进行文字描述，支持 ollama 和 OpenAI 兼容 API 两种后端。"""
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        if state.image_caption_provider == "openai":
            if not state.image_caption_api_base:
                log.warning("⚠️ provider=openai 但未配置 api_base，跳过图片描述")
                return None
            # OpenAI 兼容 API
            resp = requests.post(
                f"{state.image_caption_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {state.image_caption_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": state.image_caption_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": state.image_caption_prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            }},
                        ],
                    }],
                    "max_tokens": 800,
                },
                timeout=90,
            )
            if resp.status_code == 200:
                caption = resp.json()["choices"][0]["message"]["content"].strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"OpenAI API 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        else:
            # ollama 原生 API
            resp = requests.post(
                f"{state.ollama_base_url}/api/generate",
                json={
                    "model": state.image_caption_model,
                    "prompt": state.image_caption_prompt,
                    "images": [img_b64],
                    "stream": False,
                },
                timeout=state.ollama_timeout,
            )
            if resp.status_code == 200:
                caption = resp.json().get("response", "").strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"ollama 返回 HTTP {resp.status_code}: {resp.text[:100]}")

    except requests.Timeout:
        log.warning(f"图片描述超时 (30s)")
    except Exception as e:
        log.warning(f"图片描述失败: {e}")
    return None
