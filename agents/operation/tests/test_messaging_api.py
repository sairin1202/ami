"""Discord / Slack / Telegram 机器人接口模块的离线测试：requests 全部用桩替换，不联网。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import discord  # noqa: E402
import slack  # noqa: E402
import telegram  # noqa: E402

MODULES = [discord, slack, telegram]
TOKENS = {"DISCORD_BOT_TOKEN": "dtok", "SLACK_BOT_TOKEN": "xoxb-test", "TELEGRAM_BOT_TOKEN": "123:SECRET"}


class Resp:
    def __init__(self, data=None, status=200, headers=None):
        self.status_code, self.headers, self._data = status, headers or {}, data
        self.content = b"" if data is None else json.dumps(data).encode()

    def json(self):
        if self._data is None:
            raise ValueError("no body")
        return self._data


class FakeHTTP:
    """按「方法 + 网址里的关键字」返回预设的响应，并记下每次调用。"""

    def __init__(self, routes):
        self.routes = routes  # [(method, substring, Resp 或 callable(kwargs) -> Resp)]
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, sub, r in self.routes:
            if m == method and sub in url:
                return r(kw) if callable(r) else r
        raise AssertionError(f"意外的请求 {method} {url}")

    def posts(self, sub=""):
        return [c for c in self.calls if c[0] == "POST" and sub in c[1]]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, **TOKENS}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def http(self, mod, routes):
        f = FakeHTTP(routes)
        mock.patch.object(mod.requests, "request", f).start()
        return f


class CommonTest(Base):
    def test_strict_schema(self):
        for mod in MODULES:
            for t in mod.TOOLS:
                with self.subTest(tool=t["name"]):
                    self.assertEqual(t["type"], "function")
                    self.assertTrue(t["strict"])
                    p = t["parameters"]
                    self.assertEqual(p["type"], "object")
                    self.assertFalse(p["additionalProperties"])
                    self.assertEqual(sorted(p["required"]), sorted(p["properties"]))
                    for prop in p["properties"].values():
                        self.assertIn(prop["type"], ("string", "integer"))
                        self.assertTrue(prop["description"])
            names = {t["name"] for t in mod.TOOLS}
            self.assertTrue(mod.WRITE_TOOLS <= names)
            self.assertTrue(mod.RULES.startswith("\n- "))

    def test_available_needs_token(self):
        for mod, var in ((discord, "DISCORD_BOT_TOKEN"), (slack, "SLACK_BOT_TOKEN"), (telegram, "TELEGRAM_BOT_TOKEN")):
            self.assertTrue(mod.available())
            with mock.patch.dict(os.environ, {var: ""}):
                self.assertFalse(mod.available())
            with mock.patch.dict(os.environ):
                os.environ.pop(var)
                self.assertFalse(mod.available())

    def test_jev_skip_and_describe(self):
        self.assertTrue(discord.JEV_SKIP.search("在 Discord 里发消息"))
        self.assertTrue(slack.JEV_SKIP.search("读一下 slack #general"))
        self.assertTrue(telegram.JEV_SKIP.search("Telegram 机器人"))
        self.assertIn("general", discord.describe_call("discord_send", {"channel": "srv/#general", "text": "hi"}))
        self.assertIn("回复", slack.describe_call("slack_send", {"channel": "#a", "text": "x", "thread_ts": "1.2"}))
        self.assertIn("@chan", telegram.describe_call("telegram_send", {"chat": "@chan", "text": "x"}))

    def test_unknown_tool(self):
        for mod in MODULES:
            self.assertEqual(mod.call_tool("nope", {})["status"], "error")


# ---------------------------------------------------------------------------
GUILDS = [{"id": "100000000000000001", "name": "Alpha"}, {"id": "100000000000000002", "name": "Beta"}]
CHANS = {
    "100000000000000001": [{"id": "200000000000000001", "name": "general", "type": 0, "position": 0},
                           {"id": "200000000000000002", "name": "random", "type": 0, "position": 1},
                           {"id": "200000000000000009", "name": "Voice", "type": 2, "position": 2}],
    "100000000000000002": [{"id": "300000000000000001", "name": "general", "type": 0, "position": 0}],
}


def discord_routes(extra=()):
    routes = list(extra) + [("GET", "/users/@me/guilds", Resp(GUILDS))]
    routes += [("GET", f"/guilds/{g}/channels", Resp(c)) for g, c in CHANS.items()]
    return routes


class DiscordTest(Base):
    def test_channels(self):
        self.http(discord, discord_routes())
        r = discord.call_tool("discord_channels", {"guild": ""})
        self.assertEqual(r["status"], "ok")
        self.assertEqual([g["guild"] for g in r["guilds"]], ["Alpha", "Beta"])
        self.assertEqual([c["name"] for c in r["guilds"][0]["channels"]], ["general", "random"])  # 不列语音频道
        r = discord.call_tool("discord_channels", {"guild": "beta"})
        self.assertEqual(len(r["guilds"]), 1)
        self.assertEqual(discord.call_tool("discord_channels", {"guild": "Gamma"})["status"], "error")

    def test_ambiguous_channel_no_send(self):
        f = self.http(discord, discord_routes())
        r = discord.call_tool("discord_send", {"channel": "#general", "text": "hi"})
        self.assertEqual(r["status"], "error")
        self.assertIn("多个", r["detail"])
        self.assertEqual(f.posts(), [])

    def test_not_found_no_send(self):
        f = self.http(discord, discord_routes())
        r = discord.call_tool("discord_send", {"channel": "Alpha/#nope", "text": "hi"})
        self.assertEqual(r["status"], "error")
        self.assertEqual(f.posts(), [])

    def test_posts(self):
        f = self.http(discord, discord_routes([("POST", "/channels/200000000000000001/messages", Resp({"id": "9"}))]))
        r = discord.call_tool("discord_send", {"channel": "Alpha/#general", "text": "hello"})
        self.assertEqual(r["status"], "sent", r)
        (_, _, kw), = f.posts()
        self.assertEqual(kw["json"]["content"], "hello")
        self.assertEqual(kw["json"]["allowed_mentions"], {"parse": []})
        self.assertEqual(kw["headers"]["Authorization"], "Bot dtok")
        self.assertIn("Alpha / #general", r["detail"])

    def test_send_by_id(self):
        f = self.http(discord, discord_routes([
            ("GET", "/channels/300000000000000001", Resp({"id": "300000000000000001", "name": "general", "type": 0,
                                                         "guild_id": "100000000000000002"})),
            ("POST", "/messages", Resp({"id": "1"}))]))
        r = discord.call_tool("discord_send", {"channel": "300000000000000001", "text": "x"})
        self.assertEqual(r["status"], "sent")
        self.assertIn("Beta / #general", r["detail"])
        self.assertEqual(len(f.posts()), 1)

    def test_429(self):
        self.http(discord, [("GET", "/users/@me/guilds", Resp({"message": "rate", "retry_after": 2.5}, 429))])
        r = discord.call_tool("discord_channels", {"guild": ""})
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["retry_after"], 2.5)

    def test_read(self):
        msgs = [{"author": {"username": "bob"}, "timestamp": "2026-01-02T03:04:05.000000+00:00",
                 "content": "second", "attachments": [{"filename": "a.png"}]},
                {"author": {"username": "amy", "global_name": "Amy"}, "timestamp": "2026-01-02T03:00:00+00:00",
                 "content": "x" * 900, "attachments": []}]
        f = self.http(discord, discord_routes([("GET", "/channels/200000000000000002/messages", Resp(msgs))]))
        r = discord.call_tool("discord_read", {"channel": "Alpha/#random", "count": 500})
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual([m["author"] for m in r["messages"]], ["Amy", "bob"])  # 旧 → 新
        self.assertTrue(r["messages"][0]["content"].endswith("…"))
        self.assertEqual(r["messages"][1]["attachments"], ["a.png"])
        self.assertIn("note", r)
        self.assertEqual(f.calls[-1][2]["params"]["limit"], discord.MAX_READ)


# ---------------------------------------------------------------------------
SLACK_CHANNELS = {"ok": True, "channels": [
    {"id": "C0000001", "name": "general", "is_member": True, "is_private": False},
    {"id": "C0000002", "name": "secret", "is_member": True, "is_private": True},
    {"id": "C0000003", "name": "outside", "is_member": False}], "response_metadata": {"next_cursor": ""}}


class SlackTest(Base):
    def test_channels_members_only(self):
        self.http(slack, [("GET", "conversations.list", Resp(SLACK_CHANNELS))])
        r = slack.call_tool("slack_channels", {})
        self.assertEqual([c["name"] for c in r["channels"]], ["general", "secret"])

    def test_ok_false(self):
        self.http(slack, [("GET", "conversations.list", Resp({"ok": False, "error": "missing_scope",
                                                               "needed": "groups:read"}))])
        r = slack.call_tool("slack_channels", {})
        self.assertEqual(r["status"], "error")
        self.assertIn("groups:read", r["detail"])

    def test_post_ok_false(self):
        f = self.http(slack, [("GET", "conversations.list", Resp(SLACK_CHANNELS)),
                              ("POST", "chat.postMessage", Resp({"ok": False, "error": "not_in_channel"}))])
        r = slack.call_tool("slack_send", {"channel": "#general", "text": "hi", "thread_ts": ""})
        self.assertEqual(r["status"], "error")
        self.assertIn("invite", r["detail"])
        self.assertEqual(len(f.posts()), 1)

    def test_not_found_no_send(self):
        f = self.http(slack, [("GET", "conversations.list", Resp(SLACK_CHANNELS))])
        r = slack.call_tool("slack_send", {"channel": "#outside", "text": "hi", "thread_ts": ""})
        self.assertEqual(r["status"], "error")
        self.assertEqual(f.posts(), [])

    def test_bare_name_posts(self):
        f = self.http(slack, [("GET", "conversations.list", Resp(SLACK_CHANNELS)),
                              ("POST", "chat.postMessage", Resp({"ok": True, "ts": "1.0"}))])
        r = slack.call_tool("slack_send", {"channel": "general", "text": "hi", "thread_ts": ""})
        self.assertEqual(r["status"], "sent", r)
        self.assertIn("#general", r["detail"])
        (_, _, kw), = f.posts()
        self.assertEqual(kw["json"], {"channel": "C0000001", "text": "hi"})

    def test_thread_reply(self):
        f = self.http(slack, [("GET", "conversations.list", Resp(SLACK_CHANNELS)),
                              ("POST", "chat.postMessage", Resp({"ok": True, "ts": "2.0"}))])
        r = slack.call_tool("slack_send", {"channel": "#Secret", "text": "hi", "thread_ts": "1712345678.000100"})
        self.assertEqual(r["status"], "sent", r)
        (_, _, kw), = f.posts()
        self.assertEqual(kw["json"], {"channel": "C0000002", "text": "hi", "thread_ts": "1712345678.000100"})
        self.assertIn("帖子回复", r["detail"])

    def test_bad_thread_ts(self):
        f = self.http(slack, [])
        r = slack.call_tool("slack_send", {"channel": "#general", "text": "hi", "thread_ts": "abc"})
        self.assertEqual(r["status"], "error")
        self.assertEqual(f.calls, [])

    def test_429(self):
        self.http(slack, [("GET", "conversations.list", Resp({"ok": False, "error": "ratelimited"}, 429,
                                                              {"Retry-After": "30"}))])
        r = slack.call_tool("slack_channels", {})
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["retry_after"], 30)

    def test_read_resolves_users(self):
        slack._users.clear()
        hist = {"ok": True, "messages": [
            {"user": "U1", "ts": "1700000100.000200", "text": "hey <@U2> see <https://x.io|this>", "reply_count": 2},
            {"user": "U2", "ts": "1700000000.000100", "text": "first", "files": [{"name": "f.pdf"}]}]}
        users = {"U1": {"ok": True, "user": {"name": "u1", "profile": {"display_name": "Alice"}}},
                 "U2": {"ok": True, "user": {"name": "u2", "real_name": "Bob"}}}
        f = self.http(slack, [("GET", "conversations.history", Resp(hist)),
                              ("GET", "users.info", lambda kw: Resp(users[kw["params"]["user"]]))])
        f.routes.insert(0, ("GET", "conversations.info", Resp({"ok": True, "channel": {"name": "general"}})))
        r = slack.call_tool("slack_read", {"channel": "C0000001", "count": 0})
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["channel"], "#general")
        self.assertEqual([m["author"] for m in r["messages"]], ["Bob", "Alice"])
        self.assertEqual(r["messages"][1]["content"], "hey @Bob see this (https://x.io)")
        self.assertEqual(r["messages"][1]["ts"], "1700000100.000200")
        self.assertEqual(r["messages"][0]["files"], ["f.pdf"])
        self.assertEqual(len([c for c in f.calls if "users.info" in c[1]]), 2)  # 有缓存，每人只查一次


# ---------------------------------------------------------------------------
def tg_updates(*chats):
    out = []
    for i, (cid, title, text) in enumerate(chats):
        chat = {"id": cid, "type": "group", "title": title} if cid < 0 else \
            {"id": cid, "type": "private", "first_name": title}
        out.append({"update_id": i, "message": {"chat": chat, "from": {"first_name": "Zed"}, "date": 1700000000 + i,
                                                 "text": text}})
    return Resp({"ok": True, "result": out})


class TelegramTest(Base):
    def test_updates_no_offset(self):
        f = self.http(telegram, [("POST", "/getUpdates", tg_updates((-1, "Team", "a"), (5, "Ann", "b"),
                                                                     (-1, "Team", "c")))])
        r = telegram.call_tool("telegram_updates", {"count": 2})
        self.assertEqual(r["status"], "ok")
        self.assertEqual([m["text"] for m in r["messages"]], ["b", "c"])  # 最近的两条
        self.assertEqual(r["messages"][1]["chat_id"], -1)
        self.assertNotIn("offset", f.calls[0][2]["json"])  # 不确认（消费）更新
        self.assertIn("note", r)

    def test_send_by_title(self):
        f = self.http(telegram, [("POST", "/getUpdates", tg_updates((-42, "Team", "a"))),
                                 ("POST", "/sendMessage", Resp({"ok": True, "result": {"message_id": 7}}))])
        r = telegram.call_tool("telegram_send", {"chat": "Team", "text": "hello"})
        self.assertEqual(r["status"], "sent", r)
        self.assertIn("Team", r["detail"])
        (_, _, kw), = f.posts("sendMessage")
        self.assertEqual(kw["json"], {"chat_id": -42, "text": "hello"})

    def test_title_exact_only(self):
        f = self.http(telegram, [("POST", "/getUpdates", tg_updates((-42, "Team", "a")))])
        r = telegram.call_tool("telegram_send", {"chat": "team", "text": "x"})
        self.assertEqual(r["status"], "error")
        self.assertIn("Team", r["detail"])  # 列出最近出现过的聊天
        self.assertEqual(f.posts("sendMessage"), [])

    def test_ambiguous_title(self):
        f = self.http(telegram, [("POST", "/getUpdates", tg_updates((-1, "Team", "a"), (-2, "Team", "b")))])
        r = telegram.call_tool("telegram_send", {"chat": "Team", "text": "x"})
        self.assertEqual(r["status"], "error")
        self.assertIn("-1", r["detail"])
        self.assertEqual(f.posts("sendMessage"), [])

    def test_username(self):
        f = self.http(telegram, [("POST", "/getChat", Resp({"ok": True, "result": {
            "id": -100123, "type": "channel", "title": "My Channel", "username": "mychan"}})),
                                 ("POST", "/sendMessage", Resp({"ok": True, "result": {"message_id": 8}}))])
        r = telegram.call_tool("telegram_send", {"chat": "@mychan", "text": "x"})
        self.assertEqual(r["status"], "sent", r)
        self.assertIn("My Channel", r["detail"])
        self.assertEqual(f.calls[0][2]["json"], {"chat_id": "@mychan"})
        (_, _, kw), = f.posts("sendMessage")
        self.assertEqual(kw["json"], {"chat_id": -100123, "text": "x"})

    def test_unknown_id(self):
        self.http(telegram, [("POST", "/getChat", Resp({"ok": False, "error_code": 400,
                                                        "description": "Bad Request: chat not found"}, 400))])
        r = telegram.call_tool("telegram_send", {"chat": "123456", "text": "x"})
        self.assertEqual(r["status"], "error")

    def test_429_on_send(self):
        self.http(telegram, [("POST", "/getUpdates", tg_updates((-42, "Team", "a"))),
                             ("POST", "/sendMessage", Resp({"ok": False, "error_code": 429,
                                                            "description": "Too Many Requests",
                                                            "parameters": {"retry_after": 12}}, 429))])
        r = telegram.call_tool("telegram_send", {"chat": "Team", "text": "x"})
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["retry_after"], 12)

    def test_token_not_leaked(self):
        def boom(kw):
            raise telegram.requests.ConnectionError("failed for https://api.telegram.org/bot123:SECRET/getUpdates")
        self.http(telegram, [("POST", "/getUpdates", boom)])
        r = telegram.call_tool("telegram_updates", {"count": 5})
        self.assertEqual(r["status"], "error")
        self.assertNotIn("SECRET", r["detail"])


if __name__ == "__main__":
    unittest.main()
