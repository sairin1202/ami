"""macOS 辅助功能（AX）树：读出前台应用窗口里可操作的控件，并按控件执行点击 / 输入 / 滚动。

Jev 不看截图，只从结构化的控件表里选「操作 + 元素编号」，这里负责把屏幕变成那张表。
坐标统一用 point（与 pyautogui 一致）。
"""

import os
import subprocess
import time
from dataclasses import dataclass, field

import ApplicationServices as AS
import Quartz
from AppKit import NSApplicationActivateIgnoringOtherApps, NSFileManager, NSRunningApplication

import computer

ATTRS = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue",
    "AXHelp", "AXEnabled", "AXFocused", "AXSelected", "AXExpanded", "AXPosition", "AXSize",
    "AXChildren",
]

CLICK_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXMenuItem",
    "AXMenuBarItem", "AXLink", "AXTab", "AXDisclosureTriangle", "AXCell", "AXRow",
    "AXSwitch", "AXToggle", "AXImage", "AXIncrementor", "AXColorWell",
}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
SCROLL_ROLES = {"AXScrollArea"}
CONTEXT_ROLES = {"AXStaticText", "AXHeading"}

MAX_NODES = 3000        # 遍历上限，网页很深时防止卡住
MAX_ELEMENTS = 150      # 发给 Jev 的可操作元素上限
MAX_TEXT = 4000         # 页面可见文字上限
MAX_DEPTH = 40

TIME_BUDGET = 1.5       # 单次遍历最多花的秒数，防止被卡死的应用拖住
WEB_AX_WAIT = 4.0       # 打开 Chromium / Electron 辅助功能后，最多等网页内容出现的秒数

BROWSER_IDS = {"com.google.Chrome", "com.apple.Safari", "company.thebrowser.Browser",
               "com.microsoft.edgemac", "com.brave.Browser"}

# 已打开过网页辅助功能的进程，键是 (pid, 启动时间)，防止 pid 被复用时误判
_enabled_apps: set[tuple[int, float]] = set()
# 全局 AX 消息超时：设在 system-wide 元素上对所有元素生效，单个应用卡住时最多等这么久
AS.AXUIElementSetMessagingTimeout(AS.AXUIElementCreateSystemWide(), 1.0)
AX_ERROR_ATTRIBUTE_UNSUPPORTED = -25205


