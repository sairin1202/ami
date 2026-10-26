"""macOS 本机电脑控制：截图 + 执行 OpenAI computer tool 返回的动作。

坐标约定：发给模型的截图会缩放到「逻辑分辨率」(pyautogui.size()，即 point 坐标)，
再乘以 IMAGE_SCALE。模型返回的坐标按截图像素给出，执行前除以 IMAGE_SCALE 映射回屏幕。
"""

import base64
import io
import os
import subprocess
import time

import mss
import pyautogui
from PIL import Image

pyautogui.FAILSAFE = True  # 鼠标甩到屏幕角落可紧急中断
pyautogui.PAUSE = 0.05

# 截图缩放比例（相对逻辑分辨率）。1.0 最准；0.7 上传量约减半、模型回复更快，点击精度对常见按钮够用。
IMAGE_SCALE = float(os.environ.get("IMAGE_SCALE", "0.7"))

KEY_MAP = {
    "ENTER": "enter", "RETURN": "enter",
    "ESC": "esc", "ESCAPE": "esc",
    "TAB": "tab", "SPACE": "space",
    "BACKSPACE": "backspace", "DELETE": "delete", "DEL": "delete",
    "HOME": "home", "END": "end",
    "PAGEUP": "pageup", "PAGEDOWN": "pagedown",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
    "ARROWUP": "up", "ARROWDOWN": "down", "ARROWLEFT": "left", "ARROWRIGHT": "right",
    "CTRL": "ctrl", "CONTROL": "ctrl",
    "SHIFT": "shift",
    "OPTION": "option", "ALT": "option",
    "META": "command", "CMD": "command", "COMMAND": "command", "SUPER": "command",
    "CAPSLOCK": "capslock", "FN": "fn",
}

BUTTON_MAP = {"left": "left", "right": "right", "wheel": "middle", "middle": "middle"}


def normalize_key(key: str) -> str:
    return KEY_MAP.get(key.upper(), key.lower())


def screen_size() -> tuple[int, int]:
    """逻辑分辨率（point）。"""
    w, h = pyautogui.size()
    return int(w), int(h)


def image_size() -> tuple[int, int]:
    w, h = screen_size()
    return int(w * IMAGE_SCALE), int(h * IMAGE_SCALE)


def screenshot_png() -> bytes:
    """截取主屏幕并缩放到发给模型的尺寸，返回 PNG 字节。"""
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
        raw = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    target = image_size()
    if img.size != target:
        img = img.resize(target, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)  # 比 PNG 小很多，上传快
    return buf.getvalue()


def screenshot_data_url() -> tuple[str, bytes]:
    data = screenshot_png()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode(), data


def placeholder_data_url() -> str:
    """历史里旧截图的占位图。"""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "gray").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _pt(x, y) -> tuple[int, int]:
    return int(round(x / IMAGE_SCALE)), int(round(y / IMAGE_SCALE))


def _get(action, name, default=None):
    if isinstance(action, dict):
        return action.get(name, default)
    return getattr(action, name, default)


def _path_points(path) -> list[tuple[int, int]]:
    pts = []
    for p in path or []:
        if isinstance(p, (list, tuple)):
            pts.append(_pt(p[0], p[1]))
        else:
            pts.append(_pt(_get(p, "x"), _get(p, "y")))
    return pts


class _Modifiers:
    """click/drag/scroll 等动作可附带 keys（修饰键），执行期间按住。"""

    def __init__(self, keys):
        self.keys = [normalize_key(k) for k in (keys or [])]

    def __enter__(self):
        for k in self.keys:
            pyautogui.keyDown(k)

    def __exit__(self, *exc):
        for k in reversed(self.keys):
            pyautogui.keyUp(k)


def _paste_text(text: str, settle: float = 0.15) -> None:
    """用剪贴板粘贴文本（支持中文等非 ASCII），等 settle 秒让目标应用读完剪贴板后恢复原剪贴板。"""
    try:
        old = subprocess.run(["pbpaste"], capture_output=True, timeout=2).stdout
    except Exception:
        old = None
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2)
    pyautogui.hotkey("command", "v")
    time.sleep(settle)
    if old is not None:
        subprocess.run(["pbcopy"], input=old, timeout=2)


