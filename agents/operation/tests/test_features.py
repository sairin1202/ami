"""借鉴开源项目新增的功能的离线测试：元素表精简、动作核对、动作缓存、历史摘要、卡住提示、经验记忆、运行报告。"""

import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["COMPUTER_USE_HOME"] = tempfile.mkdtemp(prefix="cu-test-")
os.environ["TYPESAFE_API_KEY"] = ""

import action_cache  # noqa: E402
import agent  # noqa: E402
import axui  # noqa: E402
import jev  # noqa: E402
import memory  # noqa: E402
import report  # noqa: E402


def el(i, role, label, ops, frame=(0, 0, 100, 30), context="", actions=("AXPress",)):
    return axui.Element(index=str(i), ref=object(), role=role, label=label, value=None, frame=frame,
                        operations=ops, actions=actions, context=context)


def snap(*elements, bundle="com.test"):
    return axui.Snapshot("TestApp", bundle, 1, "窗口", list(elements), "")


class ElementTableTest(unittest.TestCase):
    def test_dedupe_nested_clickable(self):
        parent = el(1, "AXButton", "保存", ["CLICK"], frame=(0, 0, 100, 30))
        child = el(2, "AXImage", "保存", ["CLICK"], frame=(10, 5, 20, 20))
        field = el(3, "AXTextField", "", ["TYPE_TEXT", "CLICK"], frame=(5, 5, 50, 20))
        other = el(4, "AXButton", "取消", ["CLICK"], frame=(200, 0, 100, 30))
        out = axui._dedupe([parent, child, field, other])
        self.assertEqual([e.label for e in out], ["保存", "", "取消"])
        self.assertEqual([e.index for e in out], ["1", "2", "3"])

    def test_mark_new(self):
        a = snap(el(1, "AXButton", "确定", ["CLICK"]))
        b = snap(el(1, "AXButton", "确定", ["CLICK"]), el(2, "AXMenuItem", "复制", ["CLICK"]))
        self.assertEqual(axui.mark_new(b, a), 1)
        self.assertEqual([e.is_new for e in b.elements], [False, True])
        self.assertTrue(b.elements[1].payload()["new"])


class ActionVerifyTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(axui.computer, "execute").start()
        mock.patch.object(axui.time, "sleep").start()
        mock.patch.object(axui.AS, "AXUIElementPerformAction", return_value=0).start()
        mock.patch.object(axui.AS, "AXUIElementSetAttributeValue", return_value=0).start()
        self.addCleanup(mock.patch.stopall)

    def test_toggle_no_change_raises(self):
        mock.patch.object(axui, "_value", return_value=0).start()
        with self.assertRaises(axui.NoEffect):
            axui.click(el(1, "AXCheckBox", "自动保存", ["CLICK"]))
        self.assertEqual(axui.computer.execute.call_count, 1)  # AXPress 无效后又试了一次坐标点击

    def test_toggle_changed_ok(self):
        vals = iter([0, 1])
        mock.patch.object(axui, "_value", side_effect=lambda ref: next(vals)).start()
        axui.click(el(1, "AXCheckBox", "自动保存", ["CLICK"]))
        axui.computer.execute.assert_not_called()

    def test_typed_value_mismatch_raises(self):
        mock.patch.object(axui, "_value", return_value="别的内容").start()
        with self.assertRaises(axui.NoEffect):
            axui.type_text(el(1, "AXTextField", "搜索", ["TYPE_TEXT"]), "天气")

    def test_typed_value_ok(self):
        mock.patch.object(axui, "_value", return_value="东京 天气").start()
        axui.type_text(el(1, "AXTextField", "搜索", ["TYPE_TEXT"]), "东京天气")


