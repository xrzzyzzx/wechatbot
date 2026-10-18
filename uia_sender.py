"""
uia_sender.py — 纯键盘模拟的微信消息发送器
=================================================================

原理：
  完全通过键盘快捷键操作微信，无鼠标/无 UIA ValuePattern。
  剪贴板 (pyperclip) + SendKeys 完成一切操作。

工作流：
  1. 定位微信窗口并激活到前台
  2. Ctrl+F 搜索联系人 → Enter 进入聊天
  3. 剪贴板复制消息 → Ctrl+V 粘贴 → Enter 发送
  4. 图片通过 PowerShell 复制到剪贴板 → Ctrl+V → Enter

依赖:
  pip install uiautomation pyperclip
  发送图片需要 PowerShell (Windows 自带)
"""

import ctypes
import logging
import os
import random
import subprocess
import threading
import time

log = logging.getLogger("weflow-bridge")

# 版本标记：日志里出现它，就说明跑的是「前台激活修复版」而不是旧代码。
SENDER_VERSION = "uia-keyboard/2026-09-17-窗口自愈+启动等待+mmui类名兼容"


class BaseSender:
    """消息发送器基类"""
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError

    def send_image(self, contact: str, image_path: str) -> bool:
        raise NotImplementedError


# ================================================================
# 微信主窗口定位（兼容微信 3.x / 4.x）
# ----------------------------------------------------------------
# 上游代码只用固定类名 FindWindowW('Qt51514QWindowIcon') /
# FindWindowW('WeChatMainWndForPC') 取窗口句柄。这两个类名分别是
# Qt 5.15.14 和微信 3.x 的形态，微信 4.x 改用 Qt 6，类名不同，
# 会导致激活窗口 / 切换联系人直接失败。这里改为按「进程名 + 标题 +
# 类名」综合定位，不再依赖固定类名。
# ================================================================

_WECHAT_EXES = ("weixin.exe", "wechat.exe")
_WECHAT_CLASS_HINTS = ("WeChatMainWndForPC", "Qt51514QWindowIcon")
_WECHAT_TITLE_MAIN = ("微信", "WeChat")

# 微信 4.x（Weixin.exe）改用 mmui:: 命名空间的窗口类（如 mmui::ChatWnd /
# mmui::LoginWindow），与 3.x 的 Qt51514QWindowIcon 完全不同，必须一并认。
_WECHAT_CLASS_PREFIX_HINTS = ("mmui::",)
# 登录窗口类名特征：匹配到它说明微信还没登录，属于「窗口在但不可用」。
_WECHAT_LOGIN_MARKERS = ("LoginWindow", "LoginWnd")


def _img_diff_ratio(a, b, thresh=40):
    """两张同尺寸截图的「差异像素占比」。

    为什么用它：微信用 Qt 自绘，UIA 树里几乎是空的（实测整扇窗口只暴露一个
    1×1 的点），想判断「候选弹窗有没有弹出来」既不能靠 UIA，也不想上 OCR
    （又慢又费钱）。而「输入框上方那一条带里有没有多出东西」，像素差一瞧就够。
    """
    try:
        from PIL import ImageChops
        if a.size != b.size:
            return 1.0
        diff = ImageChops.difference(a.convert("L"), b.convert("L"))
        hist = diff.histogram()
        total = sum(hist)
        return (sum(hist[thresh:]) / float(total)) if total else 0.0
    except Exception:
        return 1.0


def _window_exe_name(hwnd) -> str:
    """取窗口所属进程的可执行文件名（小写）。失败返回空串。"""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def enum_wechat_windows() -> list:
    """枚举顶层窗口中属于微信进程的，返回 [(hwnd, 标题, 类名, 进程名), ...]。"""
    import ctypes
    from ctypes import wintypes

    out = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            tbuf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, tbuf, n + 1)
            cbuf = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetClassNameW(hwnd, cbuf, 512)
            exe = _window_exe_name(hwnd)
            if exe in _WECHAT_EXES:
                out.append((int(hwnd), tbuf.value, cbuf.value, exe))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumWindows(EnumProc(_cb), 0)
    return out


