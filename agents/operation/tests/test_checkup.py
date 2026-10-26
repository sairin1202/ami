"""每日自检：命令行工具的 status、浏览器网站有没有掉线、中控派 checkup 任务时跑、要重新登录的报给中控。不联网、不碰屏幕。"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import amients  # noqa: E402
import checkup  # noqa: E402
import workflows as wf  # noqa: E402


def fake_cli(reply=None, installed=True):
    return SimpleNamespace(available=lambda: installed, LOGIN_HINT="请自己登录",
                           call_tool=lambda name, args: reply)


class CheckCliTest(unittest.TestCase):
    def test_states(self):
        c = checkup.check_cli
        self.assertEqual(c("X", fake_cli(installed=False), "twitter")["state"], "skipped")
        self.assertEqual(c("B 站", fake_cli({"status": "ok", "data": {"name": "me"}}), "bili")["state"], "ok")
        r = c("rednote", fake_cli({"status": "error", "code": "not_authenticated", "detail": "Session expired",
                                   "hint": "去浏览器登录 rednote"}), "xhs")
        self.assertEqual((r["state"], r["hint"]), ("login", "去浏览器登录 rednote"))
        r = c("Telegram", fake_cli({"status": "error", "code": "auth_error", "detail": "EOF when reading a line"}), "tg")
        self.assertEqual((r["state"], r["hint"]), ("login", "请自己登录"))  # 没有 hint 时用模块的登录提示
        r = c("B 站", fake_cli({"status": "ok", "data": {"authenticated": False}}), "bili")
        self.assertEqual(r["state"], "login")
        r = c("B 站", fake_cli({"status": "error", "detail": "ConnectionError: timed out"}), "bili")
        self.assertEqual(r["state"], "error")  # 网络问题不算掉线


class CheckSiteTest(unittest.TestCase):
    P = checkup.Probe("rednote.com", "https://www.rednote.com/notification", r"登录|Log in")

    def test_logged_out_signals(self):
        lo = checkup.logged_out
        self.assertIn("登录页", lo("https://www.zhihu.com/signin?next=%2F", False, "", self.P))
        self.assertIn("登录页", lo("https://passport.bilibili.com/login", False, "", self.P))
        self.assertEqual(lo("https://x.com/home?redirect_after_login=1", False, "首页", self.P), "")  # 参数不算
        self.assertIn("密码框", lo("https://www.reddit.com/settings/", True, "", self.P))
        self.assertIn("「登录」", lo("https://www.rednote.com/notification", False, "发现\n 登录 \n", self.P))
        self.assertEqual(lo("https://www.rednote.com/notification", False, "评论和@\n还没有登录记录", self.P), "")

    def browser(self, url, password=False, tab="新标签页", text=""):
        b = SimpleNamespace(open_url=mock.Mock(return_value={"tab": tab}), close_tab=mock.Mock(),
                            read_page=mock.Mock(return_value={"url": url, "text": text}),
                            has_password_field=mock.Mock(return_value=password))
        return mock.patch.dict(sys.modules, {"browser": b}), b

    def test_new_tab_is_closed(self):
        patch, b = self.browser("https://www.linkedin.com/authwall?trk=x")
        steps = []
        with patch, mock.patch.object(checkup, "SETTLE", 0):
            r = checkup.check_site(checkup.Probe("linkedin.com", "https://www.linkedin.com/feed/"), lambda: steps.append(1))
        self.assertEqual(r["state"], "login")
        self.assertIn("linkedin.com", r["hint"])
        b.close_tab.assert_called_once()
        self.assertEqual(steps, [1])

    def test_reused_tab_is_kept_and_errors_reported(self):
        patch, b = self.browser("https://www.zhihu.com/", tab="当前标签页")
        with patch, mock.patch.object(checkup, "SETTLE", 0):
            self.assertEqual(checkup.check_site(checkup.Probe("zhihu.com", "https://www.zhihu.com/"))["state"], "ok")
            b.close_tab.assert_not_called()
            b.read_page.side_effect = RuntimeError("没有正在运行的浏览器")
            self.assertEqual(checkup.check_site(checkup.Probe("zhihu.com", "https://www.zhihu.com/"))["state"], "error")

    def test_run_stops_when_user_takes_over(self):
        sites = []
        with mock.patch.object(checkup, "_cli_tools", return_value=[]), \
                mock.patch.object(checkup, "check_site", side_effect=lambda p, s: sites.append(p.site) or {}):
            checkup.run(["x.com", "zhihu.com"])
            self.assertEqual(sites, ["x.com", "zhihu.com"])
            sites.clear()
            checkup.run(None, stop=lambda: True)
            self.assertEqual(sites, [])


class CheckupTaskTest(unittest.TestCase):
    """自检由中控派 ami.check 任务触发：Ami 自己不定时跑；领到就用代码查，不交给 agent，按中控的格式回报每一项。"""

    RESULTS = [{"name": "rednote", "kind": "cli", "state": "login", "detail": "Session expired", "hint": "去登录"},
               {"name": "zhihu.com", "kind": "site", "state": "ok", "detail": "已登录"}]

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS": "on", "AMIENTS_API_KEY": ""}).start()
        mock.patch.object(wf, "QUIET_HOURS", "").start()
        mock.patch("agent.UsedApps", return_value=mock.Mock()).start()
        mock.patch.object(checkup, "_cli_tools", return_value=[("rednote", fake_cli(), "xhs"),
                                                              ("X", fake_cli(installed=False), "twitter")]).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def job(self, payload=None, kind="ami.check"):
        import threading
        task = {"id": 3, "token": "t", "kind": kind, "payload": payload}
        return {"task": task, "lease": amients.Lease(task), "done": threading.Event()}

    def test_not_scheduled_locally(self):
        s = wf.Scheduler(lambda: None)
        self.assertNotIn(wf.CHECKUP, wf.load_goals()["workflows"])
        self.assertNotIn(wf.CHECKUP, s.due(time.time() + 10 ** 7))

    def test_checkup_task_runs_code_not_agent(self):
        s = wf.Scheduler(lambda: self.fail("自检不该建 agent"))
        job = self.job({"sites": ["zhihu.com"]})
        with mock.patch.object(checkup, "run", return_value=self.RESULTS) as run:
            s.run_task(job)
        self.assertEqual(run.call_args.args[0], ["zhihu.com"])
        self.assertTrue(run.call_args.kwargs["browser_too"])
        self.assertEqual(job["result"]["text"], "rednote 要重新登录；1 个正常")
        self.assertEqual(job["result"]["checks"], [  # 中控自检的格式；没装的 X 不算自检项
            {"name": "rednote 命令行登录", "ok": False, "kind": "auth", "message": "Session expired", "hint": "去登录"},
            {"name": "zhihu.com 网页登录", "ok": True}])
        self.assertEqual(s.running, {})
        evs = {e["kind"]: e for e in amients.pending(100)}
        self.assertEqual(evs["workflow.check_alert"]["level"], "warn")  # 中控里归到「需要关注」
        self.assertIn("rednote 要重新登录", evs["workflow.check_alert"]["text"])
        self.assertEqual(amients._read_status()["status"]["state"], "warn")

    def test_checks_listed_by_amients_decide_sites(self):
        s = wf.Scheduler(lambda: None)
        job = self.job({"reason": "first", "checks": ["rednote 命令行登录", "zhihu.com 网页登录"]})
        with mock.patch.object(checkup, "run", return_value=self.RESULTS) as run:
            s.run_task(job)
        self.assertEqual(run.call_args.args[0], ["zhihu.com"])
        self.assertNotIn("被打断", job["result"]["text"])
        alert = [e for e in wf.recent_events() if e["kind"] == "check_alert"][0]["text"]
        self.assertNotIn("。。", alert)

    def test_sites_off_is_not_interrupted(self):
        s = wf.Scheduler(lambda: None)
        with mock.patch.dict(os.environ, {"CHECKUP_SITES": "off"}), \
                mock.patch.object(checkup, "run", return_value=self.RESULTS[:1]):
            r = s.run_checkup()
        self.assertFalse(r["stopped"])

    def test_cli_only_and_kind_aliases(self):
        s = wf.Scheduler(lambda: None)
        job = self.job({"browser": False}, kind="Login-Check")
        with mock.patch.object(checkup, "run", return_value=self.RESULTS[:1]) as run:
            s.run_task(job)
        self.assertFalse(run.call_args.kwargs["browser_too"])
        self.assertIn("result", job)

    def test_interrupted_checkup_reports_unchecked(self):
        s = wf.Scheduler(lambda: None)
        job = self.job({"sites": ["zhihu.com", "x.com"]})
        with mock.patch.object(checkup, "run", return_value=self.RESULTS):  # 只看了一个网站
            s.run_task(job)
        self.assertIn("被打断", job["result"]["text"])
        x = [c for c in job["result"]["checks"] if c["name"] == "x.com 网页登录"]
        self.assertEqual((x[0]["ok"], x[0]["kind"]), (False, "other"))  # 没查到的也报上，算没通过

    def test_check_names_and_sites_env(self):
        self.assertEqual(checkup.check_names([]), ["rednote 命令行登录"])
        with mock.patch.dict(os.environ, {"CHECKUP_SITES": "x.com, zhihu.com, nope.com"}):
            self.assertEqual(checkup.default_sites(), ["x.com", "zhihu.com"])
            self.assertEqual(checkup.check_names()[1:], ["x.com 网页登录", "zhihu.com 网页登录"])
        with mock.patch.dict(os.environ, {"CHECKUP_SITES": "off"}):
            self.assertEqual(checkup.default_sites(), [])
        net = checkup.to_checks([{"name": "B 站", "kind": "cli", "state": "error", "detail": "ConnectTimeout"}], [])
        self.assertEqual(net[0]["kind"], "network")

    def test_register_capabilities_and_checks_before_pulling(self):
        calls = []
        mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k", "CHECKUP_SITES": "zhihu.com"}).start()
        mock.patch.object(amients, "_request", side_effect=lambda m, path, body=None, **kw: calls.append((m, path, body))
                          or ({"tasks": []} if path.endswith("pull") else None)).start()
        s = wf.Scheduler(lambda: None)
        w = amients.TaskWorker(lambda t, l: "", lambda: True, lambda *a: None, wait=0, setup=s.register,
                               kinds=wf.task_kinds())
        w.start()
        for _ in range(100):
            if any(p.endswith("pull") for _, p, _ in calls):
                break
            time.sleep(0.02)
        w.stop()
        paths_ = [p for _, p, _ in calls]
        self.assertEqual(paths_[:3], ["/api/v1/capabilities", "/api/v1/checks", "/api/v1/tasks/pull"])
        caps = calls[0][2]["capabilities"]
        self.assertEqual([c["kind"] for c in caps], ["computer.do", "message.send", "message.inbox", "post.draft", "agent.state", "web.research"])
        self.assertTrue(all({"title", "description", "input", "example"} <= set(c) for c in caps))
        self.assertEqual(calls[1][2], {"checks": ["rednote 命令行登录", "zhihu.com 网页登录"]})
        kinds = calls[2][2]["kinds"]
        self.assertIn("ami.check", kinds)                      # 自检任务一定要领
        self.assertIn("message.send", kinds)
        with mock.patch.dict(os.environ, {"AMIENTS_TASK_KINDS": "all"}):
            self.assertIsNone(wf.task_kinds())

    def test_prompt_includes_capability(self):
        prompt = wf.task_prompt(wf.load_goals()["workflows"][wf.TASKS],
                                {"id": 5, "kind": "message.send", "payload": {"app": "Lark", "to": "A", "text": "hi"}})
        self.assertIn("任务类型「替用户发消息」", prompt)
        self.assertIn('附带的内容：{"app": "Lark", "to": "A"}', prompt)

    def test_structured_result_reported(self):
        calls = []
        mock.patch.dict(os.environ, {"AMIENTS_API_KEY": "k"}).start()
        mock.patch.object(amients, "report_task", side_effect=lambda t, a, **b: calls.append((a, b))).start()
        w = amients.TaskWorker(lambda t, l: {"text": "都正常", "checks": []}, lambda: True, lambda *a: None)
        self.assertEqual(w.do({"id": 3, "token": "t", "kind": "ami.check"}), "done")
        action, body = calls[-1]
        self.assertEqual(action, "complete")
        self.assertEqual((body["result"]["text"], body["result"]["checks"]), ("都正常", []))


if __name__ == "__main__":
    unittest.main()
