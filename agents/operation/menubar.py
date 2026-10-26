"""盯右上角菜单栏里聊天软件的图标：图标变了才让模型看一眼是不是来了新消息。

平时每隔几秒只做两件便宜的事：用辅助功能找到图标的位置和标题（微信的标题就是未读数，比如「 1」），
截下这一小块（几十个像素）和上次比。不调模型、不动鼠标键盘，也不把应用切到前台。
图标有变化时，把变化前后两张小图交给模型判断「是不是来了新消息」，是的话再交给消息巡检去读、决定回不回。

目前只有微信在菜单栏有图标；Lark 没有，靠空闲时的定时巡检。
"""

import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass

import ApplicationServices as AS
import mss
from AppKit import NSWorkspace
from PIL import Image, ImageChops, ImageStat

# 进程名 → 显示名。只看这些应用的菜单栏图标
# 进程名 → 显示名。Lark 的菜单栏图标属于它的一个「Lark Helper」子进程（同名的有好几个，挑有图标的那个）
APPS = {"WeChat": "微信", "Lark Helper": "Lark"}
DIFF_MIN = float(os.environ.get("MENUBAR_DIFF_MIN", "6"))  # 灰度平均差（0–255）超过它才算图标变了


@dataclass
class Icon:
    app: str               # 进程名
    rect: tuple            # (x, y, w, h)，point
    title: str             # 图标的辅助功能标题（微信是未读数），没有就是空串
    image: Image.Image


@dataclass
class Change:
    app: str
    before: Icon
    after: Icon


def _pids(proc: str) -> list[int]:
    out = []
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        url = a.executableURL()
        if url is not None and url.lastPathComponent() == proc:
            out.append(int(a.processIdentifier()))
    return out


def _extras_item(pid: int):
    """进程在菜单栏右侧的第一个图标（AX 元素），没有就 None。"""
    err, bar = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateApplication(pid), "AXExtrasMenuBar", None)
    if err or bar is None:
        return None
    err, items = AS.AXUIElementCopyAttributeValue(bar, "AXChildren", None)
    return None if err or not items else items[0]


def _value(v):
    if isinstance(v, AS.AXValueRef):
        t = AS.AXValueGetType(v)
        ok, p = AS.AXValueGetValue(v, t, None)
        if not ok:
            return None
        return (p.x, p.y) if t == AS.kAXValueCGPointType else (p.width, p.height)
    return v


def locate(proc: str) -> tuple[tuple, str] | None:
    """应用在菜单栏右侧的图标：((x, y, w, h), 标题)。应用没开或没有图标时返回 None。"""
    item = next((it for it in map(_extras_item, _pids(proc)) if it is not None), None)
    if item is None:
        return None
    vals = {}
    for attr in ("AXPosition", "AXSize", "AXTitle"):
        err, v = AS.AXUIElementCopyAttributeValue(item, attr, None)
        vals[attr] = None if err else _value(v)
    if not vals["AXPosition"] or not vals["AXSize"]:
        return None
    (x, y), (w, h) = vals["AXPosition"], vals["AXSize"]
    return (x, y, w, h), str(vals["AXTitle"] or "").strip()


def grab(rect: tuple) -> Image.Image:
    x, y, w, h = rect
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
        raw = sct.grab({"left": int(x), "top": int(y), "width": max(1, int(w)), "height": max(1, int(h))})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def snapshot(proc: str) -> Icon | None:
    found = locate(proc)
    if found is None:
        return None
    rect, title = found
    return Icon(proc, rect, title, grab(rect))


def differs(a: Icon, b: Icon) -> bool:
    if a.title != b.title:
        return True
    if a.image.size != b.image.size:  # 图标变宽 / 变窄（未读数位数变了）
        return True
    diff = ImageChops.difference(a.image.convert("L"), b.image.convert("L"))
    return ImageStat.Stat(diff).mean[0] > DIFF_MIN


