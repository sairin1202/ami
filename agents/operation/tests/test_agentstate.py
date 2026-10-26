"""Ami 自己的状态（agent_state.json）：账号、运行状态、现算的部分，中控派 agent.state 时交回去。不联网、不碰屏幕。"""

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import agentstate  # noqa: E402
import amients  # noqa: E402
import checkup  # noqa: E402
import tools  # noqa: E402
import workflows as wf  # noqa: E402


class AgentStateTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS": "on", "AMIENTS_API_KEY": ""}).start()
        mock.patch.object(agentstate, "permissions", return_value={"辅助功能": True, "屏幕录制": True}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def test_accounts_keep_when_it_changed(self):
        with mock.patch("time.time", return_value=100.0):
            agentstate.set_account("B 站 命令行登录", False, kind="auth", detail="未登录", hint="bili login")
        with mock.patch("time.time", return_value=200.0):
            agentstate.set_account("B 站 命令行登录", False, kind="auth", detail="未登录")
        a = agentstate.load()["accounts"]["B 站 命令行登录"]
        self.assertEqual((a["ok"], a["checked_at"], a["changed_at"]), (False, 200.0, 100.0))  # 一直坏着：从 100 开始
        with mock.patch("time.time", return_value=300.0):
            agentstate.record_checks([{"name": "B 站 命令行登录", "ok": True}])
        a = agentstate.load()["accounts"]["B 站 命令行登录"]
        self.assertEqual((a["ok"], a["changed_at"], a["source"]), (True, 300.0, "check"))

    def test_running_follows_status_even_when_reporting_off(self):
        with mock.patch.dict(os.environ, {"AMIENTS": "off"}):
            amients.set_status("running", "正在巡检", origin="inbox")
        self.assertEqual(agentstate.load()["running"]["summary"], "正在巡检")
        self.assertEqual(amients._read_status(), {})  # 不上报

    def test_tool_login_failures_are_noted(self):
        acct = checkup.account_for_tool("xhs")
        fake = mock.Mock(call_tool=mock.Mock(return_value={"status": "error", "code": "not_authenticated",
                                                           "detail": "Session expired", "hint": "去登录"}))
        with mock.patch.object(tools, "_owner", return_value=fake), mock.patch("msgstore.record"):
            tools.call("xhs", {"command": "feed", "args": []})
            self.assertEqual(agentstate.load()["accounts"][acct]["detail"], "Session expired")
            fake.call_tool.return_value = {"status": "ok", "data": {}}
            tools.call("xhs", {"command": "feed", "args": []})       # 成功不算登录着
            fake.call_tool.return_value = {"status": "error", "detail": "网络超时"}
            tools.call("bili", {"command": "feed", "args": []})      # 不是没登录的错不记
        acc = agentstate.load()["accounts"]
        self.assertFalse(acc[acct]["ok"])
        self.assertNotIn(checkup.account_for_tool("bili"), acc)

    def test_snapshot_and_state_task(self):
        s = wf.Scheduler(lambda: None)
        agentstate.set_account("rednote 命令行登录", False, kind="auth", detail="Session expired")
        task = {"id": 7, "token": "t", "kind": "agent.state", "payload": {}}
        r = s.answer_state(task, amients.Lease(task))
        st = r["state"]
        self.assertIn("rednote 命令行登录 有问题", r["text"])
        for k in ("agent", "workflows", "amients", "tools", "permissions", "accounts", "updated_at"):
            self.assertIn(k, st)
        self.assertEqual([w["id"] for w in st["workflows"]], ["inbox", "tasks"])
        self.assertEqual(json.load(open(agentstate.path(), encoding="utf-8"))["agent"]["pid"], os.getpid())  # 写进了文件

    def test_fresh_rechecks_cli_without_browser(self):
        s = wf.Scheduler(lambda: None)
        results = [{"name": "rednote", "kind": "cli", "state": "ok", "detail": "已登录"}]
        with mock.patch.object(checkup, "run", return_value=results) as run, \
                mock.patch.object(checkup, "check_names", return_value=["rednote 命令行登录"]):
            task = {"id": 8, "token": "t", "kind": "agent.state", "payload": {"fresh": True}}
            r = s.answer_state(task, amients.Lease(task))
        self.assertFalse(run.call_args.kwargs["browser_too"])
        self.assertTrue(r["state"]["accounts"]["rednote 命令行登录"]["ok"])

    def test_state_kind_has_its_own_puller(self):
        self.assertIn("agent.state", [c["kind"] for c in wf.capabilities()])
        self.assertNotIn("agent.state", wf.task_kinds())   # 主线程不领：它要等屏幕空闲
        started = []
        mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k"}).start()
        mock.patch.object(amients.TaskWorker, "start", lambda self: started.append(self.kinds)).start()
        s = wf.Scheduler(lambda: None)
        mock.patch.object(s, "_loop").start()
        s.start()
        s.stop()
        self.assertIn(sorted(wf.STATE_KINDS), started)
        self.assertEqual(agentstate.load()["running"]["state"], "idle")  # 刚启动就记下运行状态
        self.assertTrue(s.state_worker.ready())             # 忙的时候也领
        events = []
        with mock.patch.object(wf, "log_event", side_effect=lambda k, t, d: events.append(k)):
            s._state_event("task", "领到", {})
            s._state_event("task_done", "做完", {})
            s._state_event("task_failed", "失败", {})
        self.assertEqual(events, ["task_failed"])           # 时不时来要状态，不刷屏

    def test_checkup_updates_accounts(self):
        s = wf.Scheduler(lambda: None)
        res = [{"name": "rednote", "kind": "cli", "state": "login", "detail": "expired", "hint": "去登录"}]
        with mock.patch.object(checkup, "run", return_value=res), \
                mock.patch.object(checkup, "check_names", return_value=["rednote 命令行登录", "x.com 网页登录"]), \
                mock.patch("agent.UsedApps", return_value=mock.Mock()):
            s.run_checkup(["x.com"])
        acc = agentstate.load()["accounts"]
        self.assertFalse(acc["rednote 命令行登录"]["ok"])
        self.assertNotIn("x.com 网页登录", acc)  # 被打断没查到的不改记下的状态


if __name__ == "__main__":
    unittest.main()
