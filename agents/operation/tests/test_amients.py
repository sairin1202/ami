"""上报到 AMI 中控：事件先进本地待发队列，有 key 才分批发；失败不丢；状态只发最新的；agent 每一步和工作流事件都会上报。不联网。"""

import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import agent  # noqa: E402
import amients  # noqa: E402
import workflows  # noqa: E402


class AmientsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS": "on", "AMIENTS_URL": "",
                                     "AMIENTS_API_KEY": "", "AMI_KEY": "", "JEV_REVIEW": "off"}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def test_emit_queues_locally_and_skips_noise(self):
        amients.emit("action", "读 Lark「A」最近 20 条", "r1")
        amients.emit("progress", "等模型回复 3 秒…", "r1")
        amients.emit("status", "x" * 5000, "r1", origin="inbox")
        evs = amients.pending(100)
        self.assertEqual([e["kind"] for e in evs], ["action", "status"])
        self.assertEqual(evs[1]["origin"], "inbox")
        self.assertEqual(len(evs[1]["text"]), amients.MAX_TEXT)
        self.assertIsInstance(evs[0]["ts"], float)
        self.assertEqual(amients.flush_once(), 0)  # 没有 key：不发，留在本地
        self.assertEqual(amients.endpoint(), "")

    def test_off(self):
        with mock.patch.dict(os.environ, {"AMIENTS": "off"}):
            self.assertIsNone(amients.emit("action", "x"))
        self.assertEqual(amients.pending(), [])

    def test_flush_in_batches_and_keep_on_failure(self):
        for i in range(amients.BATCH + 3):
            amients.emit("action", f"step {i}", "r1")
        sent = []
        with mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k"}), \
                mock.patch.object(amients, "send_batch", side_effect=lambda b: sent.append([e["text"] for e in b])):
            self.assertEqual(amients.flush_once(), amients.BATCH)
            self.assertEqual(amients.flush_once(), 3)
            self.assertEqual(amients.flush_once(), 0)
        self.assertEqual(sent[1], [f"step {i}" for i in range(amients.BATCH, amients.BATCH + 3)])

        amients.emit("action", "new", "r2")
        with mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k"}), \
                mock.patch.object(amients, "send_batch", side_effect=RuntimeError("HTTP 500")):
            with self.assertRaises(RuntimeError):
                amients.flush_once()
        self.assertEqual([e["text"] for e in amients.pending()], ["new"])  # 失败没丢
        self.assertIn("HTTP 500", amients.stats()["last_error"])

    def test_requests_match_ami_api(self):
        amients.emit("error", "Lark 读取失败", "r1", origin="inbox", data={"chat": "A"})
        amients.emit("workflow.run_done", "回了 A", origin="inbox")
        amients.set_status("running", "正在巡检", origin="inbox")
        calls = []
        ok = SimpleNamespace(status_code=200, text="")
        with mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k"}), \
                mock.patch("requests.request", side_effect=lambda m, url, **kw: calls.append((m, url, kw)) or ok):
            amients.flush_once()
            amients.flush_once()                       # 状态已发过、没有新事件：不再请求
            amients.heartbeat()
        (m1, u1, k1), (m2, u2, k2), (m3, u3, _) = calls
        self.assertEqual((m1, u1), ("PUT", "https://amients.com/api/v1/status"))
        self.assertEqual(k1["json"]["state"], "running")
        self.assertEqual(k1["headers"]["Authorization"], "Bearer k")
        self.assertEqual((m2, u2), ("POST", "https://amients.com/api/v1/events"))
        ev = k2["json"]
        self.assertEqual([e["level"] for e in ev], ["error", "info"])
        self.assertEqual(set(ev[0]), {"kind", "text", "level", "data", "ts"})
        self.assertEqual(ev[0]["data"], {"chat": "A", "run_id": "r1", "origin": "inbox", "host": ev[0]["data"]["host"]})
        self.assertEqual((m3, u3), ("POST", "https://amients.com/api/v1/heartbeat"))
        self.assertEqual(len(calls), 3)

    def test_custom_url_and_ami_key_alias(self):
        with mock.patch.dict(os.environ, {"AMI_KEY": "k2", "AMIENTS_URL": "https://ami.example/"}):
            self.assertEqual(amients.endpoint(), "https://ami.example")
            self.assertEqual(amients.api_key(), "k2")

    def test_trim_keeps_recent(self):
        with mock.patch.object(amients, "MAX_OUTBOX", 5):
            for i in range(9):
                amients.emit("action", str(i))
            amients._trim()
            self.assertEqual([e["text"] for e in amients.pending(100)], ["4", "5", "6", "7", "8"])

    def test_agent_steps_reported(self):
        a = agent.Agent(api_key="sk-test")
        a.origin = "inbox"
        a.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(text="没有新消息"),],
                                    model_dump=lambda exclude_none=True: {"type": "message"})])))
        a.history = [{"role": "user", "content": "之前的对话"}]  # 非新对话，不截图
        a.run("看看谁找我", agent.AgentCallbacks(log=lambda k, t: None))
        evs = amients.pending(100)
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds[0], "run_start")
        self.assertEqual(kinds[-1], "run_end")
        self.assertIn("stats", kinds)
        self.assertEqual({e["run_id"] for e in evs}, {evs[0]["run_id"]})
        self.assertEqual({e["origin"] for e in evs}, {"inbox"})
        self.assertEqual(evs[-1]["text"], "没有新消息")
        self.assertIn("seconds", evs[-1]["data"])
        st = amients._read_status()["status"]
        self.assertEqual((st["state"], st["summary"], st["origin"]), ("idle", "没有新消息", "inbox"))

    def test_workflow_events_reported(self):
        workflows.log_event("trigger", "微信图标「」→「1」：未读 1", {"workflow": "inbox", "unread": 1})
        ev = amients.pending()[-1]
        self.assertEqual((ev["kind"], ev["origin"]), ("workflow.trigger", "inbox"))
        self.assertEqual(ev["data"]["unread"], 1)