def _type_text(text: str) -> None:
    # 纯 ASCII 且不长时逐字输入（对终端等不支持粘贴的地方更稳），否则走剪贴板
    if text.isascii() and len(text) <= 200 and "\n" not in text:
        pyautogui.write(text, interval=0.01)
    else:
        _paste_text(text)


def describe(action) -> str:
    t = _get(action, "type")
    keys = _get(action, "keys")
    mod = f" +{'+'.join(keys)}" if keys and t != "keypress" else ""
    if t in ("click", "double_click", "move"):
        btn = _get(action, "button")
        btn = f" {btn}" if btn and btn != "left" else ""
        return f"{t}{btn} ({_get(action, 'x')}, {_get(action, 'y')}){mod}"
    if t == "scroll":
        return (f"scroll ({_get(action, 'x')}, {_get(action, 'y')}) "
                f"dx={_get(action, 'scroll_x', 0)} dy={_get(action, 'scroll_y', 0)}{mod}")
    if t == "drag":
        pts = [(_get(p, 'x'), _get(p, 'y')) if not isinstance(p, (list, tuple)) else tuple(p)
               for p in (_get(action, "path") or [])]
        return f"drag {pts[0] if pts else ''} → {pts[-1] if pts else ''}{mod}"
    if t == "keypress":
        return f"keypress {'+'.join(keys or [])}"
    if t == "type":
        text = _get(action, "text", "")
        return f"type {text[:60]!r}" + ("…" if len(text) > 60 else "")
    return t or "?"


def execute(action) -> None:
    t = _get(action, "type")
    keys = _get(action, "keys")

    if t == "click":
        x, y = _pt(_get(action, "x"), _get(action, "y"))
        button = _get(action, "button", "left") or "left"
        if button in ("back", "forward"):
            # macOS 上用浏览器快捷键模拟
            pyautogui.hotkey("command", "[" if button == "back" else "]")
            return
        with _Modifiers(keys):
            pyautogui.click(x, y, button=BUTTON_MAP.get(button, "left"))
    elif t == "double_click":
        x, y = _pt(_get(action, "x"), _get(action, "y"))
        with _Modifiers(keys):
            pyautogui.doubleClick(x, y)
    elif t == "move":
        x, y = _pt(_get(action, "x"), _get(action, "y"))
        with _Modifiers(keys):
            pyautogui.moveTo(x, y, duration=0.1)
    elif t == "drag":
        pts = _path_points(_get(action, "path"))
        if len(pts) < 2:
            raise ValueError("drag 需要至少两个点")
        with _Modifiers(keys):
            pyautogui.moveTo(*pts[0])
            pyautogui.mouseDown()
            for p in pts[1:]:
                pyautogui.moveTo(*p, duration=0.15)
            pyautogui.mouseUp()
    elif t == "scroll":
        x, y = _pt(_get(action, "x"), _get(action, "y"))
        sx = _get(action, "scroll_x", 0) or 0
        sy = _get(action, "scroll_y", 0) or 0
        pyautogui.moveTo(x, y)
        # 模型给的是像素（正数=向下/向右）；pyautogui 在 mac 上按「行」滚动，正数=向上/向左
        with _Modifiers(keys):
            if sy:
                pyautogui.scroll(-_lines(sy))
            if sx:
                pyautogui.hscroll(-_lines(sx))
    elif t == "keypress":
        names = [normalize_key(k) for k in (keys or [])]
        if names:
            pyautogui.hotkey(*names)
    elif t == "type":
        _type_text(_get(action, "text", ""))
    elif t == "wait":
        time.sleep(2)
    elif t == "screenshot":
        pass
    else:
        raise ValueError(f"不支持的动作: {t}")


def _lines(pixels: float) -> int:
    n = int(round(pixels / 20))
    if n == 0:
        n = 1 if pixels > 0 else -1
    return n


