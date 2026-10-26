"""飞书 / Lark 接口模块的离线测试：requests 全部打桩，不联网。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import feishu  # noqa: E402


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def ok(data=None):
    return Resp({"code": 0, "msg": "success", "data": data or {}})


TOKEN = Resp({"code": 0, "msg": "ok", "tenant_access_token": "t-abc", "expire": 7200})
CHATS = ok({"items": [{"chat_id": "oc_1", "name": "产品组"}, {"chat_id": "oc_2", "name": "研发"},
                      {"chat_id": "oc_3", "name": "研发"}, {"chat_id": "oc_4", "name": "产品组-外包"}],
            "has_more": False, "page_token": ""})


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "FEISHU_APP_ID": "cli_x",
                                     "FEISHU_APP_SECRET": "sec", "FEISHU_DOMAIN": "lark"}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)
        feishu._token.clear()
        feishu._user_names.clear()
        feishu._contact_ok[0] = True
        self.post = mock.patch.object(feishu.requests, "post", return_value=TOKEN).start()
        self.routes = {}  # (方法, 路径) → Resp
        self.calls = []
        self.req = mock.patch.object(feishu.requests, "request", side_effect=self._route).start()

    def _route(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split("/open-apis", 1)[1]
        self.calls.append((method, path, params, json))
        assert headers["Authorization"] == "Bearer t-abc"
        assert timeout
        r = self.routes.get((method, path))
        if r is None:
            raise AssertionError(f"没有打桩的请求 {method} {path}")
        return r() if callable(r) else r


class SchemaTest(unittest.TestCase):
    def test_strict_schema(self):
        names = set()
        for t in feishu.TOOLS:
            self.assertEqual(t["type"], "function")
            self.assertTrue(t["strict"])
            p = t["parameters"]
            self.assertFalse(p["additionalProperties"])
            self.assertEqual(set(p["required"]), set(p["properties"]))
            names.add(t["name"])
        self.assertEqual(names, {"feishu_doc_read", "feishu_chats", "feishu_read", "feishu_send"})
        self.assertEqual(feishu.WRITE_TOOLS, {"feishu_send"})
        self.assertTrue(feishu.RULES.startswith("\n- "))
        self.assertTrue(feishu.JEV_SKIP.search("帮我看看 Lark 群里说了啥"))
        self.assertTrue(feishu.JEV_SKIP.search("读飞书文档"))

    def test_unavailable_without_creds(self):
        with mock.patch.dict(os.environ, {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}):
            self.assertFalse(feishu.available())
            r = feishu.call_tool("feishu_chats", {"query": ""})
            self.assertEqual(r["status"], "error")
        with mock.patch.dict(os.environ, {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "sec"}):
            self.assertTrue(feishu.available())

    def test_domain(self):
        with mock.patch.dict(os.environ, {"FEISHU_DOMAIN": ""}):
            self.assertEqual(feishu._base(), "https://open.larksuite.com")
        with mock.patch.dict(os.environ, {"FEISHU_DOMAIN": "feishu"}):
            self.assertEqual(feishu._base(), "https://open.feishu.cn")

    def test_describe(self):
        self.assertIn("产品组", feishu.describe_call("feishu_send", {"chat": "产品组", "text": "hi"}))


class ParseTest(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(feishu.parse_doc("https://abc.sg.larksuite.com/docx/AbCdEf123456?from=x"),
                         ("docx", "AbCdEf123456", "lark"))
        self.assertEqual(feishu.parse_doc("https://abc.feishu.cn/docx/AbCdEf123456#part"),
                         ("docx", "AbCdEf123456", "feishu"))
        self.assertEqual(feishu.parse_doc("https://abc.sg.larksuite.com/wiki/WikiTok12345"),
                         ("wiki", "WikiTok12345", "lark"))
        self.assertEqual(feishu.parse_doc("abc.feishu.cn/wiki/WikiTok12345"), ("wiki", "WikiTok12345", "feishu"))
        self.assertEqual(feishu.parse_doc("AbCdEf123456"), ("docx", "AbCdEf123456", ""))
        with self.assertRaises(feishu.FeishuError):
            feishu.parse_doc("https://example.com/hello")

    def test_message_text(self):
        m = {"msg_type": "text", "body": {"content": json.dumps({"text": "@_user_1 看下 @_all"})},
             "mentions": [{"key": "@_user_1", "name": "张三"}]}
        self.assertEqual(feishu.message_text(m), "@张三 看下 @所有人")
        post = {"title": "周报", "content": [[{"tag": "text", "text": "完成 "}, {"tag": "a", "text": "链接", "href": "u"}],
                                            [{"tag": "img", "image_key": "k"}]]}
        self.assertEqual(feishu.message_text({"msg_type": "post", "body": {"content": json.dumps(post)}}),
                         "周报\n完成 链接(u)\n[图片]")
        wrapped = {"zh_cn": post}
        self.assertTrue(feishu.message_text({"msg_type": "post", "body": {"content": json.dumps(wrapped)}})
                        .startswith("周报"))
        self.assertEqual(feishu.message_text({"msg_type": "image", "body": {"content": "{}"}}), "[图片]")
        self.assertEqual(feishu.message_text({"msg_type": "file", "body": {"content": json.dumps({"file_name": "a.pdf"})}}),
                         "[文件 a.pdf]")


class ApiTest(Base):
    def test_token_cached(self):
        self.routes[("GET", "/im/v1/chats")] = CHATS
        feishu.chats("")
        feishu.chats("研发")
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.req.call_count, 2)
        self.assertTrue(self.post.call_args[0][0].startswith("https://open.larksuite.com/open-apis/auth/v3/"))

    def test_expired_token_retried(self):
        seq = iter([Resp({"code": 99991663, "msg": "token invalid"}), CHATS])
        self.routes[("GET", "/im/v1/chats")] = lambda: next(seq)
        self.assertEqual(feishu.chats("")["count"], 4)
        self.assertEqual(self.post.call_count, 2)

    def test_auth_failure(self):
        self.post.return_value = Resp({"code": 10003, "msg": "invalid param"})
        r = feishu.call_tool("feishu_chats", {"query": ""})
        self.assertEqual(r["status"], "error")
        self.assertIn("invalid param", r["detail"])

    def test_code_error(self):
        self.routes[("GET", "/im/v1/chats")] = Resp({"code": 99991672, "msg": "Access denied. scope required"}, 400)
        r = feishu.call_tool("feishu_chats", {"query": ""})
        self.assertEqual(r["status"], "error")
        self.assertIn("Access denied", r["detail"])
        self.assertIn("im:chat:readonly", r["detail"])

    def test_chats_filter(self):
        self.routes[("GET", "/im/v1/chats")] = CHATS
        r = feishu.call_tool("feishu_chats", {"query": "产品"})
        self.assertEqual([c["chat_id"] for c in r["chats"]], ["oc_1", "oc_4"])

    def test_doc_read_docx(self):
        self.routes[("GET", "/docx/v1/documents/DocTok12345")] = ok({"document": {"title": "方案"}})
        self.routes[("GET", "/docx/v1/documents/DocTok12345/raw_content")] = ok({"content": "x" * 30})
        r = feishu.call_tool("feishu_doc_read", {"url_or_token": "https://a.sg.larksuite.com/docx/DocTok12345",
                                                 "max_chars": 10})
        self.assertEqual((r["status"], r["title"], r["text"]), ("ok", "方案", "x" * 10))
        self.assertIn("truncated", r)
        self.assertIn("note", r)

    def test_wiki_resolves_to_docx(self):
        self.routes[("GET", "/wiki/v2/spaces/get_node")] = ok(
            {"node": {"obj_token": "DocTok12345", "obj_type": "docx", "title": "知识库页"}})
        self.routes[("GET", "/docx/v1/documents/DocTok12345")] = ok({"document": {"title": "知识库页"}})
        self.routes[("GET", "/docx/v1/documents/DocTok12345/raw_content")] = ok({"content": "正文"})
        r = feishu.doc_read("https://a.sg.larksuite.com/wiki/WikiTok12345")
        self.assertEqual((r["document_id"], r["text"]), ("DocTok12345", "正文"))
        self.assertEqual(self.calls[0][2], {"token": "WikiTok12345"})

    def test_wiki_non_docx(self):
        self.routes[("GET", "/wiki/v2/spaces/get_node")] = ok({"node": {"obj_token": "S", "obj_type": "sheet"}})
        r = feishu.call_tool("feishu_doc_read", {"url_or_token": "https://a.larksuite.com/wiki/WikiTok12345",
                                                 "max_chars": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("sheet", r["detail"])

    def test_doc_denied_hint(self):
        self.routes[("GET", "/docx/v1/documents/DocTok12345")] = Resp({"code": 1770032, "msg": "forBidden"}, 403)
        r = feishu.call_tool("feishu_doc_read", {"url_or_token": "DocTok12345", "max_chars": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("添加文档应用", r["detail"])

    def test_doc_domain_mismatch(self):
        r = feishu.call_tool("feishu_doc_read", {"url_or_token": "https://a.feishu.cn/docx/DocTok12345",
                                                 "max_chars": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("FEISHU_DOMAIN=feishu", r["detail"])
        self.req.assert_not_called()

    def test_read_messages(self):
        self.routes[("GET", "/im/v1/chats")] = CHATS
        self.routes[("GET", "/im/v1/messages")] = ok({"items": [
            {"msg_type": "text", "create_time": "1700000060000", "sender": {"id": "ou_bbbbbbbb", "sender_type": "user"},
             "body": {"content": json.dumps({"text": "第二条"})}},
            {"msg_type": "text", "create_time": "1700000000000", "sender": {"id": "cli_x", "sender_type": "app"},
             "body": {"content": json.dumps({"text": "第一条"})}}]})
        self.routes[("GET", "/contact/v3/users/ou_bbbbbbbb")] = Resp({"code": 99991672, "msg": "no permission"})
        r = feishu.call_tool("feishu_read", {"chat": "产品组", "count": 0})
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["messages"][0].endswith("机器人：第一条"))
        self.assertTrue(r["messages"][1].endswith("用户…bbbbbb：第二条"))
        params = [c for c in self.calls if c[1] == "/im/v1/messages"][0][2]
        self.assertEqual((params["container_id"], params["sort_type"], params["page_size"]),
                         ("oc_1", "ByCreateTimeDesc", 20))


class SendTest(Base):
    def setUp(self):
        super().setUp()
        self.routes[("GET", "/im/v1/chats")] = CHATS
        self.routes[("GET", "/im/v1/chats/oc_9")] = ok({"name": "临时群"})
        self.routes[("POST", "/im/v1/messages")] = ok({"message_id": "om_1"})

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def test_ambiguous_and_missing_no_send(self):
        for name, word in (("研发", "chat_id"), ("市场部", "没有")):
            r = feishu.call_tool("feishu_send", {"chat": name, "text": "hi"})
            self.assertEqual(r["status"], "error")
            self.assertIn(word, r["detail"])
        self.assertEqual(self.posts(), [])

    def test_posts(self):
        r = feishu.call_tool("feishu_send", {"chat": "产品组", "text": "明天 10 点开会"})
        self.assertEqual(r["status"], "sent")
        self.assertIn("产品组", r["detail"])
        self.assertEqual(r["message_id"], "om_1")
        (_, _, params, body), = self.posts()
        self.assertEqual(params, {"receive_id_type": "chat_id"})
        self.assertEqual((body["receive_id"], body["msg_type"]), ("oc_1", "text"))
        self.assertEqual(json.loads(body["content"]), {"text": "明天 10 点开会"})

    def test_chat_id_direct(self):
        r = feishu.call_tool("feishu_send", {"chat": "oc_9", "text": "hi"})
        self.assertEqual(r["status"], "sent")
        self.assertIn("临时群", r["detail"])
        self.assertEqual(self.posts()[0][3]["receive_id"], "oc_9")

    def test_send_error_code(self):
        self.routes[("POST", "/im/v1/messages")] = Resp({"code": 230002, "msg": "Bot is not in the chat"}, 400)
        r = feishu.call_tool("feishu_send", {"chat": "产品组", "text": "hi"})
        self.assertEqual(r["status"], "error")
        self.assertIn("Bot is not in the chat", r["detail"])


if __name__ == "__main__":
    unittest.main()
