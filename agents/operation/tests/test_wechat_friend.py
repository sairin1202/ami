"""微信加好友 / 好友申请的离线测试：OCR、窗口和键鼠都用桩，不会操作微信。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import tools  # noqa: E402
import wechat  # noqa: E402
import wechat_friend as wf  # noqa: E402
from ocr import Box  # noqa: E402


def box(text, x, y, w=60, h=16):
    return Box(text, x, y, w, h, 1.0)


# 假装模型看截图给出的位置（屏幕 point，x, y, w, h）
MAIN_LAYOUT = {"chats_icon": (18, 110, 24, 24), "contacts_icon": (18, 158, 24, 24), "plus_button": (261, 16, 24, 24),
               "list_column": (60, 0, 240, 640), "right_pane": (300, 0, 580, 640)}


def fake_layout(win, *need, page="chat", required=True):
    return {n: MAIN_LAYOUT[n] for n in need}


def fake_locate(frame, elements, what="", required=True):
    x, y, w, h = frame
    at = {"close": (x + 8, y + 10, 20, 20), "search_box": (x + 44, y + 56, 200, 24),
          "field": (x + 20, y + 90, w - 40, 30)}
    return {n: at[n] for n in elements}


class FakeWeChat:
    """主窗口在 (0,0,880,640)。按状态返回窗口标题和 OCR 结果，记录点击。"""

    MAIN = (0, 0, 880, 640)
    ADD = (260, 15, 360, 560)
    VERIFY = (260, 0, 360, 640)

    def __init__(self, result="found", verify_title=wf.ACCEPT_WINDOW, requests=(("陈宏", "我是陈宏", "等待验证"),)):
        self.result, self.verify_title = result, verify_title
        self.requests = [list(r) for r in requests]
        self.open = {"微信": self.MAIN}
        self.typed, self.clicks, self.detail = "", [], None
        self.menu = False

    def windows(self):
        return dict(self.open)

    def read(self, frame):
        x, y = frame[0], frame[1]
        if frame == self.MAIN:  # 「+」弹出的菜单
            return [box("2 添加朋友", 232, 94, 80)] if self.menu else []
        if frame == self.ADD:
            out = [box("添加朋友", x + 150, y + 11), box("Q " + self.typed, x + 34, y + 62, 120), box("搜索", x + 288, y + 62, 30)]
            if self.typed and self.result == "found":
                out += [box("陈宏2", x + 122, y + 130), box("添加到通讯录", x + 136, y + 270, 86)]
            elif self.typed and self.result == "friend":
                out += [box("陈宏", x + 122, y + 130), box("发消息", x + 150, y + 270, 50)]
            elif self.typed and self.result == "not_found":
                out += [box("无法找到该用户，请检查你填写的账号是否正确。", x + 48, y + 306, 260)]
            return out
        if frame in (self.VERIFY,):
            out = [box(self.verify_title, x + 136, y + 18, 86), box("备注", x + 34, y + 66, 26),
                   box("取消", x + 96, y + 594, 30), box("确定", x + 222, y + 594, 60)]
            if self.verify_title == wf.REQUEST_WINDOW:
                out.insert(1, box("发送添加朋友申请", x + 34, y + 50, 120))
            return out
        if frame[0] == 60:  # 通讯录列表
            out = [box("V 新的朋友", 72, 116, 80)]
            for i, (name, msg, st) in enumerate(self.requests):
                yy = 162 + 70 * i
                out += [box("•" + name, 122, yy, 40, 14), box(st, 248, yy, 42, 12), box(msg, 134, yy + 24, 48, 12)]
            out.append(box("联系人", 90, 162 + 70 * len(self.requests) + 20, 48))
            return out
        if frame[0] == 300:  # 申请详情
            name, msg, _ = self.detail
            return [box(name + "＆", 506, 96, 46), box(f"{name}：{msg}", 438, 178, 82),
                    box("通过搜索微信号添加", 504, 266, 128), box("前往验证", 558, 328, 58)]
        return []

    def execute(self, a):
        if a["type"] == "click":
            x, y = a["x"], a["y"]
            self.clicks.append((x, y))
            if (x, y) == (273, 28):  # 「+」
                self.menu = True
            elif self.menu and 232 <= x <= 312 and 90 <= y <= 112:
                self.menu, self.open[wf.ADD_WINDOW] = False, self.ADD
            elif wf.ADD_WINDOW in self.open and abs(x - (self.ADD[0] + 179)) < 50 and abs(y - (self.ADD[1] + 278)) < 10:
                self.open[self.verify_title] = self.VERIFY
            elif self.verify_title in self.open and abs(y - (self.VERIFY[1] + 602)) < 10:
                del self.open[self.verify_title]
                if x > self.VERIFY[0] + 200:  # 确定
                    self.submitted = True
                    for r in self.requests:
                        r[2] = "已添加"
            elif wf.ADD_WINDOW in self.open and (x, y) == (self.ADD[0] + 18, self.ADD[1] + 20):
                del self.open[wf.ADD_WINDOW]
            elif 60 < x < 300 and 150 < y < 400 and self.requests:
                i = int((y - 162) // 70)
                self.detail = self.requests[max(0, min(i, len(self.requests) - 1))]
            elif abs(x - 587) < 30 and abs(y - 336) < 10:
                self.open[wf.ACCEPT_WINDOW] = self.VERIFY
                self.verify_title = wf.ACCEPT_WINDOW

    def paste(self, text, settle=0.15):
        if wf.ADD_WINDOW in self.open and self.verify_title not in self.open:
            self.typed = text


class FriendTest(unittest.TestCase):
    def setUp(self):
        self.ui = FakeWeChat()
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        mock.patch.object(wf, "windows", side_effect=lambda: self.ui.windows()).start()
        mock.patch.object(wf, "read", side_effect=lambda frame: self.ui.read(tuple(frame))).start()
        mock.patch.object(wf.computer, "execute", side_effect=lambda a: self.ui.execute(a)).start()
        mock.patch.object(wf.computer, "_paste_text", side_effect=lambda t, settle=0.15: self.ui.paste(t)).start()
        mock.patch.object(wf.computer, "IMAGE_SCALE", 1.0).start()
        self.dialog = mock.patch.object(wf.computer, "system_dialog", return_value=None).start()  # 不看真屏幕上的系统弹窗
        mock.patch.object(wf.time, "sleep").start()
        mock.patch.object(wechat, "activate", return_value=wechat.Window(1, 0, 0, 880, 640)).start()
        mock.patch.object(wechat, "check_capture").start()
        mock.patch.object(wechat, "debug_snapshot").start()  # 不截真屏幕
        # 通讯录图标的位置由模型看截图给出，这里假装模型找到了它（假界面里点 (30, 170) 会切到通讯录）
        mock.patch.object(wechat, "layout", side_effect=fake_layout).start()
        self.locate = mock.patch.object(wechat, "locate", side_effect=fake_locate).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def test_clean_name(self):
        self.assertEqual([wf.clean_name(s) for s in ("•陈宏", "陈宏2", "陈宏＆", "陈宏』", "Tom2")],
                         ["陈宏", "陈宏", "陈宏", "陈宏", "Tom2"])

    def test_add_friend_accept_variant(self):
        r = wf.add_friend("Sairin1202", "", "")
        self.assertEqual(r["status"], "added", r)
        self.assertIn("已同意「陈宏」", r["detail"])
        self.assertTrue(self.ui.submitted)
        self.assertEqual(self.ui.open, {"微信": FakeWeChat.MAIN})  # 搜索窗口也关掉了

    def test_add_friend_request_variant_sent(self):
        self.ui.verify_title = wf.REQUEST_WINDOW
        r = wf.add_friend("Sairin1202", "", "")
        self.assertEqual(r["status"], "requested", r)
        self.assertTrue(self.ui.submitted)

    def test_not_found_and_already_friend(self):
        self.ui.result = "not_found"
        self.assertEqual(wf.add_friend("nobody", "", "")["status"], "not_found")
        self.ui = FakeWeChat(result="friend")
        r = wf.add_friend("Sairin1202", "", "")
        self.assertEqual(r["status"], "already_friend")
        self.assertFalse(getattr(self.ui, "submitted", False))

    def test_list_and_accept_request(self):
        self.ui.requests = [["张三", "广告推广加我", "已添加"], ["陈宏", "我是陈宏", "等待验证"]]
        rows = wf.list_requests()["requests"]
        self.assertEqual(rows, [{"name": "张三", "message": "广告推广加我", "status": "已添加"},
                                {"name": "陈宏", "message": "我是陈宏", "status": "等待验证"}])
        r = wf.accept_request("陈宏", "")
        self.assertEqual(r["status"], "added", r)

    def test_element_not_found_stops(self):
        with mock.patch.object(wechat, "layout", side_effect=wechat.WeChatError("在微信截图里没找到：通讯录图标")):
            r = wf.call_tool("wechat_friend_requests", {})
        self.assertEqual(r["status"], "error")
        self.assertIn("没找到", r["detail"])
        self.assertEqual(self.ui.clicks, [])  # 没找到就不点

    def test_accept_unknown_or_done(self):
        self.assertEqual(wf.accept_request("李四", "")["status"], "not_found")
        self.ui.requests = [["陈宏", "我是陈宏", "已添加"]]
        self.assertEqual(wf.accept_request("陈宏", "")["status"], "already_done")

    def test_security_block_stops_before_and_after(self):
        notice = "当前账号存在安全风险，需先到「微信团队」进行安全验证后才能继续使用当前功能。"
        self.ui.requests = [["陈宏", "我是陈宏", "等待验证"]]
        orig_execute = self.ui.execute

        def execute(a):  # 点「确定」之后弹出风控提示
            orig_execute(a)
            if getattr(self.ui, "submitted", False):
                self.ui.open[""] = (300, 250, 280, 190)
        self.ui.execute = execute
        orig_read = self.ui.read
        self.ui.read = lambda frame: [box(notice, 330, 270, 220), box("确定", 420, 360, 30)] \
            if frame == (300, 250, 280, 190) else orig_read(frame)
        r = wf.call_tool("wechat_accept_friend", {"name": "陈宏", "remark": ""})
        self.assertEqual(r["status"], "blocked")
        self.assertIn("安全风险", r["detail"])
        self.assertIn("本人", r["detail"])
        # 弹窗还在：下一次直接停，不点任何东西
        clicks = len(self.ui.clicks)
        r = wf.call_tool("wechat_friend_requests", {})
        self.assertEqual(r["status"], "blocked")
        self.assertEqual(len(self.ui.clicks), clicks)

    def test_system_dialog_stops_before_anything(self):
        self.dialog.return_value = "SecurityAgent"
        r = wf.call_tool("wechat_friend_requests", {})
        self.assertEqual(r["status"], "error")
        self.assertIn("系统弹窗", r["detail"])
        self.assertEqual(self.ui.clicks, [])

    def test_old_security_message_in_chat_is_ignored(self):
        orig_read = self.ui.read
        self.ui.read = lambda frame: (orig_read(frame) + [box("当前账号存在安全风险，部分功能暂时无法正常使用", 380, 112, 330)]) \
            if frame == FakeWeChat.MAIN else orig_read(frame)
        self.assertIsNone(wf.security_notice())

    def test_registered(self):
        names = {t["name"] for t in wf.TOOLS}
        self.assertEqual(names, {"wechat_add_friend", "wechat_friend_requests", "wechat_accept_friend"})
        self.assertTrue({"wechat_add_friend", "wechat_accept_friend"} <= tools.write_tools())
        self.assertIn(wf, tools.PROVIDERS)
        self.assertTrue(wf.JEV_SKIP.search("有新的好友申请就帮我加"))


if __name__ == "__main__":
    unittest.main()
