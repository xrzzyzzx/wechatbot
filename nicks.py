"""
群成员昵称白名单（一个群一个文件）
==================================

为什么需要：
  桥接要把 AstrBot 发来的 `at` 段翻译成「@群昵称」，才能去微信的成员列表里挑人。
  这套映射原本只有「谁在群里发过言」才记一条，桥接一重启就清空，
  于是 @ 谁都查不到昵称，只能退化发纯文字。

本模块做的事：
  1. 每个群一个独立文件：nicks/<群名>.json，互不相通；
  2. 启动时先扫一遍——有文件就直接读，没有就去 WeFlow 拉一份存下来；
  3. 运行时发现新面孔，补进对应群的文件里（写盘有节流，不会狂写）。

ID 算法（跟桥接的 _wxid_to_int 保持一致的「群内唯一」版）：
  user_id = md5(f"{chatroomId}_{群昵称}") % 2**31
  同一个群里的同一个人，ID 是稳定的；不同群之间天然不会撞。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time

import requests

import config

log = logging.getLogger("ob11-bridge")

NICK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nicks")

# 每个群默认的回复策略（写进 nicks/<群名>.json 的「回复策略」里，可手改）
# 唤醒词从 config.json 的 "wake_words" 读取，留空则该群只能靠 @ 或句首斜杠唤醒
POLICY_DEFAULT = {
    "全量回复": False,        # True = 群里闲聊全推给机器人（会主动插话）
    "提到就回": True,         # True = 只要有人提到唤醒词就回
    "唤醒词": list(config.config.get("wake_words") or []),
}

_lock = threading.Lock()
_last_flush = 0.0
_dirty = set()          # 待写盘的群名
_FLUSH_INTERVAL = 10.0  # 写盘节流：10 秒最多一次


# ============ 基础工具 ============

def _safe_name(name: str) -> str:
    """把群名变成合法文件名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    s = s.strip(" .")
    return s[:80] or "unknown"


def path_for(group_name: str) -> str:
    return os.path.join(NICK_DIR, _safe_name(group_name) + ".json")


def user_id_of(chatroom_id: str, nick: str) -> int:
    """群内唯一 ID：md5(f"{chatroomId}_{昵称}")。"""
    raw = f"{chatroom_id}_{nick}"
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest()[:12], 16) % (2 ** 31)


def group_id_of(group_name: str) -> int:
    """群 ID：md5(群名)。桥接的 group_id 就是这么来的。"""
    return int(hashlib.md5((group_name or "").encode("utf-8")).hexdigest()[:12], 16) % (2 ** 31)