class ActionCacheTest(unittest.TestCase):
    def setUp(self):
        action_cache.invalidate("打开设置")
        self.exec = mock.patch.object(jev, "execute").start()
        mock.patch.object(action_cache.time, "sleep").start()
        mock.patch.object(axui, "augment_with_ocr", return_value=0).start()
        self.addCleanup(mock.patch.stopall)

    STEPS = [{"op": "CLICK", "app": "com.test", "element": ["AXButton", "设置", ""], "app_path": None, "text": None}]

    def test_put_get_invalidate(self):
        self.assertIsNone(action_cache.get("打开设置"))
        action_cache.put("打开设置", self.STEPS)
        self.assertEqual(action_cache.get("  打开设置 "), self.STEPS)
        action_cache.invalidate("打开设置")
        self.assertIsNone(action_cache.get("打开设置"))

    def test_replay_matches_element(self):
        mock.patch.object(axui, "read", return_value=snap(el(1, "AXButton", "设置", ["CLICK"]))).start()
        r = action_cache.replay("打开设置", self.STEPS, lambda k, t: None, threading.Event())
        self.assertTrue(r.ok)
        self.exec.assert_called_once()

    def test_replay_missing_element_fails(self):
        mock.patch.object(axui, "read", return_value=snap(el(1, "AXButton", "关于", ["CLICK"]))).start()
        r = action_cache.replay("打开设置", self.STEPS, lambda k, t: None, threading.Event())
        self.assertFalse(r.ok)
        self.exec.assert_not_called()

    def test_replay_risky_click_runs(self):
        steps = [dict(self.STEPS[0], element=["AXButton", "删除", ""])]
        mock.patch.object(axui, "read", return_value=snap(el(1, "AXButton", "删除", ["CLICK"]))).start()
        r = action_cache.replay("x", steps, lambda k, t: None, threading.Event())
        self.assertTrue(r.ok)
        self.exec.assert_called_once()


class AgentFeatureTest(unittest.TestCase):
    def make(self):
        a = agent.Agent(api_key="sk-test")
        return a

    def test_after_success_caches_only_clean_runs(self):
        a = self.make()
        cb = agent.AgentCallbacks(log=lambda k, t: None)
        with mock.patch.object(action_cache, "put") as put:
            a._cache_candidate, a._gpt_effective = ("任务", [{"op": "CLICK"}], False), 0
            a._after_success("已完成", cb)
            put.assert_called_once()
        with mock.patch.object(action_cache, "put") as put:
            a._cache_candidate, a._gpt_effective = ("任务", [{"op": "CLICK"}], False), 2
            a._after_success("已完成", cb)
            put.assert_not_called()
        with mock.patch.object(action_cache, "invalidate") as inv:
            a._cache_candidate, a._gpt_effective = ("任务", [{"op": "CLICK"}], True), 1
            a._after_success("已完成", cb)
            inv.assert_called_once()

    def test_summarize_keeps_call_output_pairs(self):
        a = self.make()
        big = "x" * 2000
        hist = [{"role": "user", "content": [{"type": "input_text", "text": "任务"}]}]
        for i in range(30):
            hist += [{"type": "reasoning", "id": f"r{i}", "summary": []},
                     {"type": "computer_call", "call_id": f"c{i}", "actions": [{"type": "click", "x": 1, "y": 2}]},
                     {"type": "computer_call_output", "call_id": f"c{i}", "output": {"image_url": "data:"}},
                     {"role": "assistant", "content": [{"type": "output_text", "text": big}]}]
        a.history = hist
        a.client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(output_text="摘要内容")))
        with mock.patch.object(agent, "SUMMARY_TRIGGER_CHARS", 10000):
            a._maybe_summarize(None)
        self.assertIn("摘要内容", a.history[0]["content"][0]["text"])
        first = a.history[1]
        self.assertNotIn(first.get("type"), ("computer_call_output", "function_call_output"))
        calls = {x["call_id"] for x in a.history if x.get("type") == "computer_call"}
        outs = {x["call_id"] for x in a.history if x.get("type") == "computer_call_output"}
        self.assertEqual(calls, outs)  # 每个保留下来的调用都有结果，反之亦然

    def test_loop_nudge_on_repeat(self):
        a = self.make()
        sig = a._action_sig({"type": "click", "x": 100, "y": 100})
        self.assertIsNone(a._loop_nudge([sig], None))
        self.assertIsNone(a._loop_nudge([sig], None))
        self.assertIn("重复", a._loop_nudge([sig], None))
        self.assertIsNone(a._loop_nudge([sig], None))  # 提示过后有冷却

    def test_pause_resume_injects_note(self):
        a = self.make()
        a.pause()
        next_input = [{"type": "computer_call_output", "call_id": "c1", "output": {"image_url": "old"}}]
        threading.Timer(0.2, lambda: a.resume("1. 点击了 按钮「保存」")).start()
        with mock.patch.object(agent.computer, "screenshot_data_url", return_value=("new", b"")):
            stopped = a._wait_if_paused(next_input, agent.AgentCallbacks(log=lambda k, t: None))
        self.assertFalse(stopped)
        self.assertEqual(next_input[0]["output"]["image_url"], "new")
        self.assertIn("按钮「保存」", next_input[-1]["content"])


