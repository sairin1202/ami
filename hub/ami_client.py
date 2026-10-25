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

能力：把自己能处理的任务类型注册到中控，控制台的「能力」页和其它 agent 就知道该找谁、payload 怎么写。

    @ami.capability("translate", "把中文翻译成英文", input={"text": "要翻译的文字"}, output={"text": "译文"},
                    example={"text": "你好"})
    def translate(task):
        return {"text": do_translate(task["payload"]["text"])}

    ami.work()                                               # 启动时注册能力，只领注册过的 kind，按 kind 分给对应的函数
    ami.find("translate")                                    # 另一边：查谁注册了 translate、用自己的 key 发过去能不能被领到

每日自检：注册几项检查，中控每天定时派自检任务过来，work() / serve_checks() 会自动跑一遍并回报；
没通过的会在控制台告警，并按用户设置的通知地址推送。检查要真的调用一次，而不只是看配置在不在。

    @ami.check("Lark 登录")
    def lark_login():
        if not lark.get_chats(limit=1):                      # 用当前登录态真的拉一次
            raise CheckFailed("Lark 登录已过期", kind="auth", hint="在家里的 Mac 上重新扫码登录")

    @ami.check("OpenAI 余额")
    def openai_quota():
        client.models.list()                                 # 抛出的异常会按内容自动归类（登录 / 余额 / 网络）

    ami.serve_checks()                                       # 不用 work() 的 agent：后台线程只领自检任务

中控连不上时默认只返回 False，不会让 agent 自己挂掉；要抛异常就传 raise_errors=True。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

CHECK_KIND = "ami.check"


class CheckFailed(Exception):
    """自检项没通过时抛这个。kind：auth（登录 / 授权失效）、quota（余额 / 额度不足）、network、config、other；
    hint：给人看的处理办法，会出现在控制台和通知里。"""

    def __init__(self, message, kind="other", hint=""):
        super().__init__(message)
        self.kind, self.hint = kind, hint


def guess_kind(e):
    """普通异常按状态码和报错内容猜个大类，猜不出就是 other。"""
    code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "code", None)
    text = f"{e!r} {e}".lower()
    if code == 402 or any(w in text for w in ("insufficient_quota", "quota", "balance", "credit", "billing", "余额", "欠费", "额度")):
        return "quota"
    if code in (401, 403) or any(w in text for w in ("unauthorized", "forbidden", "invalid api key", "invalid_api_key", "authentication",
                                                     "expired", "login", "登录", "未授权", "过期")):
        return "auth"
    if (isinstance(e, (TimeoutError, ConnectionError)) or isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError)
            or any(w in text for w in ("timed out", "timeout", "connection", "network", "超时"))):
        return "network"
    return "other"


class Ami:
    def __init__(self, base_url, api_key, timeout=5, raise_errors=False):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.raise_errors = raise_errors
        self.checks = []   # [(名字, 函数)]
        self.caps = {}     # kind -> (注册给中控的说明, 处理函数)

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

    # ---- 每日自检 ----
    def check(self, name, fn=None):
        """注册一项自检，可以当装饰器用。函数正常返回算通过（返回字符串会作为说明），抛异常算没通过。"""
        def add(f):
            self.checks.append((name, f))
            return f
        return add(fn) if fn else add

    def run_checks(self):
        """把注册的检查都跑一遍，返回回报给中控的列表。"""
        out = []
        for name, f in self.checks:
            t0 = time.monotonic()
            try:
                r = f()
            except Exception as e:
                out.append({"name": name, "ok": False, "kind": e.kind if isinstance(e, CheckFailed) else guess_kind(e),
                            "message": str(e) or repr(e), "hint": getattr(e, "hint", "")})
            else:
                out.append({"name": name, "ok": True, "message": r if isinstance(r, str) else ""})
            out[-1]["ms"] = round((time.monotonic() - t0) * 1000)
        return out

    def declare_checks(self):
        """告诉中控有哪些检查项。第一次声明会自动开启每日自检（默认 09:00）并马上跑一次。"""
        return self._call("PUT", "/api/v1/checks", {"checks": [n for n, _ in self.checks]})

    def _do_check(self, t):
        self.complete(t, {"checks": self.run_checks()})

    def serve_checks(self):
        """开一个后台线程，只领自检任务。agent 不用 work() 领其它任务时用这个；用 work() 的不用调。"""
        def loop():
            declared = False
            while True:
                declared = declared or bool(self.declare_checks())
                tasks = self.pull(kinds=[CHECK_KIND])
                for t in tasks:
                    self._do_check(t)
                if not tasks:
                    time.sleep(1)
        th = threading.Thread(target=loop, name="ami-checks", daemon=True)
        th.start()
        return th

    # ---- 能力 ----
    def capability(self, kind, description="", title="", input=None, output=None, example=None, fn=None):
        """注册一项能力，可以当装饰器用：fn(task) 处理这个 kind 的任务，返回值作为结果。
        input / output 说明 payload 和结果长什么样（字段 → 说明，或 JSON Schema），example 是一份示例 payload，
        控制台派任务时会预填。work() 启动时把注册的能力报给中控。"""
        def add(f):
            spec = {"kind": kind, "title": title, "description": description, "input": input, "output": output, "example": example}
            self.caps[kind] = ({k: v for k, v in spec.items() if v not in (None, "")}, f)
            return f
        return add(fn) if fn else add

    def declare_capabilities(self):
        """把注册的能力报给中控（整份替换，没注册的就撤掉）。work() 会自动调；不用 work() 的自己在启动时调一次。"""
        return self._call("PUT", "/api/v1/capabilities", {"capabilities": [spec for spec, _ in self.caps.values()]})

    def find(self, kind=None):
        """查同一个用户名下的 agent 都注册了哪些能力；给了 kind 就只返回这一项（没人注册返回 None）。
        每项带 providers（谁注册的、在不在线）和 reachable（用这个 key push 这个 kind，注册了它的 agent 能不能领到）。"""
        r = self._call("GET", "/api/v1/capabilities")
        caps = r["capabilities"] if r else []
        return next((c for c in caps if c["kind"] == kind), None) if kind else caps

    def work(self, handler=None, kinds=None, retry=False):
        """一直领任务、做、回报，Ctrl+C 退出。用 @ami.capability 注册过的 kind 交给对应的函数，其它的交给 handler(task)；
        返回值作为结果，抛异常就回报失败。不给 handler 时只领注册过的 kind。
        自检任务（ami.check）不交给 handler，自动跑注册的检查并回报。"""
        if handler is None:
            kinds = list(self.caps) if kinds is None else kinds
            if not kinds or set(kinds) - set(self.caps):
                raise ValueError("不给 handler 时，要领的 kind 都得先用 @ami.capability 注册")
        declared, declared_caps = not self.checks, not self.caps
        while True:
            declared = declared or bool(self.declare_checks())
            declared_caps = declared_caps or bool(self.declare_capabilities())
            tasks = self.pull(kinds=kinds and [*kinds, CHECK_KIND])
            if not tasks:
                time.sleep(1)   # 没任务时长轮询已经等过了；这里只防止中控连不上时空转
                continue
            for t in tasks:
                if t["kind"] == CHECK_KIND:
                    self._do_check(t)
                    continue
                try:
                    result = (self.caps[t["kind"]][1] if t["kind"] in self.caps else handler)(t)
                except Exception as e:
                    print(f"任务 {t['id']} 失败：{e!r}", file=sys.stderr)
                    self.fail(t, repr(e), retry=retry)
                else:
                    self.complete(t, result)
