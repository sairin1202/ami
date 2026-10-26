"""Jev 快速模式的离线测试：网络和屏幕都用桩替换，不会真的点击。

运行: .venv/bin/python -m unittest discover -v tests
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", __import__("tempfile").mkdtemp(prefix="cu-test-"))  # 不写真实目录

import agent  # noqa: E402
import axui  # noqa: E402
import jev  # noqa: E402

APPS = {"A1": {"name": "备忘录 (Notes)", "path": "/System/Applications/Notes.app"}}


def el(i, role, label, ops):
    return axui.Element(index=str(i), ref=None, role=role, label=label, value=None,
                        frame=(0, 0, 10, 10), operations=ops)


def snap(*elements, title="窗口"):
    return axui.Snapshot("TestApp", "com.test", 1, title, list(elements), "")


def answer(ids, pick, conf=0.9):
    return {"choice": pick, "confidence": conf, "probabilities": {i: float(i == pick) for i in ids}}


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self.data


class StubHttp:
    """按脚本回答：每步给 (操作, 目标编号, 操作置信度, 目标置信度)。"""

    def __init__(self, script):
        self.script, self.bodies = list(script), []

    def post(self, url, json, headers):
        self.bodies.append(json)
        op, target, conf, tconf = self.script.pop(0)
        q = json["questions"]
        answers = {"operation": answer(q["operation"]["criteria"], op, conf)}
        for name, head in q.items():
            if name != "operation":
                ids = list(head["criteria"])
                answers[name] = answer(ids, target if target in ids else ids[0], tconf)
        return Resp({"answers": answers, "model": "stub"})


class JevTest(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(axui, "installed_apps", return_value=APPS),
                   mock.patch.object(axui, "click"), mock.patch.object(axui, "type_text"),
                   mock.patch.object(axui, "open_app"), mock.patch.object(jev.computer, "execute"),
                   mock.patch.object(jev.time, "sleep")]
        self.mocks = [p.start() for p in patches]
        self.click, self.type_text, self.open_app = self.mocks[1], self.mocks[2], self.mocks[3]
        self.addCleanup(mock.patch.stopall)
        self.logs = []

    def runner(self, script, snaps, text="hello"):
        client = jev.JevClient("key")
        client.http = StubHttp(script)
        self.http = client.http
        snaps = list(snaps)
        mock.patch.object(axui, "read", side_effect=lambda: snaps.pop(0) if len(snaps) > 1 else snaps[0]).start()
        r = jev.JevRunner(client, text_fn=lambda ctx: text)
        return r.run("目标", lambda k, t: self.logs.append(t), threading.Event())

    def test_request_heads(self):
        s = snap(el(1, "AXButton", "搜索", ["CLICK"]), el(2, "AXTextField", "地址", ["TYPE_TEXT", "CLICK"]))
        self.runner([("DONE", None, 0.9, 0.9)], [s])
        q = self.http.bodies[0]["questions"]
        self.assertEqual(set(q), {"operation", "click_target", "type_text_target", "open_app_target"})
        self.assertEqual(set(q["click_target"]["criteria"]), {"1", "2"})
        self.assertNotIn("PRESS_ENTER", q["operation"]["criteria"])  # 只有刚输入过才提供回车

    def test_click_then_done(self):
        s1 = snap(el(1, "AXButton", "下一步", ["CLICK"]), title="a")
        s2 = snap(el(1, "AXButton", "完成", ["CLICK"]), title="b")
        r = self.runner([("CLICK", "1", 0.9, 0.9), ("DONE", None, 0.9, 0.9)], [s1, s2])
        self.assertEqual(r.status, "done")
        self.assertEqual(self.click.call_count, 1)
        self.assertTrue(self.http.bodies[1]["state"]["recent_actions"][0]["ui_changed"])

    def test_type_text_then_enter_offered(self):
        s1 = snap(el(1, "AXTextField", "搜索", ["TYPE_TEXT", "CLICK"]), title="a")
        s2 = snap(el(1, "AXTextField", "搜索", ["TYPE_TEXT", "CLICK"]), title="b")
        r = self.runner([("TYPE_TEXT", "1", 0.9, 0.9), ("DONE", None, 0.9, 0.9)], [s1, s2])
        self.type_text.assert_called_once()
        self.assertEqual(self.type_text.call_args.args[1], "hello")
        self.assertIn("PRESS_ENTER", self.http.bodies[1]["questions"]["operation"]["criteria"])
        self.assertEqual(r.status, "done")

    def test_missing_text_blocks(self):
        s = snap(el(1, "AXTextField", "邮箱", ["TYPE_TEXT"]))
        r = self.runner([("TYPE_TEXT", "1", 0.9, 0.9)], [s], text=None)
        self.assertEqual(r.status, "blocked")
        self.type_text.assert_not_called()

    def test_low_confidence_hands_off(self):
        s = snap(el(1, "AXButton", "确定", ["CLICK"]))
        r = self.runner([("CLICK", "1", 0.9, 0.4)], [s])
        self.assertEqual(r.status, "low_confidence")
        self.click.assert_not_called()

    def test_open_app_uses_target_confidence(self):
        s1 = snap(el(1, "AXButton", "memU", ["CLICK"]), title="a")
        s2 = snap(el(1, "AXButton", "新建备忘录", ["CLICK"]), title="b")
        r = self.runner([("OPEN_APP", "A1", 0.42, 0.93), ("DONE", None, 0.9, 0.9)], [s1, s2])
        self.open_app.assert_called_once_with("/System/Applications/Notes.app")
        self.assertEqual(r.status, "done")

    def test_risky_click_runs_without_asking(self):
        s1, s2 = snap(el(1, "AXButton", "删除", ["CLICK"])), snap(el(2, "AXButton", "撤销", ["CLICK"]), title="b")
        r = self.runner([("CLICK", "1", 0.95, 0.95), ("DONE", None, 0.9, 0.9)], [s1, s2])
        self.assertEqual(r.status, "done")
        self.click.assert_called_once()

    def test_stuck_when_ui_unchanged(self):
        s = snap(el(1, "AXButton", "刷新", ["CLICK"]))
        r = self.runner([("CLICK", "1", 0.9, 0.9)] * 3, [s])
        self.assertEqual(r.status, "stuck")
        self.assertEqual(self.click.call_count, 2)

    def test_invalid_answer_rejected(self):
        s = snap(el(1, "AXButton", "确定", ["CLICK"]))
        client = jev.JevClient("key")
        client.http = mock.Mock(post=mock.Mock(return_value=Resp({"answers": {"operation": {"choice": "NOPE"}}})))
        with self.assertRaises(jev.JevError):
            client.choose("目标", s, [])

    def test_http_error(self):
        client = jev.JevClient("key")
        client.http = mock.Mock(post=mock.Mock(return_value=Resp({}, status=401)))
        with self.assertRaises(jev.JevError):
            client.choose("目标", snap(el(1, "AXButton", "确定", ["CLICK"])), [])


class HandoffTest(unittest.TestCase):
    def make(self, result):
        a = agent.Agent(api_key="sk-test", use_jev=True)
        a.jev = mock.Mock(run=mock.Mock(return_value=result))
        return a

    def test_done_asks_gpt_to_verify(self):
        a = self.make(jev.JevResult("done", "", ["CLICK [1] Button '确定'"]))
        task = a._run_jev("任务", agent.AgentCallbacks(log=lambda k, t: None))
        self.assertIn("核实", task)
        self.assertIn("CLICK [1]", task)

    def test_no_actions_passes_task_through(self):
        a = self.make(jev.JevResult("blocked", "x", []))
        self.assertEqual(a._run_jev("任务", agent.AgentCallbacks(log=lambda k, t: None)), "任务")

    def test_stopped(self):
        a = self.make(jev.JevResult("stopped"))
        self.assertIsNone(a._run_jev("任务", agent.AgentCallbacks(log=lambda k, t: None)))


if __name__ == "__main__":
    unittest.main()