class ModelWaitTest(unittest.TestCase):
    def test_stop_abandons_slow_request(self):
        import time as _t
        a = agent.Agent(api_key="sk-test")
        a.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: _t.sleep(3)))
        progress = []
        a._cb = agent.AgentCallbacks(log=lambda k, t: progress.append((k, t)))
        threading.Timer(0.3, a._stop.set).start()
        t0 = _t.time()
        self.assertIsNone(a._create([{"role": "user", "content": "hi"}]))
        self.assertLess(_t.time() - t0, 1.5)  # 不用等到请求结束

    def test_progress_shown_while_waiting(self):
        import time as _t
        a = agent.Agent(api_key="sk-test")
        a.client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kw: (_t.sleep(3.3), SimpleNamespace(output=[]))[1]))
        logs = []
        a._cb = agent.AgentCallbacks(log=lambda k, t: logs.append((k, t)))
        a._create([{"role": "user", "content": "hi"}])
        self.assertEqual([k for k, _ in logs], ["progress"])  # 3 秒时更新一次，不刷屏


class StreamTest(unittest.TestCase):
    def test_stream_collects_completed_response(self):
        done = SimpleNamespace(output=[])
        events = [SimpleNamespace(type="response.created"),
                  SimpleNamespace(type="response.output_text.delta", delta="你好"),
                  SimpleNamespace(type="response.completed", response=done)]
        a = agent.Agent(api_key="sk-test")
        a.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: iter(events) if kw.get("stream")
                                                             else self.fail("应该用流式")))
        live = {"last": 0, "chars": 0, "stream": None}
        self.assertIs(a._request({}, live), done)
        self.assertEqual(live["chars"], 2)

    def test_stream_failed_event_raises(self):
        a = agent.Agent(api_key="sk-test")
        bad = SimpleNamespace(type="response.failed", response=SimpleNamespace(error="overloaded"))
        a.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: iter([bad])))
        with self.assertRaises(RuntimeError):
            a._request({}, {"last": 0, "chars": 0, "stream": None})

    def test_stream_unsupported_falls_back(self):
        import openai
        a = agent.Agent(api_key="sk-test")
        plain = SimpleNamespace(output=[])

        def create(**kw):
            if kw.get("stream"):
                raise openai.BadRequestError("stream not supported", response=mock.MagicMock(status_code=400), body=None)
            return plain
        a.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        self.assertIs(a._request({}, {"last": 0, "chars": 0, "stream": None}), plain)
        self.assertFalse(a._stream_ok)  # 之后不再尝试流式


class WatchdogTest(unittest.TestCase):
    def test_shrink_history_drops_links_and_cuts_text(self):
        a = agent.Agent(api_key="sk-test")
        page = {"url": "u", "text": "字" * 9000, "links": [{"text": "a", "url": "b"}] * 50}
        a.history = [{"type": "function_call_output", "call_id": "f", "output": json.dumps(page, ensure_ascii=False)},
                     {"type": "function_call_output", "call_id": "g", "output": "短结果"}]
        self.assertTrue(a._shrink_history())
        data = json.loads(a.history[0]["output"])
        self.assertNotIn("links", data)
        self.assertLess(len(data["text"]), 3100)
        self.assertEqual(a.history[1]["output"], "短结果")
        self.assertFalse(a._shrink_history())  # 已经精简过，不再改动

    def test_no_first_event_retries_with_shrunk_history(self):
        import time as _t
        a = agent.Agent(api_key="sk-test")
        big = json.dumps({"text": "x" * 9000, "links": [{"url": "y"}] * 40})
        inputs = []
        done = SimpleNamespace(output=[])

        def create(**kw):
            inputs.append(kw["input"])
            if len(inputs) == 1:  # 第一次：中转一声不响地挂住
                def hang():
                    _t.sleep(5)
                    yield SimpleNamespace(type="response.completed", response=done)
                return hang()
            return iter([SimpleNamespace(type="response.completed", response=done)])
        a.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        logs = []
        a._cb = agent.AgentCallbacks(log=lambda k, t: logs.append((k, t)))
        with mock.patch.object(agent, "FIRST_EVENT_TIMEOUT", 0.5):
            t0 = _t.time()
            self.assertIs(a._create([{"type": "function_call_output", "call_id": "f", "output": big}]), done)
        self.assertLess(_t.time() - t0, 3)
        self.assertEqual(len(inputs), 2)
        self.assertNotIn("links", inputs[1][-1]["output"])
        self.assertTrue(any("精简" in t for k, t in logs if k == "status"))


