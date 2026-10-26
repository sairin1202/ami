"""把 agent 每一步的状态上报给 AMI 中控（amients.com）。

接口（amients.com「接入文档」）：所有请求带 Authorization: Bearer <key>，body 是 JSON。
- PUT /api/v1/status：整份替换当前状态，约定 state（running / idle / warn / error）和 summary（一句话），其他字段随意
- POST /api/v1/events：[{kind, text, level, data, ts}]，level 为 info / warn / error，一次最多 200 条，每个 agent 留最近 2000 条
- POST /api/v1/heartbeat：空闲时保持在线（中控在 online_window 秒内没收到上报就显示离线）
- 任务队列：POST /api/v1/tasks/pull {wait, limit} 领任务（长轮询，没有就挂 wait 秒返回空），每个任务带 token；
  POST /api/v1/tasks/{id}/complete {token, result} / fail {token, error, retry} / progress {token, progress}（把时限往后延）。
  回报拿到 409 说明这次领取已失效（超时被收回或被取消），丢掉结果即可。领的时候可以带 kinds 只领这些类型
- PUT /api/v1/capabilities {capabilities: [{kind, title, description, input, output, example}]}：注册能处理哪些任务（整份替换）
- PUT /api/v1/checks {checks: [名字…]}：声明自检项，中控每天派 kind 为 ami.check 的任务（只派给自己），
  回报 {checks: [{name, ok, kind, message, hint}]}，kind 是 auth / quota / network / config / other

这里：
- 每一步都是一个事件：前台指令和后台工作流的每次运行开始 / 每个动作 / 状态 / 出错 / 结束（含统计），以及工作流的事件
  （巡检、改目标）。不含截图；文字超过 1000 字截断；等待中的进度刷新不上报。
- 运行开始时把当前状态报成 running（在做什么），结束时报成 idle（结果）或 error。
- 事件先追加进本地待发队列 ~/.computer-use/amients_outbox.jsonl（最多保留最近 5000 条），后台线程分批发，失败退避重试，
  成功才往后走，不会丢；状态只发最新的一份；空闲时每 60 秒一次心跳。
- 领任务（TaskWorker）：能接活的时候去中控领任务，领到一个做一个（交给 workflows 的调度器，和消息巡检一样是一个工作流，
  排队轮流跑），做的过程中定时回报进度，做完回报结果、出错回报失败；中途领取失效就停下、丢掉结果。AMIENTS_TASKS=off 不领任务。
- 配置：AMIENTS_API_KEY（或 AMI_KEY）有值才发；AMIENTS_URL 默认 https://amients.com。AMIENTS=off 时完全不记录、不发送。
"""

import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime

import paths

DEFAULT_URL = "https://amients.com"
MAX_TEXT = 1000
MAX_OUTBOX = 5000
BATCH = 200                 # 接口一次最多 200 条
HEARTBEAT = 60.0            # 空闲时多久发一次心跳
SKIP_KINDS = {"progress", "thinking"}  # 等待中的进度刷新、模型的思考摘要不上报

_lock = threading.Lock()
_sender = None


def enabled() -> bool:
    return os.environ.get("AMIENTS", "on").strip().lower() not in ("off", "0", "false", "no")


def api_key() -> str:
    return (os.environ.get("AMIENTS_API_KEY") or os.environ.get("AMI_KEY") or "").strip()


def endpoint() -> str:
    """中控地址；没有 key 时返回空（不发）。"""
    if not api_key():
        return ""
    return ((os.environ.get("AMIENTS_URL") or "").strip() or DEFAULT_URL).rstrip("/")


def _status_path() -> str:
    return os.path.join(paths.state_dir(), "amients_status.json")


def _outbox() -> str:
    return os.path.join(paths.state_dir(), "amients_outbox.jsonl")


def _cursor_path() -> str:
    return os.path.join(paths.state_dir(), "amients_cursor.json")


