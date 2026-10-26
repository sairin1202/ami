"""Lark 客户端模块的离线测试：辅助功能树用假的 DOM 节点代替，键鼠用桩替换，不会真的操作 Lark。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", __import__("tempfile").mkdtemp(prefix="cu-test-"))  # 不写真实目录

import jev  # noqa: E402
import lark  # noqa: E402
import tools  # noqa: E402
from lark import Node  # noqa: E402


def make_meme_dir(names=("收到收到", "包的", "好滴", "辛苦啦", "救命")) -> str:
    """测试用的梗图目录：几张纯色小图，用途写在 memes.json 里。"""
    from PIL import Image
    d = tempfile.mkdtemp(prefix="cu-memes-")
    for n in names:
        Image.new("RGB", (8, 8), (255, 200, 200)).save(os.path.join(d, f"{n}.png"))
    with open(os.path.join(d, "memes.json"), "w", encoding="utf-8") as f:
        json.dump({n: f"{n}的时候用" for n in names}, f, ensure_ascii=False)
    return d


def T(text, frame=(0, 0, 50, 16)):
    return Node("AXStaticText", text=text, frame=frame)


def G(classes="", *children, frame=(0, 0, 100, 20), role="AXGroup", text="", focused=False):
    return Node(role, classes=tuple(classes.split(".")) if classes else (), text=text, frame=frame,
                children=list(children), focused=focused)


class FakeLark:
    """按当前状态生成 messenger / messenger-chat / search-command-bar 三个网页区域；点击按坐标找到被点的东西。"""

    FEED = (476, 106, 448, 738)

    def __init__(self):
        self.title = "其他人"
        self.feed = ["Hong Chen", "Nevamind AI"]
        self.feed_mine = set()            # 最后一条是自己发的会话
        self.results = []                 # [(名字的几段文字, "user" | "group")]
        self.search_open = False
        self.search_text = ""
        self.input = ""
        self.input_image = False          # 输入框里粘了一张图
        self.focus = None                 # "input" | "search" | None
        self.msgs = [("other", "hi")]     # [("self" | "other", 文字)]
        self.enter_sends = True
        self.clicks = {}

    def _clickable(self, node, action):
        self.clicks[node.center] = action
        return node

    def feed_area(self):
        cards = []
        for i, name in enumerate(self.feed):
            y = 160 + 62 * i
            name_node = self._clickable(T(name, (540, y, 80, 17)), lambda n=name: self._open(n))
            status = [Node("AXImage")] if name in self.feed_mine else []  # 自己发的：预览前有已读 / 未读小圆圈
            cards.append(G("a11y_feed_card_item._1e0a457", G("_9f5c521", name_node), G("_97ba617", T("昨天")),
                           *status, T(f"预览{i}"), G("a11y_feed_item_done", Node("AXImage")),
                           frame=(484, y - 6, 432, 60)))
        return G("", G("scroller.a11y_feed_main_list", *cards), role="AXWebArea", text="messenger", frame=self.FEED)

    def chat_area(self):
        items = [G("divider.date-divider", T("昨天"))]
        for who, text in self.msgs:
            cls = "js-message-item.message-item." + ("message-self" if who == "self" else "message-not-self")
            status = G("read-status-bar", Node("AXImage"))  # 「已读」图标也是图片，不能当成图片消息
            if text is None:  # 图片消息：没有 message-content
                body = G("message-image", Node("AXImage", classes=("base-image__content",)))
                items.append(G("messageList-row-wrapper", G(cls + ".im-image-message", G("message-right", body, status))))
            else:
                items.append(G("messageList-row-wrapper",
                               G(cls, G("message-right", G("message-content", T(text)), status))))
        shown = self.input or ("" if self.input_image else f"发送给 {self.title}")
        img = [G("image-item", Node("AXImage", classes=("editor-image",)))] if self.input_image else []
        box = self._clickable(
            G("zone-container.innerdocbody", T(shown), *img, role="AXTextArea",
              text=shown + "\n\u200b", frame=(963, 753, 399, 22),
              focused=self.focus == "input"),
            lambda: setattr(self, "focus", "input"))
        return G("", G("chatContainer.p2pChat",
                       G("chatWindow_chatName", T(self.title)),
                       G("list_items", *items),
                       box), role="AXWebArea", text="messenger-chat")

    def search_area(self):
        if not self.search_open:
            return None
        cards = []
        for i, (parts, kind) in enumerate(self.results):
            y = 300 + 52 * i
            name = "".join(parts)
            texts = [T(p, (481 + 40 * j, y, 40, 17)) for j, p in enumerate(parts)]
            name_node = self._clickable(G("user-chatter-info-name" if kind == "user" else "chat-text-title", *texts,
                                          frame=(481, y, 80, 24)), lambda n=name: self._open(n))
            cards.append(G("user-result-card" if kind == "user" else "group-chat-card", name_node))
        field = G("zone-container.editor-kit-container", T(self.search_text), role="AXTextArea",
                  text=self.search_text + "\u200b", focused=self.focus == "search")
        return G("", G("serp-search-bar", field), G("search-result-container", *cards),
                 role="AXWebArea", text="search-command-bar")

    def _open(self, name):
        self.title = name
        self.search_open = False
        self.search_text = ""
        self.focus = None
        self.input = ""

    # --- 桩 ---
    def area(self, pid, name, timeout=0.0):
        self.clicks.clear()
        self.feed_area(), self.chat_area(), self.search_area()  # 刷新可点击位置
        return {"messenger": self.feed_area, "messenger-chat": self.chat_area,
                "search-command-bar": self.search_area}[name]()

    def native(self, pid, pred):
        return Node("AXButton", text="搜索（⌘＋K）", frame=(304, 190, 164, 32))

    def execute(self, a):
        if a["type"] == "click":
            self.area(1, "messenger")
            act = self.clicks.get((a["x"], a["y"]))
            if (a["x"], a["y"]) == (386, 206):  # 侧边栏的搜索按钮
                self.search_open, self.focus = True, "search"
            elif act:
                act()
        elif a["type"] == "keypress":
            keys = a["keys"]
            if keys == ["BACKSPACE"]:
                if self.focus == "input":
                    self.input, self.input_image = "", False
                elif self.focus == "search":
                    self.search_text = ""
            elif keys == ["ESC"]:
                self.search_open = False
            elif keys == ["ENTER"] and self.focus == "input" and self.input_image and self.enter_sends:
                self.msgs.append(("self", None))
                self.input_image = False
            elif keys == ["ENTER"] and self.focus == "input" and self.input and self.enter_sends:
                self.msgs.append(("self", self.input))
                self.input = ""

    def paste_image(self, path, settle=0.8):
        if self.focus == "input":
            self.input_image = True

    def paste(self, text, settle=0.15):
        if self.focus == "input":
            self.input += text
        elif self.focus == "search":
            self.search_text += text


class LarkTest(unittest.TestCase):
    def setUp(self):
        self.ui = FakeLark()
        self.actions = []

        def execute(a):
            self.actions.append((a["type"], tuple(a.get("keys") or ()), a.get("x"), a.get("y")))
            self.ui.execute(a)

        def paste(text, settle=0.15):
            self.actions.append(("paste", text))
            self.ui.paste(text, settle)

        mock.patch.object(lark, "activate", return_value=1).start()
        mock.patch.object(lark, "area", side_effect=self.ui.area).start()
        mock.patch.object(lark, "_native", side_effect=self.ui.native).start()
        mock.patch.object(lark.computer, "execute", side_effect=execute).start()
        mock.patch.object(lark.computer, "_paste_text", side_effect=paste).start()

        def paste_image(path, settle=0.8):
            self.actions.append(("paste_image", os.path.basename(path)))
            self.ui.paste_image(path, settle)
        mock.patch.object(lark.memes, "paste_image", side_effect=paste_image).start()
        mock.patch.object(lark.computer, "IMAGE_SCALE", 1.0).start()
        self.dialog = mock.patch.object(lark.computer, "system_dialog", return_value=None).start()  # 不看真屏幕上的系统弹窗
        mock.patch.object(lark.time, "sleep").start()
        self.home = tempfile.TemporaryDirectory()
        meme_dir = make_meme_dir()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "MEMES_DIR": meme_dir}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)
        self.addCleanup(__import__("shutil").rmtree, meme_dir, True)

    def keys(self):
        return [a[1] for a in self.actions if a[0] == "keypress"]

    # --- 纯函数 ---
    def test_norm_ignores_case_space_and_member_count(self):
        self.assertEqual(lark.norm("Hong Chen"), lark.norm("hongchen"))
        self.assertEqual(lark.norm("Nevamind AI (13)"), lark.norm("Nevamind AI"))
        self.assertNotEqual(lark.norm("Hong"), lark.norm("Hong Chen"))

    def test_join_text_spacing(self):
        self.assertEqual(lark.join_text(["欢迎", "Ami", "加入", "Nevamind AI", "，新成员入群"]),
                         "欢迎 Ami 加入 Nevamind AI，新成员入群")

    def test_messages_from_and_dividers(self):
        self.ui.title = "Hong Chen"
        self.ui.msgs = [("other", "hi"), ("self", "你好")]
        msgs = lark.messages(self.ui.chat_area())
        self.assertEqual(msgs, [{"from": "时间", "text": "昨天"}, {"from": "Hong Chen", "text": "hi"},
                                {"from": "我", "text": "你好"}])

    def test_group_sender_and_system_message(self):
        chat = G("", G("chatContainer.groupChat", G("chatWindow_chatName", T("项目群")), G("list_items",
                 G("js-message-item.message-not-self", G("message-info", G("message-info-name", T("张三"))),
                   G("message-content", T("明天开会"))),
                 G("js-message-item.system-text-background.message-not-self", T("欢迎"),
                   G("user-name", T("李四")), T("加入")))), role="AXWebArea")
        self.assertEqual(lark.messages(chat), [{"from": "张三", "text": "明天开会"},
                                               {"from": "系统", "text": "欢迎 李四 加入"}])

    def test_input_placeholder_counts_as_empty(self):
        box = lark.input_box(self.ui.chat_area())
        self.assertEqual(lark.input_text(box, "其他人"), "")

    # --- 打开聊天 ---
    def test_open_chat_from_feed(self):
        pid, title = lark.open_chat("hong chen")
        self.assertEqual(title, "Hong Chen")
        self.assertEqual(len([a for a in self.actions if a[0] == "click"]), 1)
        self.assertNotIn(("ENTER",), self.keys())

    def test_open_chat_via_search_joins_highlighted_parts(self):
        self.ui.results = [(["Hong", "Chen"], "user"), (["Nevamind AI", "(13)"], "group"), (["Hong", "Shen"], "user")]
        self.ui.feed = []
        pid, title = lark.open_chat("Hong Chen")
        self.assertEqual(title, "HongChen")  # 假界面把切开的名字拼回去当标题
        self.assertIn(("paste", "Hong Chen"), self.actions)
        self.assertNotIn(("ENTER",), self.keys())

    def test_open_chat_no_exact_match_lists_candidates(self):
        self.ui.results = [(["Hong", "Chen"], "user"), (["Hong", "Shen"], "user")]
        self.ui.feed = []
        with self.assertRaises(lark.LarkError) as ctx:
            lark.open_chat("hong")
        self.assertIn("Hong Chen（联系人）", str(ctx.exception))
        self.assertIn("Hong Shen（联系人）", str(ctx.exception))
        self.assertEqual(self.ui.title, "其他人")
        self.assertNotIn(("ENTER",), self.keys())
        self.assertIn(("BACKSPACE",), self.keys())  # 关搜索框前清空搜索词，免得留在侧边栏过滤会话列表
        self.assertFalse(self.ui.search_open)

    def test_open_chat_duplicate_names_refused(self):
        self.ui.results = [(["张三"], "user"), (["张三"], "user")]
        self.ui.feed = []
        with self.assertRaises(lark.LarkError) as ctx:
            lark.open_chat("张三")
        self.assertIn("2 个", str(ctx.exception))
        self.assertEqual(self.ui.title, "其他人")

    # --- 发送 ---
    def test_send_sends_and_verifies(self):
        r = lark.send("Hong Chen", "你好，这是测试")
        self.assertEqual(r["status"], "sent", r)
        self.assertIn("Hong Chen", r["detail"])
        self.assertEqual(self.ui.msgs[-1], ("self", "你好，这是测试"))
        self.assertEqual(self.keys().count(("ENTER",)), 1)

    def test_send_clears_existing_draft(self):
        lark.open_chat("Hong Chen")
        self.ui.input = "草稿"
        lark.send("Hong Chen", "正文")
        self.assertEqual(self.ui.msgs[-1], ("self", "正文"))

    def test_system_dialog_stops_before_any_key(self):
        lark.open_chat("Hong Chen")
        self.actions.clear()
        self.dialog.return_value = "UserNotificationCenter"
        with self.assertRaises(lark.LarkError) as ctx:
            lark.send("Hong Chen", "你好")
        self.assertIn("系统弹窗", str(ctx.exception))
        self.assertFalse([a for a in self.actions if a[0] in ("keypress", "paste")])

    def test_send_input_mismatch_does_not_press_enter(self):
        self.ui.paste = lambda text, settle=0.15: setattr(self.ui, "input", "别的内容")
        with self.assertRaises(lark.LarkError):
            lark.send("Hong Chen", "正文")
        self.assertNotIn(("ENTER",), self.keys())
        self.assertEqual(self.ui.input, "")

    def test_send_unverified_when_message_not_seen(self):
        self.ui.enter_sends = False
        r = lark.send("Hong Chen", "正文")
        self.assertEqual(r["status"], "sent_unverified")

    # --- 梗图 ---
    def test_send_text_then_meme(self):
        r = lark.send("Hong Chen", "收到", meme="收到收到")
        self.assertEqual(r["status"], "sent", r)
        self.assertEqual(r["meme_sent"], "收到收到")
        self.assertEqual(self.ui.msgs[-2:], [("self", "收到"), ("self", None)])
        self.assertIn(("paste_image", "收到收到.png"), self.actions)
        self.assertEqual(self.keys().count(("ENTER",)), 2)
        self.assertEqual(lark.messages(self.ui.chat_area())[-1], {"from": "我", "text": "[图片]"})

    def test_send_meme_only(self):
        r = lark.call_tool("lark_send", {"contact": "Hong Chen", "text": "", "meme": "包的"})
        self.assertEqual(r["status"], "sent", r)
        self.assertEqual(self.ui.msgs[-1], ("self", None))

    def test_unknown_meme_lists_choices_and_sends_nothing(self):
        r = lark.call_tool("lark_send", {"contact": "Hong Chen", "text": "hi", "meme": "不存在的图"})
        self.assertEqual(r["status"], "error")
        self.assertIn("收到收到", r["detail"])
        self.assertEqual(self.actions, [])

    def test_meme_not_pasted_into_input_is_not_sent(self):
        self.ui.paste_image = lambda path, settle=0.8: None
        with self.assertRaises(lark.LarkError):
            lark.send("Hong Chen", "", meme="好滴")
        self.assertNotIn(("ENTER",), self.keys())

    def test_auto_meme_uses_jev_pick(self):
        picked = {"meme": lark.memes.find("辛苦啦"), "confidence": 0.9, "reason": "Jev 选了「辛苦啦」"}
        with mock.patch.object(lark.memes, "pick", return_value=picked) as pick:
            r = lark.send("Hong Chen", "早点休息", meme="auto")
        self.assertEqual(r["status"], "sent", r)
        self.assertEqual(r["meme"], "Jev 选了「辛苦啦」")
        self.assertEqual(pick.call_args.args[0], "早点休息")
        self.assertEqual(pick.call_args.args[1], [{"from": "Hong Chen", "text": "hi"}])  # 最近的聊天，不含日期
        self.assertEqual(r["meme_sent"], "辛苦啦")
        self.assertEqual(self.ui.msgs[-1], ("self", None))

    def test_auto_meme_none_sends_text_only(self):
        with mock.patch.object(lark.memes, "pick", return_value={"meme": None, "confidence": 0.0, "reason": "不用配"}):
            r = lark.send("Hong Chen", "附件请查收", meme="auto")
        self.assertEqual(r["status"], "sent")
        self.assertEqual(self.ui.msgs[-1], ("self", "附件请查收"))
        self.assertNotIn("paste_image", [a[0] for a in self.actions])

        # --- 读 / 会话列表 / 注册 ---
    def test_read_and_chats(self):
        r = lark.call_tool("lark_read", {"contact": "Hong Chen", "limit": 1})
        self.assertEqual(r["chat"], "Hong Chen")
        self.assertEqual(r["messages"], [{"from": "Hong Chen", "text": "hi"}])
        c = lark.call_tool("lark_chats", {"query": "nevamind"})
        self.assertEqual([x["name"] for x in c["chats"]], ["Nevamind AI"])
        self.assertNotIn("_node", json.dumps(c, default=str))

    def test_chats_last_from_me(self):
        self.ui.feed_mine = {"Hong Chen"}
        rows = {c["name"]: c for c in lark.chats()["chats"]}
        self.assertIs(rows["Hong Chen"]["last_from_me"], True)
        self.assertIs(rows["Nevamind AI"]["last_from_me"], False)  # False 也要保留，不能被当成空值丢掉
        self.assertEqual(rows["Hong Chen"]["preview"], "预览0")
        self.assertEqual(rows["Hong Chen"]["time"], "昨天")
        self.assertIn("last_from_me", lark.RULES)

    def test_feed_card_unread_badge_is_not_the_name(self):
        def card(*kids):
            return G("a11y_feed_card_item._1e0a457", *kids, frame=(484, 160, 432, 60))
        feed = G("", G("scroller.a11y_feed_main_list",
                       card(G("avatar", G("ud__badge", T("4"))), G("_9f5c521", T("Hong Chen")), T("10:05"), T("hi")),
                       card(G("avatar", T("99+")), G("_9f5c521", T("Nevamind AI")), T("昨天"), T("欢迎")),
                       card(G("_9f5c521", T("2048")), T("昨天"), T("好的"))),  # 名字本身是数字、没有角标
                 role="AXWebArea", text="messenger")
        rows = [(c["name"], c["unread"], c["preview"], c["time"]) for c in lark.feed_cards(feed)]
        self.assertEqual(rows, [("Hong Chen", 4, "hi", "10:05"), ("Nevamind AI", 99, "欢迎", "昨天"),
                                ("2048", 0, "好的", "昨天")])

    def test_tool_specs_and_registration(self):
        self.assertEqual({t["name"] for t in lark.TOOLS}, {"lark_chats", "lark_read", "lark_send"})
        for t in lark.TOOLS:
            self.assertEqual(set(t["parameters"]["required"]), set(t["parameters"]["properties"]))
        self.assertIn("lark_send", tools.write_tools())
        self.assertTrue(lark.JEV_SKIP.search("给 hong 发一条飞书消息"))
        self.assertIn(lark, tools.PROVIDERS)
        self.assertIn("Hong", lark.describe_call("lark_send", {"contact": "Hong", "text": "hi", "meme": ""}))
        self.assertIn("包的", lark.describe_call("lark_send", {"contact": "Hong", "text": "", "meme": "包的"}))
        self.assertIn("meme 参数", lark.RULES)


if __name__ == "__main__":
    unittest.main()


class MemesTest(unittest.TestCase):
    def test_catalog_reads_subdirs_json_and_filenames(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "ChineseBQB", "负鼠"))
            Image.new("RGB", (10, 10)).save(os.path.join(d, "ChineseBQB", "负鼠", "负鼠00002-收到办不到.png"))
            Image.new("RGB", (10, 10)).save(os.path.join(d, "我的梗.jpg"))
            with open(os.path.join(d, "memes.json"), "w", encoding="utf-8") as f:
                json.dump({"我的梗": "自己加的"}, f, ensure_ascii=False)
            with mock.patch.dict(os.environ, {"MEMES_DIR": d}):
                self.assertEqual(lark.memes.find("我的梗")["desc"], "自己加的")
                self.assertEqual(lark.memes.find("负鼠00002-收到办不到")["desc"], "收到办不到")
                self.assertIsNone(lark.memes.find("没有"))
                self.assertEqual(len(lark.memes.catalog()), 2)
                self.assertTrue(lark.memes.png_bytes(os.path.join(d, "我的梗.jpg")).startswith(b"\x89PNG"))

    def test_empty_library(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MEMES_DIR": d}):
            self.assertEqual(lark.memes.catalog(), [])
            self.assertIsNone(lark.memes.pick("hi", client=FakeJev({}))["meme"])


class FakeJev:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return self.answers(questions) if callable(self.answers) else self.answers


class PickTest(unittest.TestCase):
    def setUp(self):
        d = make_meme_dir()
        mock.patch.dict(os.environ, {"MEMES_DIR": d}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(__import__("shutil").rmtree, d, True)

    def test_pick_yes_and_best(self):
        c = FakeJev({"attach": {"choice": "yes", "probabilities": {"yes": 0.9, "no": 0.1}},
                     "g0": {"choice": "辛苦啦", "probabilities": {"辛苦啦": 0.7, "包的": 0.3}}})
        r = lark.memes.pick("早点休息", [{"from": "A", "text": "好累"}], "A", client=c)
        self.assertEqual(r["meme"]["name"], "辛苦啦")
        state, q = c.calls[0]
        self.assertEqual(state["reply_to_send"], "早点休息")
        self.assertIn("yes", q["attach"]["criteria"])
        self.assertNotIn("none", q["g0"]["criteria"])

    def test_pick_no(self):
        c = FakeJev({"attach": {"choice": "no", "probabilities": {"yes": 0.02, "no": 0.98}},
                     "g0": {"choice": "救命", "probabilities": {"救命": 0.9}}})
        self.assertIsNone(lark.memes.pick("附件请查收", client=c)["meme"])

    def test_pick_weak_fit(self):
        c = FakeJev({"attach": {"choice": "yes", "probabilities": {"yes": 0.8, "no": 0.2}},
                     "g0": {"choice": "好滴", "probabilities": {"好滴": 0.12}}})
        self.assertIsNone(lark.memes.pick("嗯嗯", client=c)["meme"])

    def test_pick_jev_error_means_no_meme(self):
        class Broken:
            def ask(self, state, questions):
                raise jev.JevError("boom")
        r = lark.memes.pick("hi", client=Broken())
        self.assertIsNone(r["meme"])
        self.assertIn("boom", r["reason"])

    def test_pick_many_memes_runs_final_round(self):
        many = [{"name": f"梗{i}", "desc": f"用途{i}", "path": f"/x/{i}.png"} for i in range(90)]

        def answers(q):
            if "attach" in q:
                return {"attach": {"choice": "yes", "probabilities": {"yes": 0.9, "no": 0.1}},
                        **{k: {"choice": next(iter(v["criteria"])), "probabilities": {}} for k, v in q.items() if k != "attach"}}
            names = list(q["g0"]["criteria"])
            return {"g0": {"choice": names[-1], "probabilities": {names[-1]: 0.8}}}
        c = FakeJev(answers)
        with mock.patch.object(lark.memes, "catalog", return_value=many):
            r = lark.memes.pick("hi", client=c)
        self.assertEqual(len(c.calls), 2)
        self.assertEqual(len(c.calls[0][1]), 1 + 3)            # attach + 3 组
        self.assertEqual(list(c.calls[1][1]["g0"]["criteria"]), ["梗0", "梗40", "梗80"])
        self.assertEqual(r["meme"]["name"], "梗80")

    def test_client_from_env_falls_back_to_nevatoken(self):
        env = {"TYPESAFE_API_KEY": "", "OPENAI_BASE_URL": "https://nevatoken.com/v1/", "OPENAI_API_KEY": "sk-x"}
        with mock.patch.dict(os.environ, env):
            c = jev.client_from_env()
        self.assertEqual(c.url, "https://nevatoken.com/v1/systemone")
        with mock.patch.dict(os.environ, {**env, "OPENAI_BASE_URL": "https://api.openai.com/v1"}):
            self.assertIsNone(jev.client_from_env())

    def test_guess_desc_from_filename(self):
        self.assertEqual(lark.memes.guess_desc("负鼠00002-收到办不到"), "收到办不到")
        self.assertEqual(lark.memes.guess_desc("Penguin00002"), "")