@dataclass
class Element:
    index: str
    ref: object = field(repr=False)
    role: str
    label: str
    value: str | None
    frame: tuple[float, float, float, float]  # x, y, w, h（point）
    operations: list[str]
    actions: tuple[str, ...] = ()
    focused: bool = False
    selected: bool | None = None
    expanded: bool | None = None
    context: str = ""
    is_new: bool = False        # 上一步动作之后新出现的元素（如弹出的菜单、对话框）

    @property
    def key(self) -> tuple:
        """跨快照识别同一个元素的特征（不含编号和坐标）。"""
        return (self.role, self.label, self.context)

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.frame
        return int(x + w / 2), int(y + h / 2)

    def describe(self) -> str:
        role = self.role.removeprefix("AX")
        return f"[{self.index}] {role} {self.label!r}"

    def payload(self) -> dict:
        d = {"index": self.index, "role": self.role.removeprefix("AX"), "label": self.label,
             "operations": self.operations}
        if self.value:
            d["value"] = self.value[:200]
        for k in ("focused", "selected", "expanded"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.context:
            d["context"] = self.context
        if self.is_new:
            d["new"] = True
        return d


@dataclass
class Snapshot:
    app_name: str
    bundle_id: str
    pid: int
    window_title: str
    elements: list[Element]
    text: str

    def signature(self) -> tuple:
        """用于判断动作后界面是否有变化。"""
        return (self.window_title, self.text[:1500],
                tuple((e.label, e.value, e.focused, e.selected) for e in self.elements))


# ---------------------------------------------------------------------------
def _clean(v):
    """CopyMultipleAttributeValues 对缺失属性返回 AXError 类型的 AXValue，统一转成 None。"""
    if v is None:
        return None
    if isinstance(v, AS.AXValueRef):
        t = AS.AXValueGetType(v)
        if t == AS.kAXValueAXErrorType:
            return None
        if t == AS.kAXValueCGPointType:
            ok, p = AS.AXValueGetValue(v, t, None)
            return (p.x, p.y) if ok else None
        if t == AS.kAXValueCGSizeType:
            ok, s = AS.AXValueGetValue(v, t, None)
            return (s.width, s.height) if ok else None
        return None
    return v


def _attrs(ref) -> dict:
    err, values = AS.AXUIElementCopyMultipleAttributeValues(ref, ATTRS, 0, None)
    if err or values is None:
        return {}
    return {k: _clean(v) for k, v in zip(ATTRS, values)}


def _text(v) -> str | None:
    if isinstance(v, str):
        v = " ".join(v.split())
        return v or None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def target_app():
    """最上层的普通窗口所属的应用（跳过本程序自己的窗口）。"""
    me = os.getpid()
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for w in wins:
        pid = int(w.get("kCGWindowOwnerPID", 0))
        if w.get("kCGWindowLayer") == 0 and pid != me:
            return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return None


def read(app=None, activate: bool = True, _web_retry: int = 0) -> Snapshot | None:
    app = app or target_app()
    if app is None:
        return None
    pid = int(app.processIdentifier())
    if activate and not app.isActive():
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.25)
    root = AS.AXUIElementCreateApplication(pid)
    window = _main_window(root)
    if app.bundleIdentifier() not in BROWSER_IDS:
        # 浏览器打开网页辅助功能后会变慢（重网页尤其明显），只在 browser_read_page 真要读网页时才打开；
        # Electron 应用（Slack、飞书等）不打开的话什么控件都读不到，照常打开
        _enable_web_ax(app, root, window)

    sw, sh = computer.screen_size()
    started = time.time()
    web_areas = []
    elements: list[Element] = []
    texts: list[str] = []
    text_len = 0
    visited = 0
    win_title = ""

    def visible(frame):
        x, y, w, h = frame
        return w > 1 and h > 1 and x + w > 0 and y + h > 0 and x < sw and y < sh

    def walk(ref, depth, context):
        nonlocal visited, text_len, win_title
        if (visited >= MAX_NODES or len(elements) >= MAX_ELEMENTS or depth > MAX_DEPTH
                or time.time() - started > TIME_BUDGET):
            return
        visited += 1
        a = _attrs(ref)
        if not a:
            return
        role = a.get("AXRole") or ""
        subrole = a.get("AXSubrole") or ""
        if role == "AXWebArea":
            web_areas.append(ref)
        pos, size = a.get("AXPosition"), a.get("AXSize")
        frame = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        if frame and not visible(frame) and role not in ("AXWindow", "AXScrollArea", "AXWebArea", "AXGroup"):
            return  # 屏幕外的控件不发给 Jev

        title = _text(a.get("AXTitle"))
        desc = _text(a.get("AXDescription"))
        value = _text(a.get("AXValue"))
        placeholder = _text(a.get("AXPlaceholderValue"))
        help_ = _text(a.get("AXHelp"))
        if role == "AXWindow" and not win_title:
            win_title = title or ""

        if role in CONTEXT_ROLES:
            t = value or title or desc
            if t and text_len < MAX_TEXT:
                texts.append(t)
                text_len += len(t) + 1

        label = title or desc or placeholder or help_ or ""
        ops = []
        enabled = a.get("AXEnabled") is not False
        if enabled and frame and visible(frame):
            if role in TEXT_ROLES or subrole == "AXSecureTextField":
                ops.append("TYPE_TEXT")
            if role in CLICK_ROLES or role in TEXT_ROLES:
                ops.append("CLICK")
            if role in SCROLL_ROLES:
                ops += ["SCROLL_DOWN", "SCROLL_UP"]
            if role == "AXImage" and not label:
                ops = []  # 没有名字的图片 Jev 无从判断
            if role in ("AXCell", "AXRow") and not label:
                label = _child_text(ref)
        if ops and (label or role in TEXT_ROLES or role in SCROLL_ROLES):
            err, actions = AS.AXUIElementCopyActionNames(ref, None)
            elements.append(Element(
                index=str(len(elements) + 1), ref=ref, role=role,
                label=(label or placeholder or role.removeprefix("AX"))[:120],
                value=None if role not in TEXT_ROLES and role not in ("AXCheckBox", "AXRadioButton", "AXPopUpButton")
                else value,
                frame=frame, operations=ops, actions=tuple(actions or ()) if not err else (),
                focused=bool(a.get("AXFocused")), selected=a.get("AXSelected"),
                expanded=a.get("AXExpanded"), context=context,
            ))

        children = a.get("AXChildren") or []
        child_ctx = context
        if role in ("AXGroup", "AXToolbar", "AXSheet", "AXTabGroup", "AXList", "AXOutline", "AXTable") and label:
            child_ctx = label[:60]
        for c in children:
            walk(c, depth + 1, child_ctx)

    # 菜单栏：先放展开着的菜单（此时窗口里的控件点不到），再放顶层菜单，最后是窗口。
    # 关着的菜单尺寸为 0，不展开遍历。
    err, menubar = AS.AXUIElementCopyAttributeValue(root, "AXMenuBar", None)
    top_items = []
    if not err and menubar is not None:
        top_items = _attrs(menubar).get("AXChildren") or []
    for item in top_items:
        for menu in _attrs(item).get("AXChildren") or []:
            m = _attrs(menu)
            if m.get("AXRole") == "AXMenu" and m.get("AXSize") and m["AXSize"][1] > 1:
                walk(menu, 0, "菜单 " + (_text(_attrs(item).get("AXTitle")) or ""))
    for item in top_items:
        walk(item, MAX_DEPTH, "菜单栏")  # 深度到顶：只取菜单名本身
    n_menu = len(elements)
    if window is not None:
        walk(window, 0, "")

    # 网页只暴露了一个稀疏的代理树（元素很少、没有输入框）时，按一下网页区域让它展开，再读一次
    in_window = elements[n_menu:]
    if (web_areas and _web_retry < 2 and len(in_window) <= 20
            and not any("TYPE_TEXT" in e.operations for e in in_window)):
        AS.AXUIElementPerformAction(web_areas[0], "AXPress")
        time.sleep(0.15)
        return read(app, activate=False, _web_retry=_web_retry + 1)
    return Snapshot(app.localizedName() or "", app.bundleIdentifier() or "", pid,
                    win_title, _dedupe(elements), "\n".join(texts)[:MAX_TEXT])


