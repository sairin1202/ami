"""Ami 自己的状态：各平台账号能不能用、在做什么、工作流、能用的工具、权限……存在 ~/.computer-use/agent_state.json，
中控派 agent.state 任务时整份交回去（workflows.Scheduler 里有一个专门的领取线程，不占屏幕，忙的时候也马上回）。

- accounts：各平台账号（命令行工具、浏览器里的网站），名字和自检项一样（「rednote 命令行登录」「zhihu.com 网页登录」）。
  自检（中控派的 ami.check、/check）每查一项就更新；平时用工具时报「没登录」也记下来。
  只从工具调用里记坏消息：调用成功不代表登录着（有的接口不用登录），好消息只认自检。
- running：当前的运行状态（running / idle / warn / error、一句话、更新时间），每次上报状态时跟着更新。
- 其余（进程、工作流、领任务、能用的工具、权限、微信是否暂停）在 snapshot() 时现算，一起写进文件；
  调度线程每分钟也刷新一次，所以文件随时能读。写的时候先写临时文件再替换，不会读到写了一半的。
"""

import json
import os
import socket
import threading
import time

import paths

STARTED = time.time()
_lock = threading.RLock()


def path() -> str:
    return os.path.join(paths.state_dir(), "agent_state.json")


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _update(fn) -> dict:
    with _lock:
        d = load()
        fn(d)
        d["updated_at"] = round(time.time(), 3)
        tmp = path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path())
        return d


def _account(d: dict, name: str, ok: bool, kind: str, detail: str, hint: str, source: str) -> None:
    now = round(time.time(), 3)
    accounts = d.setdefault("accounts", {})
    prev = accounts.get(name) or {}
    e = {"ok": ok, "detail": (detail or "")[:300], "source": source, "checked_at": now,
         "changed_at": now if prev.get("ok") is not ok else prev.get("changed_at", now)}  # 从好变坏 / 从坏变好的时间
    if kind:
        e["kind"] = kind
    if hint:
        e["hint"] = hint
    accounts[name] = e


def set_account(name: str, ok: bool, *, kind: str = "", detail: str = "", hint: str = "", source: str = "tool") -> None:
    try:
        _update(lambda d: _account(d, name, ok, kind, detail, hint, source))
    except OSError:
        pass


def record_checks(checks: list[dict], source: str = "check") -> None:
    """自检结果（中控的格式 [{name, ok, kind, message, hint}]）记进 accounts。"""
    def f(d):
        for c in checks:
            _account(d, c["name"], bool(c.get("ok")), c.get("kind") or "", c.get("message") or ("正常" if c.get("ok") else ""),
                     c.get("hint") or "", source)
    try:
        _update(f)
    except OSError:
        pass


def set_running(status: dict) -> None:
    try:
        _update(lambda d: d.__setitem__("running", status))
    except OSError:
        pass


def permissions() -> dict:
    """macOS 权限：辅助功能（控制鼠标键盘）、屏幕录制（截图）。查不了的不列。"""
    out = {}
    try:
        import ApplicationServices as AS
        out["辅助功能"] = bool(AS.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        pass
    try:
        import Quartz
        out["屏幕录制"] = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        pass
    return out


def _live(scheduler=None) -> dict:
    import amients
    import tools
    import workflows
    snap = workflows.status_snapshot(scheduler)
    st = amients.stats()
    out = {
        "agent": {"name": "Ami", "host": socket.gethostname(), "pid": os.getpid(), "started_at": round(STARTED, 3),
                  "uptime_s": round(time.time() - STARTED)},
        "workflows": [{k: w.get(k) for k in ("id", "name", "enabled", "running", "last_run", "next_run")}
                      for w in snap["workflows"]],
        "user_busy": snap["user_busy"],
        "idle_seconds": snap["idle_seconds"],
        "amients": {k: st.get(k) for k in ("endpoint", "pending", "last_ok", "last_error", "tasks", "task", "task_error")},
        "tools": {"active": [p.__name__ for p in tools.PROVIDERS if p in tools.active()],
                  "inactive": [p.__name__ for p in tools.PROVIDERS if p not in tools.active()]},
        "permissions": permissions(),
    }
    try:
        import wechat
        p = wechat.paused()
        out["wechat_paused"] = wechat.paused_detail(p) if p else None
    except Exception:  # noqa: BLE001
        pass
    return out


def summary(d: dict) -> str:
    acc = d.get("accounts") or {}
    bad = [n for n, a in acc.items() if not a.get("ok")]
    run = (d.get("running") or {}).get("summary") or "空闲"
    accounts = (f"账号 {len(acc) - len(bad)} 个正常" + (f"，{'、'.join(bad)} 有问题" if bad else "")) if acc \
        else "账号还没查过（派 ami.check，或者 agent.state 带 fresh）"
    return (f"{run}；{accounts}"
            + ("；缺权限：" + "、".join(k for k, v in (d.get("permissions") or {}).items() if not v)
               if not all((d.get("permissions") or {}).values()) else ""))


def snapshot(scheduler=None) -> dict:
    """现算一遍（进程、工作流、工具、权限……），和记下的账号、运行状态合在一起写进文件，返回整份。"""
    live = _live(scheduler)
    return _update(lambda d: d.update(live))