def is_login_window(cls: str) -> bool:
    """类名是否为微信登录窗口（窗口存在但未登录，不可用于发消息）。"""
    return any(m in cls for m in _WECHAT_LOGIN_MARKERS)


def is_main_window_class(cls: str) -> bool:
    """类名是否像微信主窗口（兼容 3.x Qt 系与 4.x mmui:: 系）。"""
    if cls in _WECHAT_CLASS_HINTS:
        return True
    if any(cls.startswith(p) for p in _WECHAT_CLASS_PREFIX_HINTS):
        return not is_login_window(cls)
    if "QWindowIcon" in cls:
        return True
    return False


def find_wechat_hwnd() -> int:
    """定位微信主窗口句柄，兼容微信 3.x / 4.x。找不到返回 0。

    会主动跳过登录窗口（mmui::LoginWindow），避免把未登录状态的窗口
    当成可用主窗口。
    """
    wins = enum_wechat_windows()
    # 1) 标题为 微信 / WeChat 的窗口最优先（排除登录窗口）
    for hwnd, title, cls, _exe in wins:
        if title.strip() in _WECHAT_TITLE_MAIN and not is_login_window(cls):
            return hwnd
    # 2) 已知主窗口类名，或任意 Qt / mmui 窗口形态
    for hwnd, _title, cls, _exe in wins:
        if is_main_window_class(cls):
            return hwnd
    # 3) 任意有标题的微信窗口（排除登录窗口）
    for hwnd, title, cls, _exe in wins:
        if title.strip() and not is_login_window(cls):
            return hwnd
    # 4) 兜底：老式固定类名查找
    import ctypes
    for cls in _WECHAT_CLASS_HINTS:
        hwnd = ctypes.windll.user32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    return 0


def find_wechat_login_hwnd() -> int:
    """单独找微信登录窗口句柄（仅用于给出「未登录」的明确提示）。"""
    for hwnd, _title, cls, _exe in enum_wechat_windows():
        if is_login_window(cls):
            return hwnd
    return 0