class Watcher:
    """记着每个图标上次的样子，poll() 返回变了的图标。图标闪动时等它稳定下来再算。"""

    def __init__(self, apps: dict | None = None, settle: float = 0.6):
        self.apps = apps or APPS
        self.settle = settle
        self.base: dict[str, Icon] = {}

    def poll(self) -> list[Change]:
        changes = []
        for proc in self.apps:
            try:
                now = snapshot(proc)
            except Exception:  # noqa: BLE001  截图 / 辅助功能偶尔失败，下一轮再看
                continue
            if now is None:
                self.base.pop(proc, None)
                continue
            old = self.base.get(proc)
            if old is None or old.rect[:2] != now.rect[:2] and old.title == now.title:
                self.base[proc] = now  # 第一次看到，或只是图标挪了位置（别的图标增减）：只记下
                continue
            if not differs(old, now):
                continue
            time.sleep(self.settle)
            again = snapshot(proc)
            if again is None or differs(now, again):
                continue  # 还在闪，下一轮再看
            self.base[proc] = again
            changes.append(Change(proc, old, again))
        return changes

    def rebase(self) -> None:
        """处理完消息后（巡检会打开聊天，未读数会变）重新记下当前样子，免得把自己造成的变化当成新消息。"""
        self.base.clear()
        self.poll()


# ---------------------------------------------------------------------------
JUDGE_PROMPT = """下面是 macOS 菜单栏右上角「{name}」图标变化前后的两张截图（已放大），以及图标的辅助功能标题（{name}里一般是未读数）。
变化前标题：「{t0}」；变化后标题：「{t1}」。
判断这次变化是不是「来了新消息、需要去看一眼」。未读数变多、出现红点 / 数字是新消息；
未读数变少或消失（用户自己看过了）、只是深浅色 / 背景变了、图标闪了一下又恢复，都不是。
只输出一个 JSON 对象：{{"new_message": true 或 false, "unread": 变化后的未读数（看不出就 null）, "reason": "一句话"}}"""


def _data_url(img: Image.Image) -> str:
    img = img.resize((img.width * 4, img.height * 4), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def judge(change: Change, client, model: str) -> dict:
    """让模型看图标前后两张图，判断是不是新消息。返回 {"new_message", "unread", "reason"}。"""
    name = APPS.get(change.app, change.app)
    resp = client.responses.create(model=model, store=False, input=[{"role": "user", "content": [
        {"type": "input_text", "text": JUDGE_PROMPT.format(name=name, t0=change.before.title, t1=change.after.title)},
        {"type": "input_image", "image_url": _data_url(change.before.image)},
        {"type": "input_image", "image_url": _data_url(change.after.image)},
    ]}])
    text = getattr(resp, "output_text", "") or ""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except ValueError:
        out = {}
    return {"new_message": bool(out.get("new_message")), "unread": out.get("unread"),
            "reason": str(out.get("reason") or text[:200])}


if __name__ == "__main__":
    # python menubar.py          # 打印当前图标位置和标题
    # python menubar.py watch    # 盯着图标，变了就打印（加 --judge 再让模型判断）
    import sys

    if sys.argv[1:2] == ["watch"]:
        client = model = None
        if "--judge" in sys.argv:
            import paths
            paths.load_env()
            import agent as agent_mod
            a = agent_mod.Agent(api_key=agent_mod.api_key_from_env(),
                                model=os.environ.get("OPENAI_MODEL") or agent_mod.DEFAULT_MODEL,
                                base_url=os.environ.get("OPENAI_BASE_URL") or None)
            client, model = a.client, a.model
        w = Watcher()
        w.poll()
        print("盯着：", {p: (i.rect, i.title) for p, i in w.base.items()}, "，Ctrl+C 退出")
        while True:
            for c in w.poll():
                print(time.strftime("%H:%M:%S"), c.app, f"「{c.before.title}」→「{c.after.title}」",
                      judge(c, client, model) if client else "")
            time.sleep(2)
    else:
        for p in APPS:
            print(p, locate(p))