def emit(kind: str, text: str = "", run_id: str = "", origin: str = "user", data: dict | None = None) -> dict | None:
    """记一个事件到待发队列。不会抛异常（上报出问题不能影响 agent）。"""
    if not enabled() or kind in SKIP_KINDS:
        return None
    ev = {"id": uuid.uuid4().hex, "ts": round(time.time(), 3), "source": "ami", "host": socket.gethostname(),
          "run_id": run_id, "origin": origin, "kind": kind, "level": level_of(kind, text), "text": (text or "")[:MAX_TEXT]}
    if data:
        ev["data"] = data
    try:
        with _lock, open(_outbox(), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return None
    return ev


WARN_KINDS = {"workflow.task_lost", "workflow.check_alert"}
ERROR_KINDS = {"error", "workflow.error", "workflow.goal_error", "workflow.task_failed"}


def level_of(kind: str, text: str = "") -> str:
    if kind in ERROR_KINDS:
        return "error"
    if kind in WARN_KINDS:
        return "warn"
    return "info"


def set_status(state: str, summary: str, **fields) -> None:
    """记下最新的当前状态（state: running / idle / warn / error），由发送线程 PUT 到中控。只保留最新一份。
    同时记进 Ami 自己的状态文件（agentstate），AMIENTS=off 时也记。"""
    st = {"state": state, "summary": (summary or "")[:200], **{k: v for k, v in fields.items() if v is not None},
          "updated": round(time.time(), 3)}
    import agentstate
    agentstate.set_running(st)
    if not enabled():
        return
    try:
        with _lock:
            tmp = _status_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"status": st, "sent": False}, f, ensure_ascii=False, default=str)
            os.replace(tmp, _status_path())
    except OSError:
        pass


