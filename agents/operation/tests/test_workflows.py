"""常驻工作流：改目标、只处理新情况、发出前 Jev 判断、调度。不调模型、不碰屏幕。"""

import copy
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

import workflows as wf  # noqa: E402


class FakeLLM:
    def __init__(self, text):
        self.text, self.inputs = text, []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.inputs.append(json.loads(kw["input"]))
        if isinstance(self.text, Exception):
            raise self.text
        return SimpleNamespace(output=[SimpleNamespace(type="message", content=[SimpleNamespace(text=self.text)])])


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        mock.patch.object(wf, "QUIET_HOURS", "").start()  # 测试结果不随跑测试的时间变
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)


class GoalTest(Base):
    def test_defaults(self):
        g = wf.load_goals()["workflows"]["inbox"]
        self.assertEqual((g["interval_min"], g["interval_max"]), (30, 60))
        self.assertTrue(g["enabled"])

    def test_old_fixed_interval_becomes_random_range(self):
        old = copy.deepcopy(wf.DEFAULT_GOALS)
        old["workflows"]["inbox"]["interval_min"] = 5
        del old["workflows"]["inbox"]["interval_max"]
        wf.save_goals(old)
        g = wf.load_goals()["workflows"]["inbox"]
        self.assertEqual((g["interval_min"], g["interval_max"]), (30, 60))

    def test_goal_change_applied_without_task(self):
        new_rules = wf.DEFAULT_GOALS["workflows"]["inbox"]["rules"] + ["陈宏找我就回「收到」"]
        llm = FakeLLM(json.dumps({"workflows": {"inbox": {"rules": new_rules, "interval_min": 10}}, "task": ""},
                                 ensure_ascii=False))
        r = wf.update_goals("以后陈宏找我就回收到，巡检改成 10 分钟", llm, "m")
        self.assertEqual(r, {"changed": ["inbox"], "task": ""})
        g = wf.load_goals()["workflows"]["inbox"]
        self.assertIn("陈宏找我就回「收到」", g["rules"])
        self.assertEqual((g["interval_min"], g["interval_max"]), (10, 60))
        self.assertEqual(g["goal"], wf.DEFAULT_GOALS["workflows"]["inbox"]["goal"])  # 没提到的字段不动
        self.assertIn("current_workflows", llm.inputs[0])
        ev = [e for e in wf.recent_events() if e["kind"] == "goal_changed"]
        self.assertEqual(ev[0]["data"]["user_said"], "以后陈宏找我就回收到，巡检改成 10 分钟")

    def test_one_off_task_does_not_change_goals(self):
        r = wf.update_goals("给 hong 发个消息", FakeLLM('{"workflows": {}, "task": "给 hong 发个消息"}'), "m")
        self.assertEqual(r, {"changed": [], "task": "给 hong 发个消息"})
        self.assertFalse(os.path.exists(os.path.join(self.home.name, "goals.json")))

    def test_bad_model_output_falls_back_to_task(self):
        for bad in ("我觉得应该……", RuntimeError("boom")):
            r = wf.update_goals("看看谁找我", FakeLLM(bad), "m")
            self.assertEqual(r, {"changed": [], "task": "看看谁找我"})

    def test_interval_floor(self):
        wf.update_goals("x", FakeLLM('{"workflows": {"inbox": {"interval_min": 0, "interval_max": "x"}}, "task": ""}'),
                        "m")
        g = wf.load_goals()["workflows"]["inbox"]
        self.assertEqual((g["interval_min"], g["interval_max"]), (1, 60))
        wf.update_goals("x", FakeLLM('{"workflows": {"inbox": {"interval_min": 90}}, "task": ""}'), "m")
        g = wf.load_goals()["workflows"]["inbox"]
        self.assertEqual((g["interval_min"], g["interval_max"]), (90, 90))  # 上限不小于下限