def _server(replies: dict, calls: list):
    """假的中控：按 (方法, 路径) 回 (状态码, JSON)，记下每个请求。"""
    def request(method, url, json=None, **kw):
        path = url.split("amients.com", 1)[1]
        calls.append((method, path, json))
        code, body = replies.get((method, path), (200, None))
        text = "" if body is None else __import__("json").dumps(body)
        return SimpleNamespace(status_code=code, text=text, json=lambda: body)
    return request


TASK = {"id": 7, "token": "tok", "kind": "message", "timeout": 600, "from_name": "hong",
        "payload": {"text": "给 A 发「明天十点开会」", "chat": "A"}}


class AmientsTasksTest(unittest.TestCase):
    """去中控领任务：领到就交给 handle 做，做完 / 失败 / 领取失效分别回报；调度里和消息巡检轮流跑。不联网、不碰屏幕。"""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS": "on", "AMIENTS_URL": "",
                                     "AMIENTS_API_KEY": "k", "AMI_KEY": "", "AMIENTS_TASKS": "on"}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)
        self.calls = []
        self.events = []

    def worker(self, handle):
        return amients.TaskWorker(handle, lambda: True, lambda k, t, d: self.events.append((k, t, d)))

    def serve(self, replies: dict):
        mock.patch("requests.request", side_effect=_server(replies, self.calls)).start()

    def test_enabled_needs_key_and_not_off(self):
        self.assertTrue(amients.tasks_enabled())
        with mock.patch.dict(os.environ, {"AMIENTS_TASKS": "off"}):
            self.assertFalse(amients.tasks_enabled())
        with mock.patch.dict(os.environ, {"AMIENTS_API_KEY": ""}):
            self.assertFalse(amients.tasks_enabled())

    def test_task_text(self):
        self.assertEqual(amients.task_text({"payload": " 打开备忘录 "}), "打开备忘录")
        self.assertEqual(amients.task_text({"payload": {"instruction": "查天气"}}), "查天气")
        self.assertEqual(amients.task_text(TASK), '给 A 发「明天十点开会」\n附带的内容：{"chat": "A"}')
        self.assertEqual(amients.task_text({"payload": {"url": "x"}}), '{"url": "x"}')
        self.assertEqual(amients.task_text({"payload": None}), "")
        tasks_wf = workflows.load_goals()["workflows"][workflows.TASKS]
        prompt = workflows.task_prompt(tasks_wf, TASK)
        self.assertIn("常驻工作流「领中控任务」", prompt)
        self.assertIn("付款、删东西", prompt)              # 规则在 goals.json 里，能改
        self.assertIn("#7（message），来自「hong」", prompt)
        self.assertIn("明天十点开会", prompt)
        self.assertIn("没有写内容", workflows.task_prompt(tasks_wf, {"id": 1, "kind": "ping"}))

    def test_pull_then_complete(self):
        self.serve({("POST", "/api/v1/tasks/pull"): (200, {"tasks": [TASK, {"id": 8}]})})
        tasks = amients.pull_tasks(wait=3)
        self.assertEqual([t["id"] for t in tasks], [7])  # 没有 token 的不算领到
        self.assertEqual(self.calls[0][2], {"wait": 3, "limit": 1})

        def handle(task, lease):
            lease.note("打开 Lark")
            return "发好了"
        w = self.worker(handle)
        self.assertEqual(w.do(tasks[0]), "done")
        method, path, body = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/v1/tasks/7/complete"))
        self.assertEqual((body["token"], body["result"]["text"]), ("tok", "发好了"))
        self.assertIsNone(w.current)
        self.assertEqual([k for k, _, _ in self.events], ["task", "task_done"])
        self.assertEqual(self.events[0][2], {"task": 7, "kind": "message"})

    def test_fail_retry_and_lost(self):
        self.serve({("POST", "/api/v1/tasks/9/complete"): (409, {"error": "lease expired"})})

        def stopped(task, lease):
            raise amients.TaskFailed("在电脑前被停止了")

        def crashed(task, lease):
            raise RuntimeError("模型接口 502")
        self.assertEqual(self.worker(stopped).do(TASK), "failed")
        self.assertEqual(self.calls[-1][2], {"token": "tok", "error": "在电脑前被停止了", "retry": False})
        self.assertEqual(self.worker(crashed).do(TASK), "failed")
        self.assertEqual(self.calls[-1][2]["retry"], True)
        self.assertIn("模型接口 502", self.calls[-1][2]["error"])
        self.assertEqual(self.worker(lambda t, l: "好了").do({**TASK, "id": 9}), "lost")
        self.assertEqual([k for k, _, _ in self.events if k != "task"], ["task_failed", "task_failed", "task_lost"])
        for k, t, d in self.events:  # 记进工作流动态，上报到中控时失败是 error、被收回是 warn
            workflows.log_event(k, t, {"workflow": workflows.TASKS, **d})
        evs = [e for e in amients.pending(100) if e["kind"] != "workflow.task"]
        self.assertEqual([e["level"] for e in evs], ["error", "error", "warn"])
        self.assertEqual({e["origin"] for e in evs}, {"tasks"})

    def test_lease_progress_and_lost_stops_agent(self):
        self.serve({})
        lease = amients.Lease(TASK)
        self.assertEqual(lease.keep, 60.0)
        lease.note("读 Lark「A」")
        lease.beat()
        lease.beat()  # 没有新进度、离时限还远：不再报
        self.assertEqual(self.calls, [("POST", "/api/v1/tasks/7/progress", {"token": "tok", "progress": {"text": "读 Lark「A」"}})])
        mock.patch.stopall()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS_API_KEY": "k", "AMIENTS_URL": ""}).start()
        self.serve({("POST", "/api/v1/tasks/7/progress"): (409, None)})
        stopped = []
        lease.on_lost = lambda: stopped.append(True)
        lease.note("发消息")
        lease.beat()
        self.assertTrue(lease.lost.is_set())
        self.assertEqual(stopped, [True])

    def scheduler(self, final="发好了", stop=False):
        ran = []

        class FakeAgent:
            def __init__(self):
                self.stopped, self.origin, self.max_steps = False, "user", 25

            def run(self, task, cb):
                ran.append((self, task))
                cb.log("action", "Lark 发送给「A」")
                self.stopped = stop
                return final

            def stop(self):
                self.stopped = True
        return workflows.Scheduler(lambda: FakeAgent()), ran

    def submit(self, s, task=TASK):
        """在领任务线程里把任务交给调度（会阻塞到跑完），返回 (线程, 结果框, lease)。"""
        import threading
        box, lease = {}, amients.Lease(task)

        def go():
            try:
                box["result"] = s.submit_task(task, lease)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
        t = threading.Thread(target=go)
        t.start()
        for _ in range(100):
            if s.tasks:
                break
            time.sleep(0.01)
        return t, box, lease

    def test_scheduler_runs_task_as_its_own_workflow(self):
        s, ran = self.scheduler()
        self.assertNotIn(workflows.TASKS, s.due(time.time() + 99999))  # 不按时间跑，领到了才排进来
        t, box, lease = self.submit(s)
        self.assertEqual(lease.text, "排队中，等手上的事做完")
        self.assertFalse(s._can_take_task())  # 有排着的任务就不再领
        s.run_task(s.tasks.pop(0))           # 调度线程轮到它
        t.join(5)
        self.assertEqual(box, {"result": "发好了"})
        agent, prompt = ran[0]
        self.assertEqual((agent.origin, agent.max_steps), ("task#7", workflows.TASK_MAX_STEPS))
        self.assertIn("常驻工作流「领中控任务」", prompt)
        self.assertEqual(lease.text, "Lark 发送给「A」")  # 每个动作都带进进度
        self.assertEqual(s.running, {})
        self.assertIn("last_run", workflows.load_state()[workflows.TASKS])

    def test_preempted_task_goes_back_to_amients(self):
        s, _ = self.scheduler(final="", stop=True)
        t, box, _ = self.submit(s)
        s.run_task(s.tasks.pop(0))
        t.join(5)
        self.assertIsInstance(box["error"], amients.TaskFailed)
        self.assertTrue(box["error"].retry)  # 被电脑前的指令打断：让中控重新派发

    def test_lost_while_queued_is_dropped(self):
        s, ran = self.scheduler()
        t, box, lease = self.submit(s)
        lease.lost.set()
        t.join(5)
        self.assertEqual((box, s.tasks, ran), ({"result": ""}, [], []))

    def test_only_takes_tasks_when_free(self):
        s, _ = self.scheduler()
        self.assertTrue(s._can_take_task())
        s._working = True                    # 正在跑消息巡检
        self.assertFalse(s._can_take_task())
        s._working = False
        s.busy.set()                         # 你在终端里下的指令在跑
        self.assertFalse(s._can_take_task())
        s.busy.clear()
        s.can_work = lambda: False           # 没有辅助功能 / 屏幕录制权限
        self.assertFalse(s._can_take_task())
        s.can_work = lambda: True
        g = workflows.load_goals()
        g["workflows"][workflows.TASKS]["enabled"] = False  # /tasks off
        workflows.save_goals(g)
        self.assertFalse(s._can_take_task())