def _contains(outer, inner, ratio=0.99) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    w = min(ox + ow, ix + iw) - max(ox, ix)
    h = min(oy + oh, iy + ih) - max(oy, iy)
    return w > 0 and h > 0 and w * h >= ratio * iw * ih


def _dedupe(elements: list[Element]) -> list[Element]:
    """去掉被另一个可点击元素几乎完全包住、名字相同或没有名字的可点击子元素（按钮里的图片 / 文字等），
    缩短发给 Jev 的元素表（参考 browser-use 的 bounding-box 过滤）。输入框始终保留。重新编号。"""
    keep = []
    for e in elements:
        dup = (e.operations == ["CLICK"] and any(
            p is not e and "CLICK" in p.operations and p.frame and e.frame and p.frame != e.frame
            and _contains(p.frame, e.frame) and (e.label == p.label or not e.label)
            for p in elements))
        if not dup:
            keep.append(e)
    for i, e in enumerate(keep, 1):
        e.index = str(i)
    return keep


def mark_new(snap: Snapshot, previous: Snapshot | None) -> int:
    """和上一次快照比较，给新出现的元素打标记，返回新元素个数。"""
    if previous is None:
        return 0
    old = {e.key for e in previous.elements}
    n = 0
    for e in snap.elements:
        e.is_new = e.key not in old
        n += e.is_new
    return n