def group_name_of_id(gid: int) -> str:
    """反查：群 ID → 群名。

    用途：桥接刚重启时，「群号 → 群名」那张运行时的表还是空的，
    发消息会把数字群号当成微信联系人名去搜索，必然搜不到，
    聊天窗口没切过去，后面敲 @ 全部落空。用本地白名单里的群名兜底即可。
    """
    if not os.path.isdir(NICK_DIR):
        return ""
    try:
        for fn in sorted(os.listdir(NICK_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(NICK_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            name = (d.get("group") or fn[:-5]).strip()
            if name and group_id_of(name) == gid:
                return name
    except Exception:
        pass
    return ""


# ============ WeFlow 抓取 ============

def fetch_members(chatroom_id: str) -> dict:
    """从 WeFlow 拉群成员，返回 {群昵称: wxid}。"""
    base = (config.WE_FLOW_BASE_URL or "").rstrip("/")
    heads = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    r = requests.get(
        base + "/api/v1/group-members",
        headers=heads,
        params={"chatroomId": chatroom_id},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    out = {}
    for m in data.get("members", []) or []:
        nick = (m.get("displayName") or m.get("nickname") or "").strip()
        if nick:
            out[nick] = m.get("wxid", "")
    return out


def list_groups() -> list:
    """列出所有群：[(群名, chatroomId), ...]。"""
    base = (config.WE_FLOW_BASE_URL or "").rstrip("/")
    heads = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    r = requests.get(base + "/api/v1/contacts", headers=heads, timeout=20)
    r.raise_for_status()
    out = []
    for c in r.json().get("contacts", []) or []:
        if c.get("type") == "group":
            name = (c.get("displayName") or c.get("nickname") or "").strip()
            username = c.get("username", "")
            if name and username.endswith("@chatroom"):
                out.append((name, username))
    return out


# ============ 读写名单 ============

def _read_file(group_name: str, chatroom_id: str = ""):
    p = path_for(group_name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        log.warning(f"[白名单] 读 {os.path.basename(p)} 失败: {e}")
        return None
    # 群号对不上就当没这份名单（群名被改过、或者文件被挪过）
    if chatroom_id and d.get("chatroom") and d["chatroom"] != chatroom_id:
        log.warning(
            f"[白名单] {os.path.basename(p)} 里的群号是 {d['chatroom']}，"
            f"跟现在的 {chatroom_id} 不符，重新生成"
        )
        return None
    try:
        return {int(k): v for k, v in (d.get("成员") or {}).items()}
    except (TypeError, ValueError):
        return None


def _write_file(group_name: str, chatroom_id: str, members: dict, policy: dict = None):
    """members: {id(int): 昵称}；policy：这个群的回复策略，缺省继承文件里原有的。"""
    os.makedirs(NICK_DIR, exist_ok=True)

    # 保住文件里已有的回复策略（用户手改过的话不能被覆盖）
    old_policy = {}
    try:
        p = path_for(group_name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                old_policy = json.load(f).get("回复策略") or {}
    except Exception:
        old_policy = {}

    data = {
        "_说明": (
            "本群配置：成员白名单（OneBot user_id → 群昵称）+ 回复策略。"
            "由 nicks.py 自动维护，也可手工增删；每个群一份，互不相通。"
        ),
        "group": group_name,
        "chatroom": chatroom_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "回复策略": {**POLICY_DEFAULT, **old_policy, **(policy or {})},
        "成员": {str(k): v for k, v in sorted(members.items(), key=lambda kv: kv[1])},
    }
    tmp = path_for(group_name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path_for(group_name))


def ensure(group_name: str, chatroom_id: str) -> dict:
    """确保这个群的名单是最新的。

    以 WeFlow 的成员表为准（它才是真相）：
      · 能抓到 → 重建名单：改名自动跟、退群自动删、新来的自动加；
      · 抓不到 → 退回本地已有的那份，不让桥接开天窗。
    """
    cached = _read_file(group_name, chatroom_id)

    try:
        members = fetch_members(chatroom_id)
    except Exception as e:
        log.warning(f"[白名单] {group_name}: 抓取失败（{e}），先用本地那份")
        return cached or {}

    if not members:
        log.warning(f"[白名单] {group_name}: 成员表是空的，先用本地那份")
        return cached or {}

    ids = {user_id_of(chatroom_id, n): n for n in members}
    old = cached or {}
    if ids == old:
        log.info(f"[白名单] {group_name}: 名单已是最新，{len(ids)} 人")
        return ids

    added = [n for k, n in ids.items() if k not in old]
    gone = [n for k, n in old.items() if k not in ids]
    _write_file(group_name, chatroom_id, ids)
    log.info(
        f"[白名单] {group_name}: 已刷新 {len(ids)} 人"
        f"（新增 {added or '无'}，移除 {gone or '无'}）"
    )
    return ids


def load_policies() -> dict:
    """读所有群的「回复策略」，返回 {群名: 策略}。"""
    out = {}
    if not os.path.isdir(NICK_DIR):
        return out
    for fn in sorted(os.listdir(NICK_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(NICK_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        name = (d.get("group") or fn[:-5]).strip()
        if name:
            out[name] = {**POLICY_DEFAULT, **(d.get("回复策略") or {})}
    return out


def ensure_all() -> dict:
    """扫描所有群，缺谁补谁。返回 {id: 昵称} 的合集。"""
    total = {}
    try:
        groups = list_groups()
    except Exception as e:
        log.warning(f"[白名单] 拉群列表失败（{e}），只加载本地已有的名单")
        groups = []

    for name, room in groups:
        total.update(ensure(name, room))
    # 本地已有、但这轮没出现在群列表里的，也照样读进来
    if os.path.isdir(NICK_DIR):
        for fn in sorted(os.listdir(NICK_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(NICK_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in (d.get("成员") or {}).items():
                    total[int(k)] = v
            except Exception:
                continue
    log.info(f"[白名单] 全部群合计 {len(total)} 条昵称")
    return total


# ============ 运行时补录 ============

_cache = {}   # {(群名, 群号): {id: 昵称}}


def remembered(group_name: str, chatroom_id: str) -> dict:
    key = (group_name, chatroom_id)
    if key not in _cache:
        _cache[key] = _read_file(group_name, chatroom_id) or {}
    return _cache[key]


def remember(group_name: str, chatroom_id: str, user_id: int, nick: str):
    """有新面孔发言就补进对应群的名单（写盘节流）。"""
    if not (group_name and chatroom_id and user_id and nick):
        return
    members = remembered(group_name, chatroom_id)
    if members.get(user_id) == nick:
        return
    members[user_id] = nick
    with _lock:
        _dirty.add((group_name, chatroom_id))
    _maybe_flush()


def flush(force: bool = False):
    global _last_flush
    with _lock:
        if not _dirty:
            return
        if not force and time.time() - _last_flush < _FLUSH_INTERVAL:
            return
        pending = set(_dirty)
        _dirty.clear()
        _last_flush = time.time()
    for group_name, chatroom_id in pending:
        try:
            _write_file(group_name, chatroom_id, remembered(group_name, chatroom_id))
        except Exception as e:
            log.warning(f"[白名单] 回写 {group_name} 失败: {e}")


def _maybe_flush():
    try:
        flush()
    except Exception:
        pass