class ResendTest(unittest.TestCase):
    def _agent(self, behaviours):
        import time as _t
        a = agent.Agent(api_key="sk-test")
        done = SimpleNamespace(output=[])
        calls = []

        def create(**kw):
            mode = behaviours[min(len(calls), len(behaviours) - 1)]
            calls.append(mode)
            if mode == "ok":
                return iter([SimpleNamespace(type="response.completed", response=done)])

            def gen():
                if mode == "stall":  # 先回一点，然后停住
                    yield SimpleNamespace(type="response.output_text.delta", delta="半句")
                _t.sleep(3)
                yield SimpleNamespace(type="response.completed", response=done)
            return gen()
        a.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        a._cb = agent.AgentCallbacks(log=lambda k, t: None)
        return a, calls, done

    def test_stall_mid_stream_resends(self):
        a, calls, done = self._agent(["stall", "ok"])
        with mock.patch.object(agent, "STALL_TIMEOUT", 0.5), mock.patch.object(agent, "FIRST_EVENT_TIMEOUT", 0.5):
            self.assertIs(a._create([{"role": "user", "content": "hi"}]), done)
        self.assertEqual(calls, ["stall", "ok"])

    def test_stuck_request_ids_reported(self):
        live = {"request_id": "abc123", "response_id": "resp_1", "sent": 0}
        ids = agent.Agent._ids(live)
        self.assertIn("abc123", ids)
        self.assertIn("resp_1", ids)
        self.assertEqual(agent.Agent._ids({}), "")

    def test_gives_up_after_max_attempts(self):
        a, calls, _ = self._agent(["hang"])
        with mock.patch.object(agent, "FIRST_EVENT_TIMEOUT", 0.3), mock.patch.object(agent, "MAX_ATTEMPTS", 3):
            with self.assertRaises(RuntimeError) as ctx:
                a._create([{"role": "user", "content": "hi"}])
        self.assertEqual(len(calls), 3)
        self.assertIn("3 次", str(ctx.exception))


class MemoryReportTest(unittest.TestCase):
    def test_memory_roundtrip(self):
        client = SimpleNamespace(responses=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(output_text="系统设置里蓝牙在左侧栏第三项")))
        memory.remember(client, "m", "在系统设置里打开蓝牙", ["CLICK 蓝牙"], "已打开")
        self.assertIn("蓝牙在左侧栏", memory.with_hints("在系统设置里关闭蓝牙"))
        self.assertEqual(memory.with_hints("给张三发邮件"), "给张三发邮件")

    def test_memory_skips_trivial(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: SimpleNamespace(output_text="无")))
        self.assertIsNone(memory.remember(client, "m", "t", [], "ok"))

    def test_report_written(self):
        rec = report.RunRecorder("测试任务")
        rec.log("action", "click (1, 2)")
        path = rec.write()
        with open(path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("测试任务", page)
        self.assertIn("click (1, 2)", page)


class JevIdleTest(unittest.TestCase):
    def test_reobserve_loop_is_stuck(self):
        s = snap(el(1, "AXButton", "确定", ["CLICK"]))
        client = jev.JevClient("k")
        client.choose = lambda goal, snap, history: {"op": "REOBSERVE", "element": None, "app": None,
                                                    "confidence": 0.9, "target_confidence": None, "latency_ms": 1}
        with mock.patch.object(axui, "read", side_effect=lambda: snap(el(1, "AXButton", f"确定{id(object())}",
                                                                         ["CLICK"]))), \
                mock.patch.object(axui, "augment_with_ocr", return_value=0), mock.patch.object(jev.time, "sleep"):
            r = jev.JevRunner(client, text_fn=lambda c: None).run("目标", lambda k, t: None, threading.Event())
        self.assertEqual(r.status, "stuck")
        del s


if __name__ == "__main__":
    unittest.main()