def _main_window(root):
    err, window = AS.AXUIElementCopyAttributeValue(root, "AXFocusedWindow", None)
    if err or window is None:
        err, wins = AS.AXUIElementCopyAttributeValue(root, "AXWindows", None)
        window = wins[0] if not err and wins else None
    return window


def _has_web_area(ref, depth=0, budget=None) -> bool:
    budget = budget if budget is not None else [400]
    if ref is None or depth > 10 or budget[0] <= 0:
        return False
    budget[0] -= 1
    a = _attrs(ref)
    if a.get("AXRole") == "AXWebArea":
        return True
    return any(_has_web_area(c, depth + 1, budget) for c in a.get("AXChildren") or [])


def _enable_web_ax(app, root, window) -> None:
    """Chrome / Electron 默认不向辅助功能暴露网页内容。写入 AXManualAccessibility（不支持时退回
    AXEnhancedUserInterface），写成功后轮询等网页区域出现（做法参考 trycua/cua 的 ax/enablement.rs）。"""
    launched = app.launchDate()
    key = (int(app.processIdentifier()), launched.timeIntervalSince1970() if launched else 0.0)
    if key in _enabled_apps:
        return
    _enabled_apps.add(key)
    err = AS.AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)
    if err == AX_ERROR_ATTRIBUTE_UNSUPPORTED:
        err = AS.AXUIElementSetAttributeValue(root, "AXEnhancedUserInterface", True)
    if err != 0 or window is None:
        return  # 原生应用不支持这两个属性，本来就暴露完整的控件
    deadline = time.time() + WEB_AX_WAIT
    while time.time() < deadline:
        if _has_web_area(_main_window(root)):
            time.sleep(0.5)  # 网页区域出现后，子节点还要一会儿才建好
            return
        time.sleep(0.1)


def _child_text(ref, limit=3) -> str:
    """表格行 / 单元格常常自身没有名字，取前几个子孙节点的文字。"""
    out = []

    def walk(r, depth):
        if len(out) >= limit or depth > 4:
            return
        a = _attrs(r)
        t = _text(a.get("AXValue")) or _text(a.get("AXTitle")) or _text(a.get("AXDescription"))
        if t and a.get("AXRole") in ("AXStaticText", "AXTextField", "AXImage"):
            out.append(t)
        for c in a.get("AXChildren") or []:
            walk(c, depth + 1)

    walk(ref, 0)
    return " · ".join(out)[:120]


# ---------------------------------------------------------------------------
class NoEffect(RuntimeError):
    """动作执行了但界面没有反应（AXPress 不管有没有效果都返回成功，所以要自己核对）。"""


TOGGLE_ROLES = {"AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"}


def _value(ref):
    err, v = AS.AXUIElementCopyAttributeValue(ref, "AXValue", None)
    return None if err else v


def _click_point(el: Element) -> None:
    computer.execute({"type": "click", "x": el.center[0] * computer.IMAGE_SCALE,
                      "y": el.center[1] * computer.IMAGE_SCALE})


def click(el: Element) -> None:
    """优先 AXPress（不依赖坐标、不受遮挡影响）；元素没声明 AXPress 时点击元素中心。
    勾选框 / 单选框按下前后各读一次值，没变化就改用坐标点击再核对一次，仍不变则抛 NoEffect。"""
    before = _value(el.ref) if el.role in TOGGLE_ROLES else None
    if "AXPress" in el.actions and AS.AXUIElementPerformAction(el.ref, "AXPress") == 0:
        pass
    else:
        _click_point(el)
    if el.role not in TOGGLE_ROLES or before is None:
        return
    time.sleep(0.3)
    if _value(el.ref) != before:
        return
    _click_point(el)
    time.sleep(0.3)
    if _value(el.ref) == before:
        raise NoEffect(f"点击「{el.label}」后它的状态没有变化")