class SchedulerTest(Base):
    def test_due(self):
        s = wf.Scheduler(lambda: None)
        self.assertEqual(s.due(), [])  # 刚启动不马上去点界面，也是随机等 30～60 分钟
        first = wf.load_state()["inbox"]["next_run"]
        self.assertTrue(time.time() + 30 * 60 - 5 <= first <= time.time() + 60 * 60)
        self.assertEqual(s.due(first - 1), [])
        self.assertEqual(s.due(first), ["inbox"])
        self.assertEqual(wf.load_state()["inbox"]["next_run"], first)  # 只排一次，不会每轮重新随机
        g = wf.load_goals()
        g["workflows"]["inbox"]["enabled"] = False
        wf.save_goals(g)
        self.assertEqual(s.due(time.time() + 9999), [])

    def test_next_run_is_random_30_to_60_min(self):
        s = wf.Scheduler(lambda: None)
        gaps = set()
        for _ in range(20):
            with mock.patch.object(wf, "inbox_snapshot", return_value=[]):
                s.run_once("inbox")
            st = wf.load_state()["inbox"]
            gap = st["next_run"] - st["last_run"]
            self.assertTrue(30 * 60 <= gap <= 60 * 60, gap)
            gaps.add(round(gap))
        self.assertGreater(len(gaps), 1)  # 不是固定间隔

    def run_once(self, items, final="处理好了", stopped=False):
        ran = []

        class FakeAgent:
            def run(self, task, cb):
                ran.append(task)
                cb.log("action", "读 Lark「A」最近 20 条")
                if stopped:
                    cb.log("status", "已停止")
                return final
        s = wf.Scheduler(lambda: FakeAgent())
        with mock.patch.object(wf, "inbox_snapshot", return_value=items):
            return s.run_once("inbox"), ran

    def test_no_new_items_skips_model(self):
        item = {"app": "Lark", "kind": "message", "chat": "A", "preview": "hi", "time": "", "unread": 0, "tags": [],
                "key": "lark|A|hi"}
        r, ran = self.run_once([item])
        self.assertEqual(r["status"], "done")
        self.assertIn("Lark「A」", ran[0])
        self.assertIn("常驻工作流「消息巡检」", ran[0])
        r, ran = self.run_once([item])          # 同样的情况第二次不再处理
        self.assertEqual((r["status"], ran), ("idle", []))

    def test_preempted_run_is_retried(self):
        item = {"app": "微信", "kind": "friend_request", "name": "B", "message": "你好", "key": "wechat-friend|B|你好"}
        r, _ = self.run_once([item], stopped=True)
        self.assertEqual(r["status"], "stopped")
        r, ran = self.run_once([item])
        self.assertEqual(r["status"], "done")
        self.assertIn("微信好友申请「B」", ran[0])

    def icon_change(self, before="", after="1"):
        import menubar
        icon = lambda t: menubar.Icon("WeChat", (0, 0, 56, 24), t, None)  # noqa: E731
        return menubar.Change("WeChat", icon(before), icon(after))

    def test_icon_change_triggers_inbox_run(self):
        s = wf.Scheduler(lambda: SimpleNamespace(client=None, model="m"))
        wf.save_state({"inbox": {"last_run": time.time()}})  # 定时巡检还没到点
        s.watcher = mock.Mock(poll=mock.Mock(return_value=[self.icon_change()]))
        with mock.patch("menubar.judge", return_value={"new_message": True, "unread": 1, "reason": "未读 1"}):
            s._watch_icons()
        self.assertEqual(s.due(), ["inbox"])
        self.assertEqual(s.triggered[0]["kind"], "wechat_unread")
        self.assertTrue(any(e["kind"] == "trigger" for e in wf.recent_events()))

    def test_icon_change_not_message_is_ignored(self):
        s = wf.Scheduler(lambda: SimpleNamespace(client=None, model="m"))
        wf.save_state({"inbox": {"last_run": time.time()}})
        s.watcher = mock.Mock(poll=mock.Mock(return_value=[self.icon_change("1", "")]))
        with mock.patch("menubar.judge", return_value={"new_message": False, "unread": None, "reason": "看过了"}):
            s._watch_icons()
        self.assertEqual((s.triggered, s.due()), ([], []))

    def test_icon_judge_error_falls_back_to_title(self):
        s = wf.Scheduler(lambda: SimpleNamespace(client=None, model="m"))
        s.watcher = mock.Mock(poll=mock.Mock(return_value=[self.icon_change("", "2")]))
        with mock.patch("menubar.judge", side_effect=RuntimeError("down")):
            s._watch_icons()
        self.assertEqual(len(s.triggered), 1)

    def test_triggered_items_reach_agent_and_requeue_when_stopped(self):
        ran, stop = [], [True]

        class FakeAgent:
            def run(self, task, cb):
                ran.append(task)
                if stop[0]:
                    cb.log("status", "已停止")
                return "好"
        s = wf.Scheduler(lambda: FakeAgent())
        item = {"app": "微信", "kind": "wechat_unread", "unread": 1, "at": time.time(), "key": "wechat-unread|1"}
        with mock.patch.object(wf, "inbox_snapshot", return_value=[]):
            self.assertEqual(s.run_once("inbox", [item])["status"], "stopped")
            self.assertEqual(s.triggered, [item])  # 被打断：放回去下次再处理
            stop[0] = False
            self.assertEqual(s.run_once("inbox", [item])["status"], "done")
        self.assertIn("微信菜单栏图标显示有新消息（未读 1）", ran[-1])
        self.assertNotIn("wechat-unread|1", wf.load_state()["inbox"].get("seen", []))

    def test_periodic_run_waits_for_idle(self):
        s = wf.Scheduler(lambda: self.fail("不该跑"), tick=0.01)
        with mock.patch.object(s, "_watch_icons"), mock.patch("computer.system_dialog", return_value=None), \
                mock.patch.object(wf, "user_idle_seconds", return_value=5), \
                mock.patch.object(s, "run_once", side_effect=AssertionError("不该跑")) as run:
            s.start()
            time.sleep(0.1)
            s.stop()
        run.assert_not_called()

    def test_icon_trigger_runs_even_when_user_busy(self):
        s = wf.Scheduler(lambda: None, tick=0.01)
        wf.save_state({"inbox": {"last_run": time.time()}})
        item = {"app": "微信", "kind": "wechat_unread", "unread": 1, "at": time.time(), "key": "wechat-unread|1"}
        s.triggered = [item]
        with mock.patch.object(s, "_watch_icons"), mock.patch("computer.system_dialog", return_value=None), \
                mock.patch.object(wf, "user_idle_seconds", return_value=5), \
                mock.patch.object(wf, "inbox_snapshot", side_effect=AssertionError("不该先看会话列表")), \
                mock.patch.object(s, "run_once", wraps=lambda wid, extra: {"status": "done"}) as run:
            s.start()
            time.sleep(0.1)
            s.stop()
        run.assert_called_once_with("inbox", [item])
        self.assertEqual(s.triggered, [])

    def test_triggered_run_skips_snapshot(self):
        class FakeAgent:
            def run(self, task, cb):
                return "好"
        s = wf.Scheduler(lambda: FakeAgent())
        item = {"app": "微信", "kind": "wechat_unread", "unread": 1, "at": time.time(), "key": "wechat-unread|1"}
        with mock.patch.object(wf, "inbox_snapshot", side_effect=AssertionError("不该先看会话列表")):
            self.assertEqual(s.run_once("inbox", [item])["status"], "done")

    def test_scheduler_pauses_while_system_dialog(self):
        s = wf.Scheduler(lambda: self.fail("不该跑"), tick=0.01)
        wf.save_state({"inbox": {"next_run": time.time() - 1}})  # 定时巡检已经到点
        with mock.patch.object(s, "_watch_icons"), \
                mock.patch("computer.system_dialog", return_value="UserNotificationCenter"), \
                mock.patch.object(s, "run_once", side_effect=AssertionError("不该跑")) as run:
            s.start()
            time.sleep(0.1)
            s.stop()
        run.assert_not_called()
        self.assertTrue(any("系统弹窗" in e["text"] for e in wf.recent_events()))

    def test_preempt_stops_running_agent(self):
        s = wf.Scheduler(lambda: None)
        a = mock.Mock()
        s.running = {"id": "inbox", "agent": a}
        s.preempt()
        a.stop.assert_called_once()
        self.assertTrue(s.busy.is_set())
        s.release()
        self.assertFalse(s.busy.is_set())


if __name__ == "__main__":
    unittest.main()
