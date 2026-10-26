"""本地消息记录：屏幕读取的对齐去重、接口消息去重、发出去的消息和读回来的对上、按聊天 / 内容读回。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import msgstore  # noqa: E402
import tools  # noqa: E402


def screen(*pairs):
    return [{"from": f, "text": t} for f, t in pairs]


class MsgStoreTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "COMPUTER_USE_MESSAGES": "on"}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def texts(self, chat, app=""):
        return [(m["from"], m["text"]) for m in msgstore.history(app, chat)["messages"]]

    def read(self, app, chat, msgs):
        return msgstore.record(f"{app}_read", {"contact": chat}, {"chat": chat, "messages": msgs})

    # --- 屏幕读取 ---
    def test_repeated_reads_append_only_new(self):
        self.assertEqual(self.read("lark", "Hong Chen", screen(("时间", "昨天"), ("Hong Chen", "hi"))), 1)
        self.assertEqual(self.read("lark", "Hong Chen", screen(("时间", "昨天"), ("Hong Chen", "hi"))), 0)
        n = self.read("lark", "Hong Chen", screen(("Hong Chen", "hi"), ("时间", "10:46"), ("我", "在吗"), ("Hong Chen", "在")))
        self.assertTrue(msgstore.history("lark", "Hong Chen")["messages"][2]["time"].startswith("10:46 之后（"))
        self.assertEqual(n, 2)
        self.assertEqual(self.texts("hong chen"), [("Hong Chen", "hi"), ("我", "在吗"), ("Hong Chen", "在")])
        msgs = msgstore.history("lark", "Hong Chen")["messages"]
        self.assertTrue(msgs[0]["time"].startswith("昨天（"))
        self.assertTrue(msgs[1]["time"].startswith("10:46（"))

    def test_window_scrolled_past_old_messages(self):
        self.read("wechat", "张三", screen(("对方", "a"), ("对方", "b"), ("我", "c")))
        self.read("wechat", "张三", screen(("我", "c"), ("对方", "d")))  # 一屏只剩最后一条旧的
        self.assertEqual(self.texts("张三"), [("对方", "a"), ("对方", "b"), ("我", "c"), ("对方", "d")])

    def test_window_shows_older_messages_too(self):
        self.read("wechat", "张三", screen(("对方", "c"), ("对方", "d")))
        self.read("wechat", "张三", screen(("对方", "a"), ("对方", "b"), ("对方", "c"), ("对方", "d"), ("我", "e")))
        self.assertEqual(self.texts("张三"), [("对方", "c"), ("对方", "d"), ("我", "e")])

    def test_repeated_identical_messages_kept(self):
        self.read("wechat", "张三", screen(("对方", "哈哈"), ("对方", "哈哈")))
        self.read("wechat", "张三", screen(("对方", "哈哈"), ("对方", "哈哈"), ("对方", "哈哈")))
        self.assertEqual(len(self.texts("张三")), 3)

    def test_sent_then_read_back_not_duplicated(self):
        self.read("lark", "Hong Chen", screen(("Hong Chen", "hi")))
        msgstore.record("lark_send", {"contact": "hong chen", "text": "你好", "meme": "auto"},
                        {"status": "sent", "meme_sent": "负鼠00023-重生之我又回到了周一"})
        self.read("lark", "Hong Chen", screen(("Hong Chen", "hi"), ("我", "你好"), ("我", "[图片]"), ("Hong Chen", "收到")))
        msgs = msgstore.history("lark", "Hong Chen")["messages"]
        self.assertEqual([(m["from"], m["text"]) for m in msgs],
                         [("Hong Chen", "hi"), ("我", "你好"), ("我", "[图片]"), ("Hong Chen", "收到")])
        self.assertEqual(msgs[2]["meme"], "负鼠00023-重生之我又回到了周一")
        self.assertRegex(msgs[1]["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")  # 发出去的有准确时间

    def test_failed_or_declined_send_not_stored(self):
        msgstore.record("lark_send", {"contact": "A", "text": "x"}, {"status": "declined"})
        msgstore.record("lark_send", {"contact": "A", "text": "x"}, {"status": "error"})
        self.assertEqual(msgstore.history()["chats"], [])

    # --- 接口读取 ---
    def test_feishu_lines_parsed_and_deduped(self):
        res = {"status": "ok", "chat": "产品组", "messages": ["10-12 09:30 张三：早\n大家好", "10-12 09:31 机器人：收到"]}
        self.assertEqual(msgstore.record("feishu_read", {"chat": "产品组"}, res), 2)
        self.assertEqual(msgstore.record("feishu_read", {"chat": "产品组"}, res), 0)
        msgs = msgstore.history("feishu", "产品组")["messages"]
        self.assertEqual(msgs[0]["text"], "早\n大家好")
        self.assertTrue(msgs[0]["time"].startswith("10-12 09:30"))

    def test_api_read_merges_with_sent(self):
        msgstore.record("slack_send", {"channel": "#general", "text": "deploy 好了"}, {"status": "sent"})
        res = {"status": "ok", "channel": "#general",
               "messages": [{"author": "bot", "time": "10-12 10:00", "ts": "1760234400.0001", "content": "deploy 好了"},
                            {"author": "amy", "time": "10-12 10:01", "ts": "1760234460.0002", "content": "nice",
                             "attachments": ["a.png"]}]}
        self.assertEqual(msgstore.record("slack_read", {"channel": "#general"}, res), 1)
        msgs = msgstore.history("slack", "#general")["messages"]
        self.assertEqual([m["text"] for m in msgs], ["deploy 好了", "nice\n[附件] a.png"])
        self.assertEqual(msgstore.record("slack_read", {"channel": "#general"}, res), 0)

    def test_telegram_updates_split_by_chat(self):
        res = {"status": "ok", "messages": [
            {"chat": "群A", "chat_id": 1, "from": "x", "time": "2026-10-12 10:00", "text": "1"},
            {"chat": "群B", "chat_id": 2, "from": "y", "time": "2026-10-12 10:01", "text": "2"}]}
        self.assertEqual(msgstore.record("telegram_updates", {"count": 20}, res), 2)
        self.assertEqual({c["chat"] for c in msgstore.history("telegram")["chats"]}, {"群A", "群B"})

    # --- 读回 ---
    def test_history_list_search_and_errors(self):
        self.read("lark", "Hong Chen", screen(("Hong Chen", "明天十点开会")))
        self.read("lark", "Hong Shen", screen(("Hong Shen", "周报交了吗")))
        self.read("wechat", "文件传输助手", screen(("我", "开会材料")))
        chats = msgstore.history()["chats"]
        self.assertEqual(chats[0]["chat"], "文件传输助手")       # 最近的在前
        self.assertEqual(chats[0]["app"], "微信")
        hits = msgstore.history(query="开会")["messages"]
        self.assertEqual({(m["chat"], m["text"]) for m in hits},
                         {("Hong Chen", "明天十点开会"), ("文件传输助手", "开会材料")})
        self.assertEqual(msgstore.history("飞书", "hong chen")["count"], 1)   # 「飞书」指客户端
        r = msgstore.history(chat="hong")
        self.assertEqual(r["status"], "error")
        self.assertIn("Hong Chen", r["detail"])
        self.assertEqual(msgstore.history(chat="没有这个人")["status"], "error")

    def test_limit_keeps_latest(self):
        self.read("wechat", "张三", screen(*[("对方", str(i)) for i in range(10)]))
        self.assertEqual([m["text"] for m in msgstore.history("wechat", "张三", limit=3)["messages"]], ["7", "8", "9"])

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"COMPUTER_USE_MESSAGES": "off"}):
            self.assertEqual(self.read("lark", "A", screen(("A", "hi"))), 0)
            self.assertFalse(msgstore.available())
        self.assertEqual(msgstore.history()["chats"], [])

    # --- 接入 ---
    def test_tools_call_records_and_tool_registered(self):
        fake = {"chat": "Hong Chen", "messages": screen(("Hong Chen", "hi"))}
        lark = __import__("lark")
        with mock.patch.object(lark, "call_tool", return_value=fake), mock.patch.object(lark, "available", return_value=True):
            self.assertIs(tools.call("lark_read", {"contact": "Hong Chen", "limit": 20}), fake)
        self.assertEqual(self.texts("Hong Chen"), [("Hong Chen", "hi")])
        self.assertIn("message_history", [t["name"] for t in tools.specs()])
        r = tools.call("message_history", {"app": "", "chat": "Hong Chen", "query": "", "limit": 10})
        self.assertEqual(r["count"], 1)

    def test_store_error_does_not_break_tool(self):
        lark = __import__("lark")
        fake = {"status": "sent"}
        with mock.patch.object(lark, "call_tool", return_value=fake), mock.patch.object(lark, "available", return_value=True), \
                mock.patch.object(msgstore, "record", side_effect=RuntimeError("disk full")):
            self.assertIs(tools.call("lark_send", {"contact": "A", "text": "x", "meme": ""}), fake)


if __name__ == "__main__":
    unittest.main()