def type_text(el: Element, text: str) -> None:
    """聚焦输入框，全选后输入（替换原有内容），然后读回输入框的值核对。"""
    if AS.AXUIElementSetAttributeValue(el.ref, "AXFocused", True) != 0 or el.role == "AXComboBox":
        _click_point(el)
    time.sleep(0.1)
    computer.execute({"type": "keypress", "keys": ["CMD", "A"]})
    computer.execute({"type": "type", "text": text})
    time.sleep(0.2)
    got = _value(el.ref)
    if isinstance(got, str) and "".join(text.split()) not in "".join(got.split()):
        raise NoEffect(f"输入后「{el.label}」里的内容和要输入的不一致")


def scroll(el: Element, down: bool) -> None:
    x, y = el.center
    computer.execute({"type": "scroll", "x": x * computer.IMAGE_SCALE, "y": y * computer.IMAGE_SCALE,
                      "scroll_x": 0, "scroll_y": 400 if down else -400})


# ---------------------------------------------------------------------------
APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
            os.path.expanduser("~/Applications"), "/Applications/Utilities"]
_apps_cache: dict[str, dict] | None = None


def installed_apps() -> dict[str, dict]:
    """{编号: {name, path}}，name 用系统显示名（中文系统下是「备忘录」而不是 Notes）。"""
    global _apps_cache
    if _apps_cache is None:
        fm = NSFileManager.defaultManager()
        seen, apps = set(), []
        for d in APP_DIRS:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                if not n.endswith(".app"):
                    continue
                path = os.path.join(d, n)
                display = str(fm.displayNameAtPath_(path)).removesuffix(".app")
                base = n.removesuffix(".app")
                if display in seen:
                    continue
                seen.add(display)
                apps.append({"name": display if display == base else f"{display} ({base})", "path": path})
        _apps_cache = {f"A{i + 1}": a for i, a in enumerate(apps)}
    return _apps_cache


def open_app(path: str, timeout: float = 5.0) -> None:
    """打开 / 切换到应用，等它的窗口到最前面。"""
    subprocess.run(["open", "-a", path], check=True, timeout=10)
    deadline = time.time() + timeout
    while time.time() < deadline:
        app = target_app()
        if app is not None and app.bundleURL() is not None and app.bundleURL().path() == path:
            time.sleep(0.3)
            return
        time.sleep(0.15)


# ---------------------------------------------------------------------------
OCR_MIN_ELEMENTS = 8    # 窗口里的可操作控件少于这个数时，用 OCR 识别出的文字补充元素表


def _window_frame(pid: int):
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    best = None
    for w in wins:
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer") != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        f = (b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0))
        if f[2] * f[3] > (best[2] * best[3] if best else 0):
            best = f
    return best


def augment_with_ocr(snap: Snapshot) -> int:
    """应用几乎不暴露控件时（微信、游戏、Canvas 等），把窗口里 OCR 识别出的文字当作可点击元素加进元素表，
    让 Jev 也能操作（参考 OmniParser：落在已有控件里的文字不重复添加）。返回新增个数。"""
    in_window = [e for e in snap.elements if e.context not in ("菜单栏",) and not e.context.startswith("菜单 ")]
    if len(in_window) >= OCR_MIN_ELEMENTS:
        return 0
    frame = _window_frame(snap.pid)
    if frame is None or frame[2] < 200 or frame[3] < 150:
        return 0
    import ocr
    try:
        boxes = ocr.recognize(frame)
    except ocr.OCRError:
        return 0
    added = 0
    for b in boxes:
        f = (b.x, b.y, b.w, b.h)
        if not b.text or b.conf < 0.3 or any(e.frame and _contains(e.frame, f, 0.8) for e in snap.elements):
            continue
        snap.elements.append(Element(index=str(len(snap.elements) + 1), ref=None, role="AXStaticText",
                                     label=b.text[:120], value=None, frame=f, operations=["CLICK"],
                                     context="屏幕文字（OCR）"))
        added += 1
        if len(snap.elements) >= MAX_ELEMENTS:
            break
    if added and not snap.text:
        snap.text = "\n".join(b.text for b in boxes)[:MAX_TEXT]
    return added
