"""微信模块的离线测试：OCR、键鼠和窗口都用桩替换，不会真的操作微信。"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", __import__("tempfile").mkdtemp(prefix="cu-test-"))  # 不写真实目录

import agent  # noqa: E402
import wechat  # noqa: E402

WIN = wechat.Window(1, 0, 0, 1000, 800)
LIST_X = 285
# 假装模型看截图给出的界面元素位置（屏幕 point，x, y, w, h）
LAYOUT = {
    "chats_icon": (18, 110, 24, 24), "contacts_icon": (18, 158, 24, 24),
    "search_box": (60, 10, 190, 26), "plus_button": (261, 16, 24, 24),
    "list_column": (0, 45, LIST_X, 755), "chat_title": (LIST_X, 0, 715, 45),
    "messages": (LIST_X, 45, 715, 565), "input_box": (LIST_X, 610, 715, 190),
}


NAV = ("click", (), 30.0, 122.0)  # open_chat 先点导航栏的「聊天」回到聊天页


def fake_layout(win, *need, page="chat", required=True):
    return {n: LAYOUT[n] for n in need}


def box(text, x, y, w=80, h=16):
    return wechat.Box(text, x, y, w, h, 1.0)


class FakeScreen:
    """按区域返回 OCR 结果：标题条 / 聊天区 / 整个窗口（搜索结果）。"""

    def __init__(self, title="其他人", search=(), chat=()):
        self.title, self.search, self.chat = title, list(search), list(chat)
        self.sessions = []         # 还没搜索时会话列表里显示的文字
        self.searched = False      # 往搜索框里粘贴过之后，列表里显示的是搜索结果
        self.input = ""            # 聊天输入框里的文字
        self.focus = None          # 最后一次点击落在哪里
        self.paste_works = True    # False 模拟微信没读到剪贴板

    def ocr(self, win, region=None, fast=False):
        if region is None or region == LAYOUT["list_column"]:  # 会话列表这一栏（没搜索时是会话，搜索后是结果）
            shown = self.search if self.searched else self.sessions
            return shown if region is None else [b for b in shown if wechat.inside(b, region)]
        if region == LAYOUT["chat_title"] or (region[0] >= LIST_X and region[1] + region[3] <= 60):  # 标题栏 / 标题附近
            return [box(self.title, LIST_X + 20, 20)] if self.title else []
        if region[0] < LIST_X and region[1] < 45:  # 会话列表顶部的搜索框（识别范围比框稍大）
            return [b for b in self.search if b.y < 45]
        if region == LAYOUT["input_box"]:
            return [box(self.input, LIST_X + 40, WIN.h - 80), box("发送", WIN.w - 60, WIN.h - 40)] \
                if self.input else [box("发送", WIN.w - 60, WIN.h - 40)]
        return self.chat


class WeChatTest(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.screen = FakeScreen()
        for target, kw in [("activate", {"return_value": WIN}), ("layout", {"side_effect": fake_layout}),
                           ("ocr", {"side_effect": lambda *a, **k: self.screen.ocr(*a, **k)})]:
            mock.patch.object(wechat, target, **kw).start()
        mock.patch.object(wechat.computer, "execute", side_effect=self._exec).start()
        mock.patch.object(wechat.computer, "_paste_text", side_effect=self._paste).start()
        mock.patch.object(wechat.time, "sleep").start()
        mock.patch.object(wechat, "check_capture").start()
        self.snap = mock.patch.object(wechat, "debug_snapshot").start()  # 不截真屏幕
        mock.patch.object(wechat.computer, "IMAGE_SCALE", 1.0).start()  # 假屏幕按 point 坐标比对点击位置
        self.dialog = mock.patch.object(wechat.computer, "system_dialog", return_value=None).start()  # 不看真屏幕上的系统弹窗
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def _paste(self, text, settle=0.15):
        self.actions.append(("paste", text))
        if self.screen.focus == "input" and self.screen.paste_works:
            self.screen.input += text
        elif self.screen.focus != "input":
            self.screen.searched = True

    def _exec(self, a):
        self.actions.append((a["type"], tuple(a.get("keys") or ()), a.get("x"), a.get("y")))
        if a["type"] == "keypress" and a.get("keys") == ["BACKSPACE"] and self.screen.focus == "input":
            self.screen.input = ""
        if a["type"] == "click":
            ix, iy = wechat._input_point(WIN)
            self.screen.focus = "input" if (a["x"], a["y"]) == (ix, iy) else "other"
        # 点击会话列表 / 搜索结果后，标题变成被点的那个人
        if a["type"] == "click":
            for b in (self.screen.search if self.screen.searched else self.screen.sessions):
                if abs(b.cx - a["x"]) < 1 and abs(b.cy - a["y"]) < 1:
                    self.screen.title = b.text

    def test_open_chat_clicks_exact_result_below_query(self):
        self.screen.search = [box("张三", 100, 40),            # 搜索框里输入的文字
                              box("联系人", 90, 100),           # 分区标题
                              box("张三丰", 100, 120),          # 相近但不完全一样
                              box("张三", 100, 160)]            # 真正的结果
        wechat.open_chat("张三")
        clicks = [a for a in self.actions if a[0] == "click" and a != NAV]
        self.assertEqual(len(clicks), 2)  # 搜索框 + 结果
        self.assertEqual(self.screen.title, "张三")
        self.assertAlmostEqual(clicks[1][3], 168)

    def test_send_retries_with_fresh_layout(self):
        self.screen.title = "张三"
        self.screen.chat = [box("你好呀～", 700, 500)]
        self.screen.paste_works = False  # 第一次粘贴没进去
        orig = wechat.forget_layout

        def forget():
            self.screen.paste_works = True  # 重新看图之后点对了
            orig()
        with mock.patch.object(wechat, "forget_layout", side_effect=forget) as fl:
            r = wechat.send("张三", "你好呀～")
        self.assertEqual(r["status"], "sent", r)
        fl.assert_called_once()
        self.snap.assert_called_once()  # 第一次失败留了现场

    def test_chat_title_skips_time_divider(self):
        with mock.patch.object(wechat, "ocr", return_value=[box("昨天14:27", 500, 60), box("陈宏", 300, 15)]):
            self.assertEqual(wechat.chat_title(WIN).text, "陈宏")
        with mock.patch.object(wechat, "ocr", return_value=[box("昨天14:27", 300, 60)]):
            self.assertIsNone(wechat.chat_title(WIN))  # 只看到时间：当成没认出标题，不会当成聊天对象

    def test_open_chat_clicks_visible_session_without_search(self):
        self.screen.sessions = [box("陈宏", 100, 60), box("16:40", 240, 60), box("在吗", 100, 80),
                                box("陈宏宇", 100, 120)]
        wechat.open_chat("陈宏")
        self.assertEqual(self.screen.title, "陈宏")
        self.assertNotIn("paste", [a[0] for a in self.actions])  # 没有搜索
        clicks = [a for a in self.actions if a[0] == "click" and a != NAV]
        self.assertEqual(len(clicks), 1)
        self.assertAlmostEqual(clicks[0][3], 68)

    def test_open_chat_falls_back_to_search_when_not_in_list(self):
        self.screen.sessions = [box("陈宏宇", 100, 60)]  # 只有名字相近的，不点
        self.screen.search = [box("陈宏", 100, 20), box("联系人", 90, 100), box("陈宏", 100, 160)]
        wechat.open_chat("陈宏")
        self.assertEqual(self.screen.title, "陈宏")
        self.assertIn("paste", [a[0] for a in self.actions])

    def test_open_chat_no_exact_match_does_not_guess(self):
        self.screen.search = [box("张三", 100, 40), box("联系人", 90, 100), box("张三丰", 100, 120)]
        with self.assertRaises(wechat.WeChatError) as ctx:
            wechat.open_chat("张三")
        self.assertIn("张三丰", str(ctx.exception))
        self.assertEqual(len([a for a in self.actions if a[0] == "click" and a != NAV]), 1)  # 只点了搜索框
        self.assertNotIn(("keypress", ("ENTER",), None, None), self.actions)
        self.assertFalse([a for a in self.actions if a[0] == "paste" and a[1] != "张三"])

    def test_open_chat_split_highlighted_name(self):
        # 搜索词被高亮，OCR 把名字切成两段；搜索框里的文字带空格和光标竖线
        self.screen.search = [box("文 件传输助手|", 80, 20), box("功能", 90, 130, w=26),
                              box("文件", 120, 150, w=26), box("传输助手", 148, 150, w=52)]
        orig = self._exec

        def exec_(a):
            orig(a)
            if a["type"] == "click" and 120 <= a["x"] <= 200 and 150 <= a["y"] <= 166:
                self.screen.title = "文件传输助手"
        wechat.computer.execute.side_effect = exec_
        wechat.open_chat("文件传输助手")
        clicks = [a for a in self.actions if a[0] == "click" and a != NAV]
        self.assertEqual(len(clicks), 2)
        self.assertEqual(self.screen.title, "文件传输助手")
        self.assertNotIn(("keypress", ("ENTER",), None, None), self.actions)  # 认出结果就点击，不用回车兜底

    def test_open_chat_text_not_in_search_box_is_cleared(self):
        # 文字没出现在搜索框（可能粘到了聊天输入框）：清空、不回车、报错
        self.screen.search = [box("张三", 400, 750)]
        with self.assertRaises(wechat.WeChatError):
            wechat.open_chat("张三")
        keys = [a[1] for a in self.actions if a[0] == "keypress"]
        self.assertIn(("BACKSPACE",), keys)
        self.assertNotIn(("ENTER",), keys)

    def test_title_split_into_two_boxes(self):
        screen = self.screen

        def ocr(win, region=None, fast=False):
            return [box("文件", LIST_X + 12, 20, w=26), box("传输助手", LIST_X + 40, 20, w=52),
                    box("···", WIN.w - 40, 20, w=20)]
        wechat.ocr.side_effect = ocr
        self.assertEqual(wechat.chat_title(WIN).text, "文件传输助手")
        wechat.ocr.side_effect = lambda *a, **k: screen.ocr(*a, **k)

    def test_ignores_match_in_chat_record_section(self):
        # 「聊天记录」分区里同名的文字不是联系人，点了会打开消息搜索，不能点
        self.screen.search = [box("张三", 100, 40), box("聊天记录", 90, 100), box("张三", 100, 130)]
        with self.assertRaises(wechat.WeChatError) as ctx:
            wechat.open_chat("张三")
        self.assertIn("聊天记录", str(ctx.exception))
        self.assertEqual(len([a for a in self.actions if a[0] == "click" and a != NAV]), 1)  # 只点了搜索框

    def test_ignores_match_in_chat_pane(self):
        # 右侧聊天区里出现的同名文字不是搜索结果
        self.screen.search = [box("张三", 100, 40), box("联系人", 90, 100), box("张三", 600, 300)]
        with self.assertRaises(wechat.WeChatError):
            wechat.open_chat("张三")
        self.assertEqual(len([a for a in self.actions if a[0] == "click" and a != NAV]), 1)

    def test_contact_section_preferred_over_chat_record_above(self):
        self.screen.search = [box("张三", 100, 40), box("聊天记录", 90, 80), box("张三", 100, 100),
                              box("联系人", 90, 140), box("张三", 100, 160)]
        wechat.open_chat("张三")
        clicks = [a for a in self.actions if a[0] == "click" and a != NAV]
        self.assertAlmostEqual(clicks[1][3], 168)  # 点的是联系人分区里的那一个

    def test_open_chat_already_open_skips_search(self):
        self.screen.title = "项目群(12)"
        wechat.open_chat("项目群")
        self.assertEqual(self.actions, [NAV])  # 只点了导航栏的「聊天」回到聊天页

    def test_send_verifies(self):
        self.screen.title = "文件传输助手"
        self.screen.chat = [box("hello world", 700, 500)]
        r = wechat.send("文件传输助手", "hello world")
        self.assertEqual(r["status"], "sent")
        self.assertIn(("paste", "hello world"), self.actions)
        self.assertIn(("keypress", ("ENTER",), None, None), self.actions)

    def test_send_clears_draft_first(self):
        self.screen.title = "文件传输助手"
        self.screen.input = "旧草稿"
        self.screen.chat = [box("hello", 700, 500)]
        r = wechat.send("文件传输助手", "hello")
        self.assertEqual(r["status"], "sent")
        self.assertEqual(self.screen.input, "hello")  # 回车前输入框里只有这条

    def test_send_paste_failed_no_enter(self):
        self.screen.title = "文件传输助手"
        self.screen.paste_works = False
        with self.assertRaises(wechat.WeChatError):
            wechat.send("文件传输助手", "hello")
        self.assertNotIn(("keypress", ("ENTER",), None, None), self.actions)
        self.assertEqual(len([a for a in self.actions if a[0] == "paste"]), 2)  # 重试过一次

    def test_lock_busy(self):
        import fcntl
        with open(os.path.join(wechat.state_dir(), "wechat.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with mock.patch.object(wechat.time, "time", side_effect=[0, 0, 100]):
                with self.assertRaises(wechat.WeChatError):
                    with wechat.gui_lock(timeout=1):
                        pass

    def test_send_aborts_if_chat_changed_before_enter(self):
        self.screen.title = "文件传输助手"
        orig = self._paste

        def paste(text, settle=0.15):
            orig(text, settle)
            self.screen.title = "老板"  # 输入期间聊天被切走
        wechat.computer._paste_text.side_effect = paste
        with self.assertRaises(wechat.WeChatError):
            wechat.send("文件传输助手", "hi")
        self.assertNotIn(("keypress", ("ENTER",), None, None), self.actions)

    def test_read_groups_by_side(self):
        self.screen.title = "张三"
        mid = LIST_X + (WIN.w - LIST_X) / 2
        self.screen.chat = [box("下午3:05", mid - 30, 100),
                            box("在吗", LIST_X + 60, 130),
                            box("晚上一起吃饭", LIST_X + 60, 150),
                            box("好的", WIN.w - 120, 200)]
        r = wechat.read("张三")
        self.assertEqual([m["from"] for m in r["messages"]], ["时间", "对方", "我"])
        self.assertEqual(r["messages"][1]["text"], "在吗\n晚上一起吃饭")

    def test_capture_check_blank_screen(self):
        mock.patch.stopall()
        mock.patch.object(wechat, "ocr", return_value=[]).start()
        mock.patch.object(wechat, "version", return_value=(4, 1, 61)).start()
        wechat._capture_ok_until = 0
        with self.assertRaises(wechat.WeChatError) as ctx:
            wechat.check_capture(WIN)
        self.assertIn("4.1.60", str(ctx.exception))

    def test_norm(self):
        self.assertEqual(wechat.norm("项目 群（23）"), "项目群")


class AgentFunctionCallTest(unittest.TestCase):
    def test_function_call_round_trip(self):
        a = agent.Agent(api_key="sk-test")
        responses = [
            SimpleNamespace(output=[SimpleNamespace(type="function_call", name="wechat_send", call_id="c1",
                                                    arguments=json.dumps({"contact": "文件传输助手", "text": "hi"}))]),
            SimpleNamespace(output=[SimpleNamespace(type="message", content=[SimpleNamespace(text="已发送")])]),
        ]
        sent = []

        def create(**kw):
            sent.append(kw["input"])
            return responses.pop(0)
        a.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        for r in responses:
            for item in r.output:
                item.model_dump = lambda exclude_none=True, i=item: {"type": i.type}
        a.history = [{"role": "user", "content": "之前的对话"}]  # 非新对话，不截图
        with mock.patch.object(wechat, "call_tool", return_value={"status": "sent", "detail": "ok"}) as call, \
                mock.patch.dict(os.environ, {"JEV_REVIEW": "off"}):
            out = a.run("给文件传输助手发 hi", agent.AgentCallbacks(log=lambda k, t: None))
        call.assert_called_once()
        self.assertEqual(out, "已发送")
        fco = [i for i in sent[1] if isinstance(i, dict) and i.get("type") == "function_call_output"]
        self.assertEqual(fco[0]["call_id"], "c1")
        self.assertIn("wechat_send", [t.get("name") for t in a.tools])


if __name__ == "__main__":
    unittest.main()
