"""微信被锁定 / 弹安全提示后暂停所有微信自动操作：之后不再碰微信界面，直到用户 /wechat on 恢复。不碰屏幕。"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import menubar  # noqa: E402
import wechat  # noqa: E402
import wechat_friend  # noqa: E402
import workflows as wf  # noqa: E402

WIN = wechat.Window(1, 0, 0, 1200, 800)


def box(text):
    return wechat.Box(text, 500, 400, 120, 16, 1.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        mock.patch.object(wf, "QUIET_HOURS", "").start()
        mock.patch.object(wechat, "_guard_ok_until", 0.0).start()
        mock.patch.object(wechat.computer, "minimize_windows").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)


class PauseTest(Base):
    def test_locked_window_pauses(self):
        with mock.patch.object(wechat, "ocr", return_value=[box("Mac 微信已被锁定"), box("解锁以继续使用")]):
            with self.assertRaises(wechat.WeChatPaused) as e:
                wechat.check_locked(WIN)
        self.assertIn("已被锁定", wechat.paused()["reason"])
        self.assertIn("/wechat on", str(e.exception))

    def test_normal_window_not_paused(self):
        with mock.patch.object(wechat, "ocr", return_value=[box("搜索"), box("文件传输助手")]):
            wechat.check_locked(WIN)
        self.assertIsNone(wechat.paused())

    def test_paused_activate_does_not_touch_wechat(self):
        wechat.pause("测试")
        with mock.patch.object(wechat, "_app", side_effect=AssertionError("不该碰微信")):
            with self.assertRaises(wechat.WeChatPaused):
                wechat.activate()
            r = wechat.call_tool("wechat_read", {"contact": "陈宏"})
        self.assertEqual(r["status"], "error")
        self.assertIn("已暂停", r["detail"])

    def test_resume(self):
        self.assertFalse(wechat.resume())
        wechat.pause("测试")
        self.assertTrue(wechat.resume())
        self.assertIsNone(wechat.paused())

    def test_security_window_pauses(self):
        wins = [{"kCGWindowOwnerPID": 7, "kCGWindowLayer": 0, "kCGWindowNumber": n, "kCGWindowName": t,
                 "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300}}
                for n, t in ((1, "微信"), (2, "添加朋友"), (3, ""))]
        texts = {1: "当前账号存在安全风险", 2: "暂时无法使用", 3: "当前账号存在安全风险，需先进行安全验证"}
        seen = []

        def fake_ocr(win, region=None, fast=False):
            seen.append(region)
            return [box(texts[3])]
        with mock.patch.object(wechat, "_app", return_value=mock.Mock(processIdentifier=lambda: 7)), \
                mock.patch.object(wechat.Quartz, "CGWindowListCopyWindowInfo", return_value=wins), \
                mock.patch.object(wechat, "ocr", side_effect=fake_ocr):
            wechat.OWN_WINDOWS.add("添加朋友")
            with self.assertRaises(wechat.WeChatPaused):
                wechat.check_security(WIN)
        self.assertEqual(len(seen), 1)  # 主窗口和本程序自己的小窗口不看
        self.assertIn("安全提示", wechat.paused()["reason"])
        wechat.computer.minimize_windows.assert_called_with(7)

    def test_check_window_cached(self):
        with mock.patch.object(wechat, "check_locked") as lk, mock.patch.object(wechat, "check_security") as sec:
            wechat.check_window(WIN)
            wechat.check_window(WIN)
            self.assertEqual((lk.call_count, sec.call_count), (1, 1))
            wechat.check_window(WIN, force=True)
            self.assertEqual(lk.call_count, 2)

    def test_contacts_badge_asks_model(self):
        client = mock.Mock()
        client.responses.create.return_value = mock.Mock(output_text='{"badge": true, "reason": "通讯录图标上有红点 1"}')
        with mock.patch.object(wechat, "activate", return_value=WIN), \
                mock.patch.object(wechat, "window_png", return_value=(b"png", (600, 400))):
            self.assertEqual(wechat_friend.contacts_badge(client, "m"), {"badge": True, "reason": "通讯录图标上有红点 1"})
            content = client.responses.create.call_args.kwargs["input"][0]["content"]
            self.assertIn("600x400", content[0]["text"])
            self.assertEqual(content[1]["type"], "input_image")
            for bad in ("看不清", '{"badge": "yes"}'):  # 说不清就当没有，不去点「新的朋友」
                client.responses.create.return_value = mock.Mock(output_text=bad)
                self.assertFalse(wechat_friend.contacts_badge(client, "m")["badge"])

    def test_layout_asks_model_once_and_caches(self):
        client = mock.Mock()
        wechat.set_vision(client, "m")
        wechat.forget_layout()
        client.responses.create.return_value = mock.Mock(output_text=json.dumps({
            "search_box": [30, 5, 125, 18], "chat_title": [150, 0, 600, 22], "input_box": None,
            "messages": [1, 2, 3], "list_column": [0, 25, 140, 400]}))
        with mock.patch.object(wechat, "window_png", return_value=(b"png", (600, 400))):  # 窗口 1200x800 缩成一半
            r = wechat.layout(WIN, "search_box", "chat_title")
            self.assertEqual(r, {"search_box": (60.0, 10.0, 190.0, 26.0), "chat_title": (300.0, 0.0, 900.0, 44.0)})
            wechat.layout(WIN, "list_column")  # 缓存里有，不再问
            self.assertEqual(client.responses.create.call_count, 1)
            self.assertIn("搜索框", client.responses.create.call_args.kwargs["input"][0]["content"][0]["text"])
            # 没给出来的 / 格式不对的：再看一次，还是没有就报错，不猜
            with self.assertRaises(wechat.WeChatError) as e:
                wechat.layout(WIN, "input_box")
            self.assertIn("输入区", str(e.exception))
            self.assertEqual(wechat.layout(WIN, "messages", required=False), {})
            # 窗口挪了：重新看
            wechat.layout(wechat.Window(1, 50, 0, 1200, 800), "search_box")
            self.assertEqual(client.responses.create.call_count, 4)  # 初看 + 输入区 + 消息区 + 挪窗口
        wechat.forget_layout()

    def test_layout_reask_keeps_known_boxes(self):
        client = mock.Mock()
        wechat.set_vision(client, "m")
        wechat.forget_layout()
        first = {"chat_title": [150, 0, 600, 22], "input_box": None}
        second = {"chat_title": [150, 30, 600, 50], "input_box": [150, 300, 600, 400]}  # 第二次把标题框歪了
        client.responses.create.side_effect = [mock.Mock(output_text=json.dumps(first)),
                                               mock.Mock(output_text=json.dumps(second))]
        with mock.patch.object(wechat, "window_png", return_value=(b"png", (600, 400))):
            t1 = wechat.layout(WIN, "chat_title")["chat_title"]
            r = wechat.layout(WIN, "input_box", "chat_title")  # 缺输入区：再问一次，只补上输入区
        self.assertEqual(r["chat_title"], t1)
        self.assertEqual(r["input_box"], (300.0, 600.0, 900.0, 200.0))
        wechat.forget_layout()

    def test_wechat_open_tool(self):
        with mock.patch.object(wechat, "activate") as act:
            self.assertEqual(wechat.call_tool("wechat_open", {})["status"], "ok")
        act.assert_called_once()
        wechat.pause("测试")
        r = wechat.call_tool("wechat_open", {})
        self.assertEqual(r["status"], "error")
        self.assertIn("已暂停", r["detail"])

    def test_security_notice_pauses(self):
        with mock.patch.object(wechat, "activate"), \
                mock.patch.object(wechat_friend, "windows", return_value={"安全提示": (0, 0, 300, 200)}), \
                mock.patch.object(wechat_friend, "read", return_value=[box("当前账号存在安全风险，需先进行安全验证")]):
            self.assertTrue(wechat_friend.security_notice())
        self.assertIn("安全提示", wechat.paused()["reason"])


class AgentGuardTest(Base):
    def setUp(self):
        super().setUp()
        self.execute = mock.patch.object(agent.computer, "execute").start()
        self.front = mock.patch.object(agent.computer, "frontmost_app", return_value=(1, "com.apple.Terminal", "终端")).start()

    def test_spotlight_wechat_blocked(self):
        st = {}
        self.assertIsNone(agent.wechat_guard({"type": "keypress", "keys": ["CMD", "SPACE"]}, st))
        self.assertIsNone(agent.wechat_guard({"type": "wait"}, st))
        self.assertIn("Spotlight", agent.wechat_guard({"type": "type", "text": "微信"}, st))
        self.execute.assert_called_with({"type": "keypress", "keys": ["ESC"]})

    def test_spotlight_other_app_allowed(self):
        st = {}
        agent.wechat_guard({"type": "keypress", "keys": ["CMD", "SPACE"]}, st)
        self.assertIsNone(agent.wechat_guard({"type": "type", "text": "备忘录"}, st))
        self.assertIsNone(agent.wechat_guard({"type": "type", "text": "微信"}, st))  # 不是紧跟在 ⌘空格 后面
        self.execute.assert_not_called()

    def test_frontmost_wechat_checked(self):
        self.front.return_value = (7, wechat.BUNDLE_ID, "微信")
        with mock.patch.object(wechat, "main_window", return_value=WIN), \
                mock.patch.object(wechat, "check_window") as chk:
            self.assertIsNone(agent.wechat_guard({"type": "click", "x": 1, "y": 1}, {}))
        chk.assert_called_once_with(WIN)
        wechat.pause("Mac 微信已被锁定")
        self.assertIn("已暂停", agent.wechat_guard({"type": "click", "x": 1, "y": 1}, {}))
        self.assertIsNone(agent.wechat_guard({"type": "screenshot"}, {}))  # 截图不拦


class WorkflowPauseTest(Base):
    def test_snapshot_skips_wechat_when_paused(self):
        wechat.pause("测试")
        with mock.patch("lark.available", return_value=False), \
                mock.patch.object(wechat_friend, "available", return_value=True), \
                mock.patch.object(wechat_friend, "list_requests", side_effect=AssertionError("不该碰微信")), \
                mock.patch.object(wechat_friend, "contacts_badge", side_effect=AssertionError("不该碰微信")):
            self.assertEqual(wf.inbox_snapshot(judge_model=(None, "m")), [])

    def test_snapshot_opens_new_friends_only_with_red_dot(self):
        req = {"name": "张三", "message": "我是张三", "status": "等待验证"}
        for dot, expect in ((False, []), (True, ["张三"])):
            with mock.patch("lark.available", return_value=False), \
                    mock.patch.object(wechat_friend, "available", return_value=True), \
                    mock.patch.object(wechat_friend, "contacts_badge", return_value={"badge": dot, "reason": "", "point": (30, 170)}), \
                    mock.patch.object(wechat_friend, "list_requests", return_value={"requests": [req]}) as lr:
                self.assertEqual([i["name"] for i in wf.inbox_snapshot(judge_model=(None, "m"))], expect)
            self.assertEqual(lr.called, dot)

    def test_quiet_hours_defer_everything(self):
        s = wf.Scheduler(lambda: None)
        wf.save_state({"inbox": {"next_run": time.time() - 1}})
        s.triggered = [{"kind": "wechat_unread"}]
        night = time.mktime((2026, 10, 13, 2, 0, 0, 0, 0, -1))
        noon = time.mktime((2026, 10, 13, 12, 0, 0, 0, 0, -1))
        with mock.patch.object(wf, "QUIET_HOURS", "23-8"):
            self.assertEqual(s.due(night), [])
            self.assertEqual(s.due(noon), ["inbox"])
        self.assertEqual(len(s.triggered), 1)  # 夜里攒着，早上再处理
        for h, q in ((22, False), (23, True), (0, True), (7, True), (8, False)):
            with mock.patch.object(wf, "QUIET_HOURS", "23-8"):
                self.assertEqual(wf.quiet_now(time.mktime((2026, 10, 13, h, 30, 0, 0, 0, -1))), q, h)

    def test_icon_trigger_ignored_when_paused(self):
        wechat.pause("测试")
        s = wf.Scheduler(lambda: mock.Mock(client=None, model="m"))
        icon = lambda t: menubar.Icon("WeChat", (0, 0, 56, 24), t, None)  # noqa: E731
        s.watcher = mock.Mock(poll=mock.Mock(return_value=[menubar.Change("WeChat", icon(""), icon("1"))]))
        with mock.patch("menubar.judge", return_value={"new_message": True, "unread": 1, "reason": "未读 1"}):
            s._watch_icons()
        self.assertEqual(s.triggered, [])

    def test_lark_icon_triggers_lark_run(self):
        wechat.pause("测试")  # 微信暂停不影响 Lark
        s = wf.Scheduler(lambda: mock.Mock(client=None, model="m"))
        icon = lambda t: menubar.Icon("Lark Helper", (0, 0, 51, 24), t, None)  # noqa: E731
        s.watcher = mock.Mock(poll=mock.Mock(return_value=[menubar.Change("Lark Helper", icon("2"), icon("3"))]))
        with mock.patch("menubar.judge", return_value={"new_message": True, "unread": 3, "reason": "未读 3"}):
            s._watch_icons()
        self.assertEqual([(t["app"], t["kind"], t["unread"]) for t in s.triggered], [("Lark", "lark_unread", 3)])
        task = wf.inbox_task(wf.load_goals()["workflows"]["inbox"], s.triggered)
        self.assertIn("Lark 菜单栏图标显示有新消息（未读 3）", task)
        self.assertIn("lark_chats", task)
        self.assertNotIn("好友申请「", task)
        self.assertIn("只处理上面列出的这些新情况", task)

    def test_task_tells_model_to_leave_wechat_alone(self):
        wechat.pause("Mac 微信已被锁定")
        item = {"app": "Lark", "kind": "message", "chat": "Hong Chen", "preview": "hi", "time": "", "unread": 1,
                "tags": [], "key": "k"}
        task = wf.inbox_task(wf.load_goals()["workflows"]["inbox"], [item])
        self.assertIn("微信自动操作已暂停", task)
        self.assertIn("不要用任何方式打开或操作微信", task)


if __name__ == "__main__":
    unittest.main()
