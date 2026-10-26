"""HN、V2EX、GitHub 接口工具的离线测试：mock 掉 requests.get 和 subprocess.run，不联网、不真的发评论。"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import github  # noqa: E402
import hn  # noqa: E402
import v2ex  # noqa: E402

MODULES = [hn, v2ex, github]


def resp(data, status=200):
    r = mock.Mock(status_code=status)
    r.json.return_value = data
    return r


def proc(data, rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=json.dumps(data) if not isinstance(data, str) else data,
                                       stderr=stderr)


class SchemaTest(unittest.TestCase):
    def test_strict_schema(self):
        for m in MODULES:
            for t in m.TOOLS:
                with self.subTest(tool=t["name"]):
                    p = t["parameters"]
                    self.assertTrue(t["strict"])
                    self.assertEqual(t["type"], "function")
                    self.assertEqual(sorted(p["required"]), sorted(p["properties"]))
                    self.assertIs(p["additionalProperties"], False)
                    for spec in p["properties"].values():
                        self.assertIn("description", spec)

    def test_module_interface(self):
        for m in MODULES:
            with self.subTest(module=m.__name__):
                self.assertTrue(m.RULES.startswith("\n- "))
                self.assertIn("数据", m.RULES)
                self.assertTrue(hasattr(m.JEV_SKIP, "search"))
                self.assertLessEqual(m.WRITE_TOOLS, {t["name"] for t in m.TOOLS})
                self.assertIsInstance(m.describe_call(m.TOOLS[0]["name"], {}), str)
                r = m.call_tool("no_such_tool", {})
                self.assertEqual(r["status"], "error")
        self.assertTrue(hn.JEV_SKIP.search("看看 Hacker News 今天的热门"))
        self.assertTrue(v2ex.JEV_SKIP.search("总结一下 V2EX 最热"))
        self.assertTrue(github.JEV_SKIP.search("看下 GitHub 上 openai/codex 的 issue"))
        self.assertEqual(hn.WRITE_TOOLS | v2ex.WRITE_TOOLS, set())
        self.assertEqual(github.WRITE_TOOLS, {"github_comment", "github_create_issue"})


class HNTest(unittest.TestCase):
    def test_html_to_text(self):
        s = 'First<p>See <a href="https:&#x2F;&#x2F;x.com&#x2F;a" rel="nofollow">https:&#x2F;&#x2F;x.com...</a> &amp; <i>more</i>'
        self.assertEqual(hn._text(s), "First\nSee https://x.com/a & more")

    def test_stories(self):
        now = time.time()

        def get(url, params=None, **kw):
            if url.endswith("topstories.json"):
                return resp([1, 2, 3])
            i = int(url.rsplit("/", 1)[1].split(".")[0])
            if i == 3:
                return resp({"id": 3, "deleted": True})
            return resp({"id": i, "title": f"T{i}", "url": f"https://e.com/{i}" if i == 1 else None,
                         "score": 10 * i, "descendants": i, "by": "pg", "time": now - 7200})

        with mock.patch("hn.requests.get", side_effect=get):
            r = hn.call_tool("hn_stories", {"kind": "top", "count": 5})
        self.assertEqual(r["status"], "ok")
        self.assertEqual([s["id"] for s in r["stories"]], [1, 2])  # 保持顺序，去掉已删除
        self.assertEqual(r["stories"][0]["url"], "https://e.com/1")
        self.assertEqual(r["stories"][1]["url"], "https://news.ycombinator.com/item?id=2")  # Ask HN 没有外链
        self.assertEqual(r["stories"][0]["age"], "2 小时前")
        self.assertIn("note", r)

    def test_bad_kind_and_network_error(self):
        self.assertEqual(hn.call_tool("hn_stories", {"kind": "jobs", "count": 5})["status"], "error")
        import requests
        with mock.patch("hn.requests.get", side_effect=requests.ConnectionError("down")):
            r = hn.call_tool("hn_search", {"query": "rust", "sort": "date", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("连不上", r["detail"])

    def test_search_endpoint_and_count(self):
        hits = [{"objectID": str(i), "title": f"t{i}", "points": i, "num_comments": 0, "author": "a",
                 "created_at_i": time.time()} for i in range(60)]
        with mock.patch("hn.requests.get", return_value=resp({"hits": hits, "nbHits": 999})) as g:
            r = hn.call_tool("hn_search", {"query": "rust", "sort": "date", "count": 100})
        self.assertTrue(g.call_args.args[0].endswith("/search_by_date"))
        self.assertEqual(g.call_args.kwargs["params"]["hitsPerPage"], 50)
        self.assertEqual(r["count"], 50)
        self.assertEqual(r["total"], 999)

    def test_item_tree_truncation(self):
        def c(text, children=(), author="u"):
            return {"type": "comment", "author": author, "text": text, "children": list(children)}

        deep = c("d1", [c("d2", [c("d3", [c("d4", [c("d5")])])])])
        tree = {"id": 7, "type": "story", "title": "Ask HN", "text": "<p>hi</p>", "points": 5, "author": "op",
                "children": [c("x" * 2000), c(None, [c("orphan reply")]), deep]}
        with mock.patch("hn.requests.get", return_value=resp(tree)):
            r = hn.call_tool("hn_item", {"id": 7, "max_comments": 0})
        self.assertEqual(r["story"]["text"], "hi")
        texts = [x["text"] for x in r["comments"]]
        self.assertTrue(texts[0].endswith("…") and len(texts[0]) == hn.COMMENT_CHARS + 1)
        self.assertIn("orphan reply", texts)  # 被删评论下的回复还在
        self.assertNotIn("d5", texts)  # 超过 4 层不展开
        self.assertEqual(r["comments_total"], 7)
        with mock.patch("hn.requests.get", return_value=resp(tree)):
            r = hn.call_tool("hn_item", {"id": 7, "max_comments": 2})
        self.assertEqual(r["comments_shown"], 2)

    def test_http_error(self):
        with mock.patch("hn.requests.get", return_value=resp(None, 503)):
            r = hn.call_tool("hn_item", {"id": 1, "max_comments": 0})
        self.assertEqual(r["status"], "error")


TOPIC = {"id": 42, "title": "求推荐", "url": "https://www.v2ex.com/t/42", "content": "正文" * 2000, "replies": 3,
         "member": {"username": "alice"}, "node": {"name": "qna"}, "created": time.time() - 120}


class V2EXTest(unittest.TestCase):
    def test_topics_hot(self):
        with mock.patch("v2ex.requests.get", return_value=resp([TOPIC] * 30)) as g:
            r = v2ex.call_tool("v2ex_topics", {"source": "hot", "node": "", "count": 5})
        self.assertTrue(g.call_args.args[0].endswith("/topics/hot.json"))
        self.assertEqual(r["count"], 5)
        t = r["topics"][0]
        self.assertEqual((t["author"], t["node"], t["age"]), ("alice", "qna", "2 分钟前"))
        self.assertEqual(len(t["content"]), v2ex.SNIPPET_CHARS + 1)

    def test_node_validation_and_url(self):
        r = v2ex.call_tool("v2ex_topics", {"source": "node", "node": "", "count": 0})
        self.assertEqual(r["status"], "error")
        with mock.patch.object(v2ex, "TOKEN", ""), \
                mock.patch("v2ex.requests.get", return_value=resp([TOPIC])) as g:
            r = v2ex.call_tool("v2ex_topics", {"source": "node", "node": "https://www.v2ex.com/go/python", "count": 0})
        self.assertEqual(g.call_args.kwargs["params"], {"node_name": "python"})
        self.assertEqual(r["source"], "节点 python")

    def test_node_v2_paginates_with_token(self):
        page = {"success": True, "result": [dict(TOPIC, member=None, node=None)] * 20}
        with mock.patch.object(v2ex, "TOKEN", "tok"), \
                mock.patch("v2ex.requests.get", return_value=resp(page)) as g:
            r = v2ex.call_tool("v2ex_topics", {"source": "node", "node": "python", "count": 30})
        self.assertEqual(g.call_count, 2)
        self.assertEqual(g.call_args.kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(r["count"], 30)
        self.assertEqual(r["topics"][0]["node"], "python")

    def test_topic_with_replies(self):
        replies = [{"member": {"username": f"u{i}"}, "content": "r" * 1000, "created": time.time()} for i in range(3)]

        def get(url, params=None, **kw):
            return resp([TOPIC] if "topics/show" in url else replies)

        with mock.patch("v2ex.requests.get", side_effect=get):
            r = v2ex.call_tool("v2ex_topic", {"id": 42, "max_replies": 2})
        self.assertEqual(r["replies_shown"], 2)
        self.assertEqual(r["replies_total"], 3)
        self.assertEqual(r["replies"][1]["floor"], 2)
        self.assertEqual(len(r["topic"]["content"]), v2ex.TOPIC_CHARS + 1)

    def test_topic_missing_and_v2_error(self):
        with mock.patch("v2ex.requests.get", return_value=resp([])):
            self.assertEqual(v2ex.call_tool("v2ex_topic", {"id": 1, "max_replies": 0})["status"], "error")
        with mock.patch.object(v2ex, "TOKEN", "tok"), \
                mock.patch("v2ex.requests.get", return_value=resp({"success": False, "message": "Invalid token"})):
            r = v2ex.call_tool("v2ex_notifications", {"count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("Invalid token", r["detail"])

    def test_notifications_hidden_without_token(self):
        if not v2ex.TOKEN:
            self.assertNotIn("v2ex_notifications", [t["name"] for t in v2ex.TOOLS])
        with mock.patch.object(v2ex, "TOKEN", ""):
            self.assertEqual(v2ex.call_tool("v2ex_notifications", {"count": 5})["status"], "error")

    def test_notifications(self):
        n = {"member": {"username": "bob"}, "text": '<a href="/member/bob">bob</a> 在 <a href="/t/1">x</a> 里回复了你',
             "payload": "谢谢", "created": time.time()}
        with mock.patch.object(v2ex, "TOKEN", "tok"), \
                mock.patch("v2ex.requests.get", return_value=resp({"success": True, "result": [n] * 3})):
            r = v2ex.call_tool("v2ex_notifications", {"count": 0})
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["notifications"][0]["what"], "bob 在 x 里回复了你")


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def run_gh(self, routes):
        """routes: 路径前缀 → 返回的数据（或 CompletedProcess）。记下所有调用。"""
        self.calls = []

        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            path = cmd[cmd.index("-X") + 2]
            for prefix, data in routes:
                if path.startswith(prefix):
                    return data if isinstance(data, subprocess.CompletedProcess) else proc(data)
            return proc({"message": "Not Found"}, rc=1, stderr="gh: Not Found (HTTP 404)")

        return mock.patch("github.subprocess.run", side_effect=run)

    def test_repo_validation(self):
        for bad in ("", "openai", "a/b/c", "../x", "a b/c"):
            with self.subTest(repo=bad):
                self.assertEqual(github.call_tool("github_repo", {"repo": bad})["status"], "error")
        self.assertEqual(github._repo("https://github.com/openai/codex.git"), "openai/codex")

    def test_repo_info(self):
        readme = base64.b64encode(("# 标题\n" + "x" * 5000).encode()).decode()
        routes = [("repos/o/r/releases/latest", {"tag_name": "v1.2", "name": "", "body": "notes"}),
                  ("repos/o/r/readme", {"content": readme}),
                  ("repos/o/r", {"full_name": "o/r", "stargazers_count": 5, "topics": ["a"], "license": None})]
        with self.run_gh(routes):
            r = github.call_tool("github_repo", {"repo": "o/r"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["latest_release"]["tag"], "v1.2")
        self.assertTrue(r["readme"].startswith("# 标题"))
        self.assertEqual(len(r["readme"]), github.README_CHARS + 1)
        # 没有 release、没有 README 时不算错
        with self.run_gh([("repos/o/r/", proc({"message": "Not Found"}, rc=1)), ("repos/o/r", {"full_name": "o/r"})]):
            r = github.call_tool("github_repo", {"repo": "o/r"})
        self.assertEqual((r["status"], r["latest_release"], r["readme"]), ("ok", None, ""))

    def test_api_error_message(self):
        with self.run_gh([]):
            r = github.call_tool("github_repo", {"repo": "o/r"})
        self.assertEqual(r["status"], "error")
        self.assertIn("Not Found", r["detail"])
        with mock.patch("github.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 20)):
            self.assertEqual(github.call_tool("github_repo", {"repo": "o/r"})["status"], "error")

    def test_search_kinds(self):
        items = [{"number": 1, "title": "bug", "state": "open", "user": {"login": "a"},
                  "repository_url": "https://api.github.com/repos/o/r", "pull_request": {"merged_at": "2024-01-01T00:00:00Z"}}]
        with self.run_gh([("search/issues", {"total_count": 1, "items": items})]):
            r = github.call_tool("github_search", {"kind": "issues", "query": "is:pr label:bug", "count": 0})
        self.assertIn("q=is%3Apr%20label%3Abug&per_page=10", self.calls[0][0][4])
        row = r["results"][0]
        self.assertEqual((row["repo"], row["is_pr"], row["state"]), ("o/r", True, "merged"))
        self.assertEqual(github.call_tool("github_search", {"kind": "users", "query": "x", "count": 0})["status"],
                         "error")

    def test_issue_comments_latest(self):
        comments = [{"user": {"login": f"u{i}"}, "body": f"c{i}", "created_at": "2024-01-01T00:00:00Z"} for i in range(25)]

        def page(p, per):
            return comments[(p - 1) * per:p * per]

        routes = [("repos/o/r/issues/5/comments?per_page=10&page=3", page(3, 10)),
                  ("repos/o/r/issues/5/comments?per_page=10&page=2", page(2, 10)),
                  ("repos/o/r/issues/5", {"number": 5, "title": "t", "state": "open", "comments": 25,
                                          "body": "b" * 9000, "user": {"login": "op"}})]
        with self.run_gh(routes):
            r = github.call_tool("github_issue", {"repo": "o/r", "number": 5, "max_comments": 10})
        self.assertEqual([c["text"] for c in r["comments"]], [f"c{i}" for i in range(15, 25)])
        self.assertEqual(len(r["issue"]["body"]), github.BODY_CHARS + 1)
        self.assertEqual(r["comments_total"], 25)

    def test_list_issues_excludes_prs(self):
        data = [{"number": 1, "title": "pr", "pull_request": {}}, {"number": 2, "title": "issue"}]
        with self.run_gh([("repos/o/r/issues", data)]):
            r = github.call_tool("github_list", {"repo": "o/r", "kind": "issues", "state": "open", "count": 5})
        self.assertEqual([i["number"] for i in r["items"]], [2])

    # --- 写 ---
    def write_args(self):
        return {"repo": "o/r", "number": 3, "body": "LGTM，谢谢"}

    def posts(self):
        return [c for c in self.calls if "POST" in c[0]]

    def test_comment_writes(self):
        with self.run_gh([("repos/o/r/issues/3/comments", {"html_url": "https://github.com/o/r/issues/3#c1"})]):
            r = github.call_tool("github_comment", self.write_args())
        self.assertEqual(r["status"], "posted")
        self.assertEqual(r["url"], "https://github.com/o/r/issues/3#c1")
        (cmd, kw), = self.posts()
        self.assertEqual(json.loads(kw["input"]), {"body": "LGTM，谢谢"})  # 正文走 stdin
        self.assertNotIn("LGTM，谢谢", " ".join(cmd))

    def test_create_issue_writes(self):
        args = {"repo": "o/r", "title": "崩溃", "body": "复现步骤"}
        with self.run_gh([("repos/o/r/issues", {"html_url": "https://github.com/o/r/issues/9"})]):
            r = github.call_tool("github_create_issue", args)
        self.assertEqual((r["status"], r["url"]), ("posted", "https://github.com/o/r/issues/9"))
        self.assertEqual(json.loads(self.posts()[0][1]["input"]), {"title": "崩溃", "body": "复现步骤"})

    def test_write_validation(self):
        for args in ({"repo": "o/r", "number": 3, "body": "  "}, {"repo": "bad", "number": 3, "body": "x"},
                     {"repo": "o/r", "number": 0, "body": "x"}):
            with self.subTest(args=args), self.run_gh([]):
                self.assertEqual(github.call_tool("github_comment", args)["status"], "error")
            self.assertEqual(self.posts(), [])

    def test_available_cached(self):
        github._available = None
        with mock.patch("github.shutil.which", return_value="/usr/bin/gh"), \
                mock.patch("github.subprocess.run", return_value=proc("", rc=0)) as run:
            self.assertTrue(github.available())
            self.assertTrue(github.available())
        self.assertEqual(run.call_count, 1)
        github._available = None
        with mock.patch("github.shutil.which", return_value=None):
            self.assertFalse(github.available())
        github._available = None


if __name__ == "__main__":
    unittest.main()