class UiaSender(BaseSender):
    """
    纯键盘模拟的微信消息发送器。

    不依赖 UIA ValuePattern / InvokePattern / 鼠标点击，
    全程使用剪贴板 + SendKeys 键盘快捷键操作。
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    def __init__(self, search_enabled: bool = True, wait_seconds: int = 30):
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None

        # 最近联系人缓存（相同目标跳过搜索，快速发送）
        self._last_contact = ""

        self.search_enabled = search_enabled
        self.wait_seconds = wait_seconds
        self._last_search_ts = 0.0

        self._init()

    # ================================================================
    # 初始化
    # ================================================================

    def _init(self, wait_seconds: int = None):
        """初始化 uiautomation 并定位微信窗口。

        关键修复：不再「搜一次就放弃」。启动时若微信还没起来（或窗口正在
        刷新 / 被最小化到托盘），会每秒重试，最多等 wait_seconds 秒。
        这样桥接比微信先启动也不会永久瘫痪。
        """
        try:
            import uiautomation as auto
            self._auto = auto
        except ImportError:
            log.error("请先安装 uiautomation: pip install uiautomation")
            return

        if wait_seconds is None:
            wait_seconds = getattr(self, "wait_seconds", 30)

        log.info(f"UIA Sender 版本: {SENDER_VERSION}")
        log.info(f"正在搜索微信窗口（最多等待 {wait_seconds}s）...")

        deadline = time.time() + max(0, wait_seconds)
        attempt = 0
        while True:
            attempt += 1
            self._find_window()
            if self._window:
                log.info(
                    f"微信窗口: '{self._window.Name}' "
                    f"ClassName={self._window.ClassName}（第 {attempt} 次尝试）"
                )
                self._ready = True
                return
            if time.time() >= deadline:
                break
            if attempt == 1:
                log.warning("暂未找到微信窗口，每秒重试中…（请确认微信已启动并登录）")
            time.sleep(1.0)

        # 等待超时：区分「未登录」和「完全没窗口」，给出可操作的提示
        login_hwnd = find_wechat_login_hwnd()
        if login_hwnd:
            log.error(
                "❌ 只找到微信「登录窗口」（mmui::LoginWindow），说明微信尚未登录。"
                "请先扫码登录微信，然后重启桥接。"
            )
        else:
            log.error(
                f"❌ {wait_seconds}s 内未找到微信窗口，发送功能不可用。"
                "请确认：① 微信已启动且已登录 ② 窗口未最小化到托盘 ③ 微信是 3.x/4.x 桌面版。"
            )

    def _find_window(self):
        """按标题/类名搜索微信窗口，失败则按进程名兜底（兼容微信 3.x/4.x）。

        每次调用前先清空 _window，避免残留失效句柄让调用方误判成功。
        """
        auto = self._auto
        if auto is None:
            return
        self._window = None
        try:
            root = auto.GetRootControl()
            for w in root.GetChildren():
                if w.ClassName in ("Chrome_WidgetWin_1", "CabinetWClass"):
                    continue
                # 跳过登录窗口：窗口在但不可用，认了它后续按键全打空
                if is_login_window(w.ClassName):
                    continue
                # 标题精确匹配，避免误匹配任何标题含“微信”字样的窗口
                if w.Name in self.WECHAT_TITLES:
                    self._window = w
                    return
                # 微信主窗口类名匹配（兼容 3.x Qt 系与 4.x mmui:: 系）
                if is_main_window_class(w.ClassName):
                    self._window = w
                    return
        except Exception as e:
            log.warning(f"枚举顶层窗口异常: {e}")

        # 兜底：按进程名定位（微信 4.x 的 mmui 窗口类名与 3.x 不同）
        hwnd = find_wechat_hwnd()
        if hwnd:
            try:
                w = auto.ControlFromHandle(hwnd)
                if w:
                    self._window = w
                    return
            except Exception as e:
                log.warning(f"按句柄取窗口失败: {e}")

    def _hwnd(self) -> int:
        """取微信主窗口句柄：优先用已定位窗口自带句柄，兼容微信 3.x/4.x。"""
        try:
            if self._window:
                h = int(self._window.NativeWindowHandle)
                if h:
                    return h
        except Exception:
            pass
        return find_wechat_hwnd()

    # 相邻两次「重新搜索窗口」的最小间隔，避免每条消息都枚举一遍桌面窗口
    _RESEARCH_COOLDOWN = 2.0

    def _ensure_window(self) -> bool:
        """确保窗口可用。

        关键修复：原实现第一行就是 `if not self._ready: return False`，把下面
        的「重新搜索」自愈路径彻底堵死了 —— 启动瞬间找不到微信窗口，这个进程
        余生就永久瘫痪，只能重启。现在改成：无论 _ready 是什么，只要当前句柄
        失效就重新搜索，搜到就补记 _ready = True，允许自愈。
        """
        # 1) 已有窗口且仍然有效 → 直接可用
        if self._window is not None:
            try:
                if self._window.Exists(0.2):
                    self._ready = True
                    return True
            except Exception:
                pass
            log.warning("微信窗口句柄已失效，尝试重新搜索…")
            self._window = None

        # 2) 句柄缺失/失效 → 重新搜索（带冷却，防止高频调用时反复枚举）
        now = time.time()
        if now - getattr(self, "_last_search_ts", 0.0) < self._RESEARCH_COOLDOWN:
            return False
        self._last_search_ts = now

        self._find_window()
        if not self._window:
            log.warning("微信窗口未找到（请确认微信已启动并登录，且未最小化到托盘）")
            self._ready = False
            return False

        # 3) 自愈成功 —— 补上这一句，这是原代码缺失的关键
        if not self._ready:
            log.info(
                f"微信窗口已找回: '{self._window.Name}' "
                f"ClassName={self._window.ClassName}（发送功能恢复）"
            )
        self._ready = True
        return True

    # ================================================================
    # 底层按键注入（不再使用 uiautomation 的 SendKeys）
    # ----------------------------------------------------------------
    # 为什么不能用 auto.SendKeys('{Enter}')：
    #   uiautomation 内部给**每一个**特殊键都无条件加上了
    #   KEYEVENTF_EXTENDEDKEY(0x0001)，也就是注入 E0 前缀。
    #   于是 SendKeys('{Enter}') 注入的不是主键盘 Enter(VK_RETURN,scan 0x1C)，
    #   而是 **E0 1C = 小键盘 Enter(Numpad Enter)**。
    #   Qt 把两者当完全不同的键：Qt::Key_Return(主键盘) / Qt::Key_Enter(小键盘)。
    #   微信 4.x 的「发送」只绑在 Key_Return 上，所以小键盘 Enter 根本不会触发发送
    #   —— 消息被粘进输入框后一直躺在那儿，永远不会发出去。
    #   同理 Ctrl 被注入成 E0 1D = 右 Ctrl。
    #
    # 这里改成手工 keybd_event，扫描码/标志位完全自己控制。
    # ================================================================
    VK_CONTROL = 0x11
    VK_RETURN = 0x0D
    VK_ESCAPE = 0x1B
    KEYEVENTF_KEYUP = 0x0002

    @staticmethod
    def _sc(vk: int) -> int:
        return int(ctypes.windll.user32.MapVirtualKeyW(vk, 0))

    def _key_down(self, vk: int):
        ctypes.windll.user32.keybd_event(vk, self._sc(vk), 0, 0)

    def _key_up(self, vk: int):
        ctypes.windll.user32.keybd_event(vk, self._sc(vk), self.KEYEVENTF_KEYUP, 0)

    def _press(self, vk: int, hold: float = 0.06):
        """按一下某个键（不带 E0 前缀，即「主键盘」那一个）。"""
        self._key_down(vk)
        time.sleep(hold)
        self._key_up(vk)
        time.sleep(0.05)

    def _ctrl_letter(self, letter: str):
        """Ctrl + 字母，例如 _ctrl_letter('v') 就是 Ctrl+V 粘贴。"""
        vk = ord(letter.upper())
        if not (0x41 <= vk <= 0x5A):
            raise ValueError(f"只支持字母: {letter!r}")
        self._key_down(self.VK_CONTROL)
        time.sleep(0.05)
        self._press(vk, 0.05)
        self._key_up(self.VK_CONTROL)
        time.sleep(0.05)

    def _press_enter(self):
        """真·主键盘 Enter（决定能不能发出去的关键）。"""
        self._press(self.VK_RETURN)

    def _press_escape(self):
        self._press(self.VK_ESCAPE)

    # --- Windows 窗口常量 ---
    SW_RESTORE = 9
    SW_SHOW = 5

    def _activate(self) -> bool:
        """把微信窗口切到前台，返回「是否确实成为前台窗口」。

        为什么必须返回布尔值：
          Windows 有「前台锁定」——后台进程直接 SetForegroundWindow 通常会被
          静默拒绝（只在任务栏闪一下，返回值被忽略时完全看不出来）。此时后续
          的 Ctrl+F / Ctrl+V / Enter 全部打到当前真正的前台窗口（例如桥接自己的
          控制台）上，微信什么都不会收到，而代码里又没有异常，于是日志谎报成功。

        正确做法：
          先把自己这个线程挂到「当前前台窗口所在线程」的输入队列上，
          再抢前台。挂到微信自己的线程上是无效的（本项目原代码的 bug）。
        """
        hwnd = self._hwnd()
        if not hwnd:
            log.warning("_activate: 找不到微信窗口句柄")
            return False

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        try:
            # 最小化 / 收进托盘时，SetForegroundWindow 等于白调，必须先还原
            if u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, self.SW_RESTORE)
                time.sleep(0.25)
            elif not u32.IsWindowVisible(hwnd):
                u32.ShowWindow(hwnd, self.SW_SHOW)
                time.sleep(0.25)

            if u32.GetForegroundWindow() == hwnd:
                return True

            cur_tid = k32.GetCurrentThreadId()
            fg = u32.GetForegroundWindow()
            fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0

            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                u32.BringWindowToTop(hwnd)
                u32.SetForegroundWindow(hwnd)
                u32.SetFocus(hwnd)
            finally:
                if attached:
                    u32.AttachThreadInput(cur_tid, fg_tid, False)

            time.sleep(0.2)
            ok = (u32.GetForegroundWindow() == hwnd)
            if not ok:
                log.warning("_activate: SetForegroundWindow 被系统拒绝，微信未到前台")
            return ok
        except Exception as e:
            log.warning(f"_activate 异常: {e}")
            return False

    # ================================================================
    # 联系人切换
    # ================================================================

    def _switch_contact(self, contact: str) -> bool:
        """
        切换到指定联系人/群聊的聊天窗口。

        纯键盘：Ctrl+F → 粘贴联系人名 → Enter
        """
        if not self._ensure_window():
            return False

        if not self._activate():
            log.error("微信未到前台，按键会打到别的窗口，放弃切换联系人")
            return False

        try:
            auto = self._auto

            # Ctrl+F 打开搜索
            self._ctrl_letter('f')
            time.sleep(0.6)

            # Ctrl+A 全选 → 清空搜索框里已有的内容
            self._ctrl_letter('a')
            time.sleep(0.15)

            # 粘贴联系人名
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(0.15)
            self._ctrl_letter('v')
            time.sleep(0.4)

            # 主键盘 Enter 选中第一个结果
            self._press_enter()
            time.sleep(0.9)

            log.info(f"已发出切换联系人按键: {contact}（键盘方案无法确认真实切换，以下日志不代表已送达）")
            return True
        except Exception as e:
            log.error(f"切换联系人按键异常: {e}")
            return False

    # ================================================================
    # 发送文字 (纯键盘)
    # ================================================================

    def send_text(self, contact: str, text: str) -> bool:
        """
        发送文本消息。

        纯键盘方案：剪贴板 → Ctrl+V → Enter
        """
        with self._lock:
            if not self._ready:
                # 先尝试自愈一次（可能只是启动时微信还没起来 / 窗口句柄失效）
                if not self._ensure_window():
                    log.error("UIA Sender 未就绪")
                    return False

            if not self._ensure_window():
                return False

            # 安全检查：过滤 PIL 引用
            if "<PIL." in text or "PIL." in text:
                log.warning(f"跳过 PIL 引用消息: {text[:60]}")
                return False

            if not self._activate():
                log.error(f"微信未到前台，放弃发送（否则按键会打到别的窗口）: {contact}")
                return False
            auto = self._auto

            # 切换到联系人
            if self.search_enabled and contact:
                if contact != self._last_contact:
                    if not self._switch_contact(contact):
                        log.error(f"切换联系人失败，中止发送: {contact}")
                        return False
                    self._last_contact = contact

            try:
                import pyperclip

                # 模拟真人随机延时
                time.sleep(random.uniform(0.3, 1.0))

                # 复制消息到剪贴板
                pyperclip.copy(text)
                time.sleep(random.uniform(0.1, 0.3))

                # Ctrl+V 粘贴
                self._ctrl_letter('v')

                # 根据消息长度动态等待粘贴完成（长文本多等一会）
                paste_wait = min(len(text) * 0.02, 2.0) + random.uniform(0.3, 0.8)
                time.sleep(paste_wait)

                # 主键盘 Enter 发送
                self._press_enter()
                time.sleep(0.4)

                # 键盘方案拿不到「消息是否真的进入聊天窗口」的反馈，
                # 所以这里只报告「按键已发出」，不再声称已送达。
                log.info(f"[UIA→] {contact}: 按键已发出（Ctrl+V+Enter）: {text[:50]}")
                return True

            except Exception as e:
                log.error(f"[UIA✗] {contact}: {e}")
                return False

    # ================================================================
    # @某人：敲@ → 输昵称 → UIA 找候选弹窗 → 点那一行 → 粘正文 → 回车
    # ================================================================

    MENTION_POPUP_MIN_W = 100     # 弹窗最小宽度
    MENTION_POPUP_MAX_H = 120     # 只认「单一候选」的矮弹窗，多个候选不赌

    def _win_rect(self):
        try:
            import ctypes.wintypes as wt
            rc = wt.RECT()
            ctypes.windll.user32.GetWindowRect(self._hwnd(), ctypes.byref(rc))
            return (rc.left, rc.top, rc.right, rc.bottom)
        except Exception:
            return None

    def _click_at(self, x, y):
        ctypes.windll.user32.SetCursorPos(int(x), int(y)); time.sleep(0.22)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.06)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0); time.sleep(0.18)

    # 候选列表的窗口类名（实测：微信进程的独立 Qt 顶层窗口）
    MENTION_POPUP_CLASS = "Qt51514QWindowToolSaveBits"

    def _mention_popup_rects(self, l, t, r, b):
        """列出当前所有「可能是 @ 候选列表」的顶层窗口矩形。

        为什么用窗口枚举而不是 UIA 树查找：
          微信是 Qt 自绘，从主窗口往下 walk 只能拿到一个 1×1 的点；
          但候选列表本身是**独立的顶层窗口**（class=Qt51514QWindowToolSaveBits），
          Win32 枚举一眼就能看到，而且不用截图——比像素比对快得多也稳得多。
        至于列表里的条目，UIA 照样读不到（整窗只有一个空 Pane），
        所以选中那一步仍然靠「输昵称收窄 + 回车」，不读列表。
        """
        import ctypes.wintypes as wt

        out = set()
        user32 = ctypes.windll.user32

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != self.MENTION_POPUP_CLASS:
                    return True
                exe = _window_exe_name(hwnd)
                if "weixin" not in exe and "wechat" not in exe:
                    return True
                rc = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rc))
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if not (80 <= w <= 700 and 20 <= h <= 500):
                    return True
                # 必须落在微信窗口附近，免得认到别的浮窗
                if not (l - 40 <= rc.left <= r + 40 and t - 40 <= rc.top <= b + 40):
                    return True
                out.add((rc.left, rc.top, rc.right, rc.bottom))
            except Exception:
                pass
            return True

        try:
            proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(_cb)
            user32.EnumWindows(proc, 0)
        except Exception as e:
            log.debug(f"[UIA@] 枚举窗口失败: {e}")
        return out

    def _find_mention_popup(self, win, maxdepth=4):
        """候选弹窗是个独立的 Qt 窗口：UIA 读得到它的矩形，读不到里面的条目。"""
        found = []

        def walk(el, depth):
            if depth > maxdepth:
                return
            try:
                kids = el.GetChildren()
            except Exception:
                return
            for k in kids:
                try:
                    cls = k.ClassName or ""
                    r = k.BoundingRectangle
                    rr = (r.left, r.top, r.right, r.bottom)
                    if "ToolSaveBits" in cls and (rr[2] - rr[0]) >= self.MENTION_POPUP_MIN_W:
                        found.append(rr)
                except Exception:
                    pass
                walk(k, depth + 1)

        walk(win, 0)
        return max(found, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if found else None

    def _dump_qt_windows(self, win, maxdepth=4):
        """排查用：把窗口下所有像弹窗的控件（类名+矩形）打出来。"""
        out = []

        def walk(el, depth):
            if depth > maxdepth:
                return
            try:
                kids = el.GetChildren()
            except Exception:
                return
            for k in kids:
                try:
                    cls = k.ClassName or ""
                    r = k.BoundingRectangle
                    rr = (r.left, r.top, r.right, r.bottom)
                    if "Qt" in cls or "Tool" in cls or "Menu" in cls or "Popup" in cls:
                        out.append((cls, (k.Name or "")[:20], rr))
                except Exception:
                    pass
                walk(k, depth + 1)

        walk(win, 0)
        return out

    @staticmethod
    def _widest_text_row(im, rect, thresh=140):
        """在弹窗矩形内找「最宽的那一行深色文字」= 候选名字所在行。"""
        l, t, r, b = rect
        crop = im.convert("L").crop((l, t, r, b))
        w, h = crop.size
        px = crop.load()
        rows = []
        for y in range(h):
            xs = [x for x in range(w) if px[x, y] < thresh]
            rows.append((min(xs), max(xs)) if xs else None)
        out, cur = [], None
        for y, rr in enumerate(rows):
            if rr:
                if cur is None:
                    cur = [y, y, rr[0], rr[1]]
                else:
                    cur[1] = y; cur[2] = min(cur[2], rr[0]); cur[3] = max(cur[3], rr[1])
            else:
                if cur:
                    out.append(tuple(cur)); cur = None
        if cur:
            out.append(tuple(cur))
        cand = [(y0 + t, y1 + t, x0 + l, x1 + l) for (y0, y1, x0, x1) in out if y1 - y0 >= 4]
        return max(cand, key=lambda r: r[3] - r[2]) if cand else None

    def send_text_with_mention(self, contact: str, mention_name: str, text: str) -> bool:
        """@某人 + 正文。@ 失败返回 False（调用方可以退回普通文字发送）。

        流程：点聊天空白(关表情面板) → 点输入框 → 清空 → 敲 @ → 输昵称
              → UIA 拿候选弹窗矩形 → 点候选那一行 → 粘正文 → 回车
        任何一步不成立就清空输入框撤退，绝不盲点、绝不留半截草稿。
        """
        VK_SHIFT, VK_2, VK_DELETE = 0x10, 0x32, 0x2E
        with self._lock:
            if not self._ensure_window():
                return False
            if not self._activate():
                log.error(f"微信未到前台，放弃 @ 发送: {contact}")
                return False

            switched = False
            if self.search_enabled and contact and contact != self._last_contact:
                if not self._switch_contact(contact):
                    self._last_contact = None
                    return False
                self._last_contact = contact
                switched = True

            def _abort(msg):
                log.warning(f"[UIA@] {msg}，退回普通发送")
                self._ctrl_letter('a'); time.sleep(0.15)
                self._press(VK_DELETE, 0.05); time.sleep(0.3)
                return False

            try:
                import pyperclip
                from PIL import ImageGrab

                win = self._win_rect()
                if not win:
                    return False
                l, t, r, b = win
                in_x = l + int((r - l) * 0.55)
                # 0.18 是量出来的：窗口底往上 18% 正对输入框中心。
                # 原来的 0.09 会落在输入框和表情工具栏之间的缝里：
                #   ① 点下去可能把表情面板点开，@ 列表就不弹了；
                #   ② 用它算出来的取样带（in_y-340 ~ in_y）高达 300+ 像素，
                #      弹窗只占一小块，像素差低于阈值 → 永远误判「没弹窗」。
                in_y = b - int((b - t) * 0.18)

                # 关键改动（都是今晚实测逼出来的）：
                #   ① 绝不再先点「聊天空白 / 输入框」——一戳焦点或输入法状态就废，
                #      @ 会敲进空气里，候选列表根本不弹；
                #   ② 不能再靠 UIA 找弹窗——微信用 Qt 自绘，整扇窗口在 UIA 树里
                #      只暴露一个 1×1 的点（现场日志实证），永远找不到；
                #   ③ 改用「像素差」判断弹窗有没有出来，便宜、够快、不用 OCR。
                if not switched:
                    # 只有「没换过联系人」时输入框才可能没焦点，这时才点一下
                    self._click_at(in_x, in_y)
                    time.sleep(0.35)
                self._ctrl_letter('a'); time.sleep(0.12)
                self._press(VK_DELETE, 0.05); time.sleep(0.35)

                band = (l + int((r - l) * 0.02), max(t, in_y - 340), r, in_y - 6)
                base = ImageGrab.grab(bbox=band)
                # UIA 通路：先记下「敲 @ 之前」已经存在的浮窗，敲完只认新冒出来的那个
                popups_before = self._mention_popup_rects(l, t, r, b)

                popped = False
                detect_by = ""
                for attempt in (1, 2, 3):
                    self._key_down(VK_SHIFT); time.sleep(0.05)
                    self._press(VK_2, 0.08)
                    self._key_up(VK_SHIFT); time.sleep(0.9)

                    # ① 首选 UIA/Win32：候选列表是独立顶层窗口，出现即命中，不用截图
                    fresh = self._mention_popup_rects(l, t, r, b) - popups_before
                    if fresh:
                        popped = True
                        detect_by = f"UIA 窗口 {sorted(fresh)[0]}"
                        break

                    # ② 备胎 像素差：主人要求先留着，UIA 万一失灵还有它顶着
                    try:
                        if base is not None and _img_diff_ratio(base, ImageGrab.grab(bbox=band)) > 0.015:
                            popped = True
                            detect_by = "像素差"
                            break
                    except Exception as e:
                        log.debug(f"[UIA@] 像素比对失败: {e}")

                    log.warning(f"[UIA@] 第 {attempt} 次敲 @，UIA 和像素都没看到候选弹窗")
                    self._ctrl_letter('a'); time.sleep(0.12)
                    self._press(VK_DELETE, 0.05); time.sleep(0.3)

                if not popped:
                    log.warning(
                        f"[UIA@] 敲 @ 后没检测到候选弹窗（窗口={win} 输入点=({in_x},{in_y})）"
                    )
                    return _abort("敲 @ 之后候选弹窗没出来")
                log.info(f"[UIA@] 已检测到候选弹窗（{detect_by}）")

                # 4) 输昵称把候选收窄（走剪贴板，中文昵称也能输）
                pyperclip.copy(mention_name); time.sleep(0.2)
                self._ctrl_letter('v'); time.sleep(1.0)

                # 5) 回车选中当前高亮的第一条候选：
                #    不点鼠标、不读列表——昵称筛完通常只剩一条，回车最稳，
                #    也绕开了「UIA 读不到弹窗内容」这个死结。
                self._press_enter(); time.sleep(0.65)
                log.info(f"[UIA@] 已回车选中候选 {mention_name!r}")

                # 6) 粘正文 + 回车
                pyperclip.copy(text); time.sleep(0.25)
                self._ctrl_letter('v')
                time.sleep(min(len(text) * 0.02, 2.0) + random.uniform(0.3, 0.8))
                self._press_enter()
                time.sleep(0.4)
                log.info(f"[UIA@→] {contact}: 已发 @{mention_name} + 正文（键盘方案无法回读）")
                return True
            except Exception as e:
                log.error(f"[UIA@✗] {contact}: {e}")
                try:
                    self._ctrl_letter('a'); self._press(VK_DELETE, 0.05)
                except Exception:
                    pass
                return False

    # ================================================================
    # 发送图片 (剪贴板 + 纯键盘)
    # ================================================================

    def send_image(self, contact: str, image_path: str) -> bool:
        """
        发送图片。

        方案：PowerShell 复制图片到剪贴板 → Ctrl+V → Enter
        """
        with self._lock:
            if not self._ready:
                return False
            if not os.path.isfile(image_path):
                log.error(f"图片不存在: {image_path}")
                return False

            try:
                if not self._ensure_window():
                    return False
                if not self._activate():
                    log.error(f"微信未到前台，放弃发送图片: {contact}")
                    return False
                auto = self._auto

                if self.search_enabled and contact:
                    if contact != self._last_contact:
                        if not self._switch_contact(contact):
                            log.error(f"切换联系人失败，中止发送: {contact}")
                            return False
                        self._last_contact = contact

                time.sleep(random.uniform(0.3, 0.8))

                # PowerShell 复制图片到剪贴板
                self._copy_image_to_clipboard(image_path)
                time.sleep(0.3)

                # Ctrl+V 粘贴
                self._ctrl_letter('v')
                time.sleep(random.uniform(0.8, 1.5))  # 等待微信加载图片预览

                # 主键盘 Enter 发送
                self._press_enter()

                log.info(f"[UIA→] 图片按键已发出 → {contact}: {os.path.basename(image_path)}")
                return True

            except Exception as e:
                log.error(f"[UIA✗] 图片 → {contact}: {e}")
                return False

    def _copy_image_to_clipboard(self, path: str):
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）"""
        abs_path = os.path.abspath(path)
        # PowerShell 单引号字符串内需将 ' 转义为 ''，防止路径含单引号破坏命令/注入
        ps_path = abs_path.replace("'", "''")
        try:
            subprocess.run([
                "powershell", "-WindowStyle", "Hidden", "-Command",
                f"Add-Type -AssemblyName System.Windows.Forms;"
                f"Add-Type -AssemblyName System.Drawing;"
                f"$img = [System.Drawing.Image]::FromFile('{ps_path}');"
                f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                f"$img.Dispose()"
            ], check=True, timeout=10)
            log.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            log.error(f"复制图片到剪贴板失败: {e}")
            raise