# 修饰键的虚拟键码 → pyautogui 键名（左右各一个）
_MODIFIER_KEYCODES = {55: "command", 54: "command", 56: "shift", 60: "shift",
                      58: "option", 61: "option", 59: "ctrl", 62: "ctrl", 63: "fn"}


def release_inputs() -> list[str]:
    """停止或出错后调用：松开仍处于按下状态的修饰键和鼠标按钮，防止 ⌘ / Shift 残留导致后续误操作。
    只处理系统报告确实按着的键，返回松开了哪些。"""
    import Quartz
    state = Quartz.kCGEventSourceStateCombinedSessionState
    released = []
    for code, name in _MODIFIER_KEYCODES.items():
        if Quartz.CGEventSourceKeyState(state, code) and name not in released:
            pyautogui.keyUp(name)
            released.append(name)
    for button, name in ((Quartz.kCGMouseButtonLeft, "left"), (Quartz.kCGMouseButtonRight, "right"),
                         (Quartz.kCGMouseButtonCenter, "middle")):
        if Quartz.CGEventSourceButtonState(state, button):
            pyautogui.mouseUp(button=name)
            released.append(f"mouse-{name}")
    return released


# 一次运行结束后不最小化的应用：本程序自己、桌面 / Dock 这类系统进程
KEEP_OPEN_BUNDLES = {"com.apple.finder", "com.apple.dock", "com.apple.systemuiserver", "com.apple.controlcenter"}


def frontmost_app() -> tuple[int, str, str] | None:
    """最前面的应用：(pid, bundle id, 名字)，取最上层普通窗口（不算本程序自己的）所属的应用。
    不用 NSWorkspace.frontmostApplication：它要靠主线程的 run loop 更新，在后台线程里会一直是旧值。"""
    import Quartz
    from AppKit import NSRunningApplication
    me = os.getpid()
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    pid = next((int(w["kCGWindowOwnerPID"]) for w in wins
                if w.get("kCGWindowLayer") == 0 and int(w.get("kCGWindowOwnerPID", 0)) != me
                and (w.get("kCGWindowBounds") or {}).get("Height", 0) > 50), None)
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
    if app is None:
        return None
    return int(app.processIdentifier()), str(app.bundleIdentifier() or ""), str(app.localizedName() or "")


def minimize_windows(pid: int) -> int:
    """用辅助功能把应用所有普通窗口最小化（浏览器就是把网页窗口收进 Dock），返回最小化了几个。"""
    import ApplicationServices as AS
    err, wins = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateApplication(pid), "AXWindows", None)
    if err or not wins:
        return 0
    n = 0
    for w in wins:
        err, sub = AS.AXUIElementCopyAttributeValue(w, "AXSubrole", None)
        if err or sub != "AXStandardWindow":
            continue
        err, done = AS.AXUIElementCopyAttributeValue(w, "AXMinimized", None)
        if not err and done:
            continue
        if AS.AXUIElementSetAttributeValue(w, "AXMinimized", True) == 0:
            n += 1
    return n


# 会抢走键盘焦点的系统弹窗（权限请求、密码 / 授权框、「允许查找本地网络设备」等）。它们在最前面时按键会落到弹窗上，
# 回车可能替用户点了「允许」，所以往应用里粘贴、按回车之前先查一下，有就停
SYSTEM_DIALOG_OWNERS = ("UserNotificationCenter", "SecurityAgent", "CoreServicesUIAgent", "universalAccessAuthWarn",
                        "coreautha", "System Settings", "系统设置", "loginwindow")


def system_dialog() -> str | None:
    """屏幕上有系统弹窗时返回它属于哪个进程，否则 None。"""
    try:
        import Quartz
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    for w in wins:
        owner = w.get("kCGWindowOwnerName") or ""
        b = w.get("kCGWindowBounds") or {}
        if owner in SYSTEM_DIALOG_OWNERS and b.get("Width", 0) > 100 and b.get("Height", 0) > 80:
            return owner
    return None
