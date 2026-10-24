"""AMI 中控的上报客户端，只用标准库，拷进 agent 项目里就能用。

    from ami_client import Ami
    ami = Ami("http://127.0.0.1:8787", "ami_xxx")
    ami.status(state="running", summary="正在巡检 Lark 消息", queue=3)
    ami.event("sent", "已回复张三", chat="项目群")
    ami.event("error", "Lark 登录过期", level="error")

任务队列：任务进提交它的 key 所在分组的队列（不分组的 key 进公共池），同组的 key 来领，一条任务只给一个 agent。

    task = ami.push("translate", {"text": "hello"})                  # 发任务
    ami.wait(task["id"], timeout=60)                                 # 等结果（可选）

    ami.work(lambda t: {"text": translate(t["payload"]["text"])})    # 领任务的一方：一直领、做、回报

中控连不上时默认只返回 False，不会让 agent 自己挂掉；要抛异常就传 raise_errors=True。
"""
import json
import sys
import time
import urllib.error
import urllib.request


class Ami:
    def __init__(self, base_url, api_key, timeout=5, raise_errors=False):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.raise_errors = raise_errors

    def _call(self, method, path, body=None, timeout=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                return json.loads(res.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError):
            if self.raise_errors:
                raise
            return False

    def status(self, **fields):
        """整份替换当前状态。约定字段：state（running/idle/warn/error…）、summary（一句话）；其余随意。"""
        return self._call("PUT", "/api/v1/status", fields)

    def patch(self, **fields):
        """只更新给出的字段，其余保留。"""
        return self._call("PATCH", "/api/v1/status", fields)

    def event(self, kind, text="", level="info", ts=None, **data):
        e = {"kind": kind, "text": text, "level": level, "data": data or None}
        if ts is not None:
            e["ts"] = ts
        return self._call("POST", "/api/v1/events", e)

    def events(self, items):
        """批量发：items 是 [{"kind", "text", "level", "data", "ts"}, ...]，一次最多 200 条。"""
        return self._call("POST", "/api/v1/events", {"events": items})

    def heartbeat(self):
        return self._call("POST", "/api/v1/heartbeat")

    # ---- 任务队列 ----
    def push(self, kind="task", payload=None, **opts):
        """发任务到这个 key 所在分组的队列。opts：priority、max_attempts、timeout（每次执行的时限秒数）、
        expires_in（排队多久没人领就过期）、dedupe_key（同一个队列里同一个值只建一次）。返回任务。"""
        r = self._call("POST", "/api/v1/tasks", {"kind": kind, "payload": payload, **opts})
        return r and r["task"]

    def task(self, task_id, wait=0):
        """查任务；wait>0 时最多等这么多秒（上限 30），直到它结束。"""
        r = self._call("GET", f"/api/v1/tasks/{task_id}?wait={wait}", timeout=wait + self.timeout)
        return r and r["task"]

    def wait(self, task_id, timeout=300):
        """一直等到任务结束（完成 / 失败 / 取消 / 过期）或超时，返回最后看到的任务。"""
        end, t = time.monotonic() + timeout, None
        while True:
            left = end - time.monotonic()
            t = self.task(task_id, wait=max(0, min(30, int(left)))) or t
            if t and t["status"] in ("done", "failed", "canceled", "expired") or left <= 0:
                return t
            if not t:
                time.sleep(2)

    def pull(self, wait=25, limit=1, kinds=None):
        """领任务（自己分组的队列 + 公共池）；没有就最多挂 wait 秒（上限 30）。
        返回任务列表，每个任务带 token，回报时要用；连不上时返回空列表。"""
        body = {"wait": wait, "limit": limit, **({"kinds": kinds} if kinds else {})}
        r = self._call("POST", "/api/v1/tasks/pull", body, timeout=wait + self.timeout)
        return r["tasks"] if r else []

    # 下面几个回报方法的 task 都是 pull 拿到的任务（要用里面的 id 和 token）
    def progress(self, task, extend=None, **fields):
        """报进度（合并到任务的 progress 里），同时把执行时限往后延 extend 秒（默认延一个 timeout）。"""
        return self._call("POST", f"/api/v1/tasks/{task['id']}/progress",
                          {"token": task["token"], "progress": fields or None, "extend": extend})

    def complete(self, task, result=None):
        return self._call("POST", f"/api/v1/tasks/{task['id']}/complete", {"token": task["token"], "result": result})

    def fail(self, task, error, retry=False):
        """retry=True 且还有次数时重新排队，否则直接算失败。"""
        return self._call("POST", f"/api/v1/tasks/{task['id']}/fail",
                          {"token": task["token"], "error": str(error), "retry": retry})

    def cancel(self, task_id):
        """取消自己发出的任务。"""
        return self._call("POST", f"/api/v1/tasks/{task_id}/cancel")

    def work(self, handler, kinds=None, retry=False):
        """一直领任务交给 handler(task)，它的返回值作为结果；抛异常就回报失败。Ctrl+C 退出。"""
        while True:
            tasks = self.pull(kinds=kinds)
            if not tasks:
                time.sleep(1)   # 没任务时长轮询已经等过了；这里只防止中控连不上时空转
                continue
            for t in tasks:
                try:
                    result = handler(t)
                except Exception as e:
                    print(f"任务 {t['id']} 失败：{e!r}", file=sys.stderr)
                    self.fail(t, repr(e), retry=retry)
                else:
                    self.complete(t, result)