def _read_status() -> dict:
    try:
        with open(_status_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 发送
def _read_cursor() -> dict:
    try:
        with open(_cursor_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_cursor(c: dict) -> None:
    tmp = _cursor_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, _cursor_path())


def pending(limit: int = BATCH) -> list[dict]:
    """还没发出去的事件（按顺序），最多 limit 条。"""
    last = _read_cursor().get("last_id")
    try:
        with _lock, open(_outbox(), encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    if last:
        ids = [e.get("id") for e in events]
        events = events[ids.index(last) + 1:] if last in ids else events
    return events[:limit]


def stats() -> dict:
    c = _read_cursor()
    w = _worker
    return {"pending": len(pending(MAX_OUTBOX)), "endpoint": endpoint(), "last_ok": c.get("last_ok"),
            "last_error": c.get("last_error"), "tasks": w is not None,
            "task": task_label(w.current) if w and w.current else None, "task_error": w.last_error if w else None}


class LeaseLost(RuntimeError):
    """回报任务时 409：这次领取已失效（超时被收回重新派发，或被取消），丢掉结果即可。"""


def _request(method: str, path: str, body=None, timeout: float = 15):
    """发请求，返回响应的 JSON（没有内容时 None）。失败抛异常，409 抛 LeaseLost。"""
    import requests
    r = requests.request(method, endpoint() + path, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    if r.status_code == 409:
        raise LeaseLost(f"{method} {path} → HTTP 409: {r.text[:200]}")
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {path} → HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else None


def to_api(ev: dict) -> dict:
    """本地事件 → 中控的事件格式 {kind, text, level, data, ts}。运行 id、发起方、主机放进 data。"""
    data = dict(ev.get("data") or {})
    data.update({k: ev[k] for k in ("run_id", "origin", "host") if ev.get(k)})
    ts = ev.get("ts")
    if not isinstance(ts, (int, float)):  # 旧格式（ISO 字符串）
        try:
            ts = datetime.fromisoformat(str(ts)).timestamp()
        except ValueError:
            ts = time.time()
    return {"kind": ev["kind"], "text": ev.get("text", ""), "level": ev.get("level") or level_of(ev["kind"]),
            "data": data, "ts": ts}


def send_batch(events: list[dict]) -> None:
    """POST /api/v1/events（数组，最多 200 条）。失败抛异常。"""
    _request("POST", "/api/v1/events", [to_api(e) for e in events])


def flush_status() -> bool:
    """有没发出去的新状态就 PUT。返回是否发了。"""
    cur = _read_status()
    if not cur or cur.get("sent") or not endpoint():
        return False
    _request("PUT", "/api/v1/status", cur["status"])
    with _lock:
        latest = _read_status()
        if latest.get("status") == cur["status"]:  # 发的期间没有更新的状态才标记已发
            latest["sent"] = True
            with open(_status_path(), "w", encoding="utf-8") as f:
                json.dump(latest, f, ensure_ascii=False, default=str)
    return True


def heartbeat() -> None:
    _request("POST", "/api/v1/heartbeat")


def flush_once() -> int:
    """发一批事件（和最新状态）。返回发出去的事件条数；没配 key 或没有新东西时返回 0。"""
    if not enabled() or not endpoint():
        return 0
    c = _read_cursor()
    try:
        flush_status()
        batch = pending()
        if batch:
            send_batch(batch)
    except Exception as e:  # noqa: BLE001
        c["last_error"] = f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}"[:300]
        _write_cursor(c)
        raise
    c["last_sent_at"] = time.time()
    if batch:
        c.update(last_id=batch[-1]["id"], last_ok=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None)
    _write_cursor(c)
    if batch:
        _trim()
    return len(batch)


def _trim() -> None:
    """待发队列只留最近 MAX_OUTBOX 条（已经发出去的旧事件可以丢）。"""
    try:
        with _lock:
            with open(_outbox(), encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= MAX_OUTBOX:
                return
            with open(_outbox(), "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_OUTBOX:])
    except OSError:
        pass


class Sender:
    """后台线程：每 interval 秒发一批事件和最新状态，空闲时每分钟心跳；失败后按 10s、20s… 退避（最长 5 分钟）。"""

    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="amients", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        wait = 0.5
        while not self._stop.wait(wait):
            try:
                n = flush_once()
                if time.time() - (_read_cursor().get("last_sent_at") or 0) >= HEARTBEAT:
                    heartbeat()  # 空闲时保持在线
                    c = _read_cursor()
                    c["last_sent_at"] = time.time()
                    _write_cursor(c)
                wait = 0.5 if n == BATCH else self.interval  # 积压时连续发
            except Exception:  # noqa: BLE001
                wait = min(300.0, max(self.interval, wait) * 2)


def start_sender() -> Sender | None:
    """有 key 才启动发送线程（没有时事件只留在本地待发队列）。"""
    global _sender
    if _sender is None and enabled() and endpoint():
        _sender = Sender()
        _sender.start()
    return _sender


def stop_sender() -> None:
    global _sender
    if _sender is not None:
        _sender.stop()
        _sender = None


# ---------------------------------------------------------------------------
# 领任务
TASK_WAIT = 25                                         # 没有任务时长轮询挂多少秒
CHECK_KIND = "ami.check"                               # 中控每天派的自检任务
TEXT_KEYS = ("text", "task", "instruction", "prompt")  # 任务内容里这些字段就是指令本身

_worker = None


def tasks_enabled() -> bool:
    return enabled() and bool(endpoint()) and \
        os.environ.get("AMIENTS_TASKS", "on").strip().lower() not in ("off", "0", "false", "no")


class TaskFailed(Exception):
    """做任务的一方主动报失败。retry：要不要让中控重新派发。"""

    def __init__(self, reason: str, retry: bool = False):
        super().__init__(reason)
        self.retry = retry


def pull_tasks(wait: int = TASK_WAIT, limit: int = 1, kinds: list[str] | None = None) -> list[dict]:
    """领任务：有就立刻返回，没有就最多挂 wait 秒后返回空列表。kinds：只领这些类型。"""
    body = {"wait": wait, "limit": limit, **({"kinds": kinds} if kinds else {})}
    r = _request("POST", "/api/v1/tasks/pull", body, timeout=wait + 15)
    items = r.get("tasks") if isinstance(r, dict) else r
    return [t for t in items or [] if isinstance(t, dict) and t.get("id") is not None and t.get("token")]


def register_capabilities(caps: list[dict]) -> None:
    """注册这个 agent 能处理哪些任务（整份替换），中控的「能力」页和派任务时会用到。"""
    _request("PUT", "/api/v1/capabilities", {"capabilities": caps})


def declare_checks(names: list[str]) -> None:
    """声明自检项：中控据此每天派 ami.check 任务（第一次声明时马上派一次）。"""
    _request("PUT", "/api/v1/checks", {"checks": names})


def report_task(task: dict, action: str, **body) -> None:
    """回报 complete / fail / progress，带上领取时的 token。领取已失效时抛 LeaseLost。"""
    _request("POST", f"/api/v1/tasks/{task['id']}/{action}", {"token": task["token"], **body})


def task_label(task: dict) -> str:
    return f"#{task.get('id')}（{task.get('kind') or 'task'}）"


def task_text(task: dict) -> str:
    """任务要做什么：内容是纯文本，或者 {"text": …} 这类字段，就是那段话（其余字段附在后面）；否则是整份 JSON。"""
    p = task.get("payload")
    if isinstance(p, str):
        return p.strip()
    if not isinstance(p, dict):
        return "" if p is None else json.dumps(p, ensure_ascii=False)
    key = next((k for k in TEXT_KEYS if isinstance(p.get(k), str) and p[k].strip()), None)
    if key is None:
        return json.dumps(p, ensure_ascii=False) if p else ""
    rest = {k: v for k, v in p.items() if k != key}
    return p[key].strip() + (f"\n附带的内容：{json.dumps(rest, ensure_ascii=False)}" if rest else "")


class Lease:
    """领到的一个任务。做的过程中后台定时回报进度，把时限往后延；回报拿到 409 说明领取已失效：lost 置位，调 on_lost（停下 agent）。"""

    def __init__(self, task: dict):
        self.task = task
        self.lost = threading.Event()
        self.on_lost = None
        self.text = "开始做"
        self.keep = max(3.0, min(60.0, float(task.get("timeout") or 600) / 3))  # 至少每 1/3 个时限回报一次
        self._sent = None
        self._last = time.time()
        self._done = threading.Event()

    def note(self, text: str) -> None:
        """记下现在在做什么，下次回报进度时带上。"""
        self.text = (text or "")[:200]

    def beat(self) -> None:
        """有新进度、或者离上次回报快 1/3 个时限了，就回报一次。"""
        if self.text == self._sent and time.time() - self._last < self.keep:
            return
        try:
            report_task(self.task, "progress", progress={"text": self.text})
            self._sent, self._last = self.text, time.time()
        except LeaseLost:
            self.lost.set()
            if self.on_lost:
                self.on_lost()
        except Exception:  # noqa: BLE001  网络抖一下，下一轮再报
            pass

    def start(self):
        threading.Thread(target=self._loop, name="amients-lease", daemon=True).start()

    def close(self):
        self._done.set()

    def _loop(self):
        while not self._done.wait(min(10.0, self.keep)) and not self.lost.is_set():
            self.beat()


class TaskWorker:
    """后台线程：ready() 说能接活时去中控领任务（长轮询），领到一个做一个。handle(task, lease) 返回结果文字
    （或 {"text": …, 其它字段} 的字典，原样放进结果）；
    抛 TaskFailed 报失败（retry 决定要不要重新派发），抛别的异常报失败并让中控重新派发。
    log(kind, text, data) 记动态：task（领到）/ task_done / task_failed / task_lost。
    setup()：开始领之前先做一次（注册能力、声明自检项），失败了退避重试；kinds：只领这些类型（None 什么都领）。"""

    def __init__(self, handle, ready, log, wait: int = TASK_WAIT, setup=None, kinds: list[str] | None = None):
        self.handle, self.ready, self.log, self.wait = handle, ready, log, wait
        self.setup, self.kinds = setup, kinds
        self.current: dict | None = None   # 正在做的任务
        self.last_error = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="amients-tasks", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        pause = 0.0
        while not self._stop.wait(pause):
            try:
                if self.setup is not None:
                    self.setup()
                    self.setup = None
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                if not (self.last_error or "").endswith(err):  # 同样的错只记一次
                    self.log("error", f"向中控注册能力 / 自检项失败，稍后重试：{err}", {})
                self.last_error = f"{time.strftime('%H:%M:%S')} {err}"
                pause = min(300.0, max(5.0, pause * 2))
                continue
            if not self.ready():
                pause = 2.0
                continue
            try:
                tasks = pull_tasks(self.wait, kinds=self.kinds)
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}"[:300]
                pause = min(300.0, max(5.0, pause * 2))
                continue
            pause = 1.0  # 长轮询返回了就接着领（隔一秒，免得服务端不挂起时打得太勤）
            for t in tasks:
                self.do(t)

    def do(self, task: dict) -> str:
        """做一个领到的任务并回报。返回 done / failed / lost / unreported。"""
        label = task_label(task)
        data = {"task": task.get("id"), "kind": task.get("kind")}
        self.log("task", f"领到任务 {label}：{task_text(task)}", data)
        lease = Lease(task)
        self.current = task
        started = time.time()
        lease.start()
        try:
            try:
                result, error, retry = self.handle(task, lease), None, False
            except TaskFailed as e:
                result, error, retry = None, str(e) or "失败", e.retry
            except Exception as e:  # noqa: BLE001
                result, error, retry = None, f"{type(e).__name__}: {e}", True
            seconds = round(time.time() - started, 1)
            outcome = "lost" if lease.lost.is_set() else self._report(task, result, error, retry, seconds)
        finally:
            lease.close()
            self.current = None
        data = {**data, "seconds": seconds}
        if outcome == "done":
            text = result.get("text") if isinstance(result, dict) else result
            self.log("task_done", f"任务 {label} 做完了：{text or '完成'}", data)
        elif outcome == "failed":
            self.log("task_failed", f"任务 {label} 失败" + ("，让中控重新派发" if retry else "") + f"：{error}", data)
        elif outcome == "lost":
            self.log("task_lost", f"任务 {label} 的领取已失效（超时被收回或被取消），结果丢掉了", data)
        else:
            self.log("task_failed", f"任务 {label} 的结果没报上去（等中控超时收回）：{self.last_error}", data)
        return outcome

    def _report(self, task: dict, result, error, retry: bool, seconds: float) -> str:
        for i in range(4):
            try:
                if error is None:
                    body = dict(result) if isinstance(result, dict) else {"text": result or "完成"}
                    report_task(task, "complete", result={**body, "seconds": seconds, "host": socket.gethostname()})
                    return "done"
                report_task(task, "fail", error=error[:MAX_TEXT], retry=retry)
                return "failed"
            except LeaseLost:
                return "lost"
            except Exception as e:  # noqa: BLE001  报不出去：等一下再试
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}"[:300]
                if i < 3:
                    time.sleep(2 ** (i + 1))
        return "unreported"


def start_worker(handle, ready, log, setup=None, kinds: list[str] | None = None) -> TaskWorker | None:
    """配了 key、没关 AMIENTS_TASKS 才启动领任务的线程。"""
    global _worker
    if _worker is None and tasks_enabled():
        _worker = TaskWorker(handle, ready, log, setup=setup, kinds=kinds)
        _worker.start()
    return _worker


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
