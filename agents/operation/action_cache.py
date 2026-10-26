"""动作缓存：同一条指令成功过一次后，记住 Jev 走过的步骤，下次直接回放，不调用任何模型。

参考 midscene 的计划缓存：存的是「操作 + 元素特征（角色、名字、所在分组）」，不是坐标；
回放每一步前都重新读控件表，确认元素还在，找不到就停止回放并作废这条缓存，交回正常流程。
只在「Jev 走完、GPT 截图核实时没有再做任何操作」时写入，保证存下来的是真正完成任务的步骤。
缓存文件只在本机：~/.computer-use/action_cache.json（可用 COMPUTER_USE_HOME 改位置）。
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import axui
import jev
from paths import state_dir

MAX_ENTRIES = 200
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(state_dir(), "action_cache.json")


def task_key(task: str) -> str:
    return re.sub(r"\s+", " ", task.strip()).lower()


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("version") == 1 else {"version": 1, "entries": {}}
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}


def _save(data: dict) -> None:
    entries = data["entries"]
    if len(entries) > MAX_ENTRIES:  # 超出上限时丢掉最久没用的
        for k in sorted(entries, key=lambda k: entries[k].get("used", 0))[:len(entries) - MAX_ENTRIES]:
            del entries[k]
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def get(task: str) -> list[dict] | None:
    with _lock:
        e = _load()["entries"].get(task_key(task))
    return e["steps"] if e else None


def put(task: str, steps: list[dict]) -> None:
    if not steps:
        return
    with _lock:
        data = _load()
        data["entries"][task_key(task)] = {"steps": steps, "created": time.time(), "used": time.time()}
        _save(data)


def invalidate(task: str) -> None:
    with _lock:
        data = _load()
        if data["entries"].pop(task_key(task), None) is not None:
            _save(data)


def _touch(task: str) -> None:
    with _lock:
        data = _load()
        e = data["entries"].get(task_key(task))
        if e:
            e["used"] = time.time()
            _save(data)


def _find(snap: axui.Snapshot, key: list) -> axui.Element | None:
    """按特征找回元素：角色 + 名字 + 分组完全一致；找不到时退一步只比角色 + 名字，但必须唯一。"""
    role, label, context = key
    exact = [e for e in snap.elements if (e.role, e.label, e.context) == (role, label, context)]
    if exact:
        return exact[0]
    loose = [e for e in snap.elements if (e.role, e.label) == (role, label)]
    return loose[0] if len(loose) == 1 else None


@dataclass
class ReplayResult:
    ok: bool
    reason: str = ""
    actions: list[str] = field(default_factory=list)


def replay(task: str, steps: list[dict], log, stop_event) -> ReplayResult:
    """按缓存的步骤回放。任何一步对不上就停下（已执行的步骤不撤销），由调用方作废缓存、交回正常流程。"""
    done: list[str] = []
    for i, st in enumerate(steps, 1):
        if stop_event.is_set():
            return ReplayResult(False, "已停止", done)
        op, el = st["op"], None
        if st.get("element"):
            snap = None
            for _ in range(3):  # 界面可能还在刷新，最多看三次
                snap = axui.read()
                if snap is not None:
                    axui.augment_with_ocr(snap)
                if snap is not None and snap.bundle_id == st.get("app"):
                    el = _find(snap, st["element"])
                    if el is not None:
                        break
                time.sleep(0.4)
            if el is None:
                return ReplayResult(False, f"第 {i} 步找不到「{st['element'][1]}」", done)
        desc = f"{op} {el.describe()}" if el else f"{op} {os.path.basename(st.get('app_path') or '')}".strip()
        log("action", f"[缓存回放 {i}/{len(steps)}] {desc}")
        try:
            jev.execute(op, el, st.get("app_path"), st.get("text"))
        except Exception as e:  # noqa: BLE001
            return ReplayResult(False, f"第 {i} 步执行失败: {e}", done)
        done.append(desc)
        time.sleep(0.35)
    _touch(task)
    return ReplayResult(True, "", done)
