"""Exa 网页搜索、RSS 阅读器的离线测试：mock 掉 requests，不联网；订阅列表写在临时目录。"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["COMPUTER_USE_HOME"] = tempfile.mkdtemp(prefix="cu-test-")  # 不写真实目录

import requests  # noqa: E402

import exa  # noqa: E402
import rss  # noqa: E402

MODULES = [exa, rss]


def resp(data=None, status=200, content=b"", url="", ctype="application/xml"):
    r = mock.Mock(status_code=status, content=content, url=url, headers={"Content-Type": ctype})
    if isinstance(data, Exception):
        r.json.side_effect = data
    else:
        r.json.return_value = data
    r.text = data if isinstance(data, str) else ""
    return r


def ago(hours: float, fmt: str = "rfc") -> str:
    d = datetime.now(timezone.utc) - timedelta(hours=hours)
    return d.strftime("%a, %d %b %Y %H:%M:%S +0000") if fmt == "rfc" else d.strftime("%Y-%m-%dT%H:%M:%SZ")


RSS2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Demo Blog</title>
  <link>https://blog.example.com/</link>
  <atom:link href="https://blog.example.com/feed" rel="self" type="application/rss+xml"/>
  <item>
    <title><![CDATA[New <b>post</b> & more]]></title>
    <link>https://blog.example.com/new</link>
    <pubDate>{ago(2)}</pubDate>
    <dc:creator>Alice</dc:creator>
    <description><![CDATA[<p>Hello <a href="https://x.com">world</a></p><script>bad()</script>&nbsp;tail]]></description>
  </item>
  <item>
    <title>Old post</title>
    <link>https://blog.example.com/old</link>
    <pubDate>{ago(72)}</pubDate>
    <author>bob@example.com (Bob)</author>
    <description>&lt;p&gt;escaped &amp;amp; html&lt;/p&gt;</description>
  </item>
  <item>
    <title>No date</title>
    <guid>https://blog.example.com/nodate</guid>
    <description>{"x" * 500}</description>
  </item>
</channel>
</rss>""".encode()

ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from repo</title>
  <link rel="alternate" type="text/html" href="https://github.com/o/r/releases"/>
  <link rel="self" href="https://github.com/o/r/releases.atom"/>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.2.0</id>
    <updated>{ago(1, "iso")}</updated>
    <link rel="alternate" type="text/html" href="/o/r/releases/tag/v1.2.0"/>
    <title>v1.2.0</title>
    <content type="html">&lt;h2&gt;Features&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;faster&lt;/li&gt;&lt;/ul&gt;</content>
    <author><name>octocat</name></author>
  </entry>
  <entry>
    <title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Old <em>one</em></div></title>
    <link href="https://github.com/o/r/releases/tag/v1.0.0"/>
    <published>2020-01-01T00:00:00Z</published>
    <summary>first release</summary>
  </entry>
</feed>""".encode()

RDF = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.org/">
    <title>RDF Feed</title>
    <link>https://example.org/</link>
  </channel>
  <item rdf:about="https://example.org/a">
    <title>Paper A</title>
    <link>https://example.org/a</link>
    <dc:creator>Smith, J.</dc:creator>
    <dc:date>2024-05-01T10:00:00+02:00</dc:date>
    <description>Abstract text</description>
  </item>
</rdf:RDF>"""

HTML_PAGE = b"""<!DOCTYPE html><html><head><title>Blog</title>
<link rel="stylesheet" href="/s.css">
<link rel="alternate" type="application/atom+xml" title="Atom" href="/atom/everything/">
<link href='/rss.xml' type='application/rss+xml' rel='alternate'>
</head><body>hi</body></html>"""


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
                self.assertEqual(m.WRITE_TOOLS, set())
                for t in m.TOOLS:
                    self.assertIsInstance(m.describe_call(t["name"], {}), str)
                self.assertEqual(m.call_tool("no_such_tool", {})["status"], "error")
        self.assertEqual({t["name"] for t in exa.TOOLS}, {"exa_search", "exa_contents", "exa_answer"})
        self.assertEqual({t["name"] for t in rss.TOOLS}, {"rss_read", "rss_subscribe", "rss_unsubscribe", "rss_digest"})

    def test_jev_skip(self):
        for s in ["上网搜一下 GPT-5 的发布时间", "帮我搜索网上关于 M5 芯片的评测", "查一下最新新闻", "search the web for rust 2026",
                  "用 exa 查一下"]:
            self.assertTrue(exa.JEV_SKIP.search(s), s)
        for s in ["打开备忘录", "搜索文件 report.pdf", "在访达里搜索下载文件夹", "查一下微信消息", "open example.com"]:
            self.assertFalse(exa.JEV_SKIP.search(s), s)
        for s in ["读一下这个 RSS", "看看我的订阅源有什么更新", "Simon 的博客更新了吗", "读 substack 上的文章",
                  "arXiv cs.AI 今天的新论文"]:
            self.assertTrue(rss.JEV_SKIP.search(s), s)
        for s in ["打开备忘录", "发送 feedback 给团队", "调中等亮度 medium brightness", "搜索文件"]:
            self.assertFalse(rss.JEV_SKIP.search(s), s)


class ExaTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"EXA_API_KEY": "k-test"})
        p.start()
        self.addCleanup(p.stop)

    def test_available_needs_key(self):
        self.assertTrue(exa.available())
        with mock.patch.dict(os.environ, {"EXA_API_KEY": ""}):
            self.assertFalse(exa.available())
            r = exa.call_tool("exa_search", {"query": "x", "count": 0, "recent_days": 0, "domains": "", "text_chars": 0})
        self.assertEqual(r["status"], "error")

    def test_search_body_and_result(self):
        data = {"results": [{"title": "T", "url": "https://a.com/1", "publishedDate": "2026-10-01T00:00:00.000Z",
                             "author": "A", "text": "y" * 5000, "highlights": ["hl one", ""]}]}
        with mock.patch("exa.requests.post", return_value=resp(data)) as p:
            r = exa.call_tool("exa_search", {"query": " llm agents ", "count": 50, "recent_days": 7,
                                             "domains": "https://openai.com/, arxiv.org", "text_chars": 0})
        self.assertEqual(p.call_args.args[0], "https://api.exa.ai/search")
        self.assertEqual(p.call_args.kwargs["headers"]["x-api-key"], "k-test")
        body = p.call_args.kwargs["json"]
        self.assertEqual((body["query"], body["numResults"], body["type"]), ("llm agents", 20, "auto"))
        self.assertEqual(body["includeDomains"], ["openai.com", "arxiv.org"])
        start = datetime.strptime(body["startPublishedDate"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
        self.assertAlmostEqual((datetime.now(timezone.utc) - start).total_seconds(), 7 * 86400, delta=120)
        self.assertEqual(body["contents"]["text"]["maxCharacters"], exa.SEARCH_TEXT_CHARS)
        self.assertIn("highlights", body["contents"])
        x = r["results"][0]
        self.assertEqual((x["published"], x["highlights"]), ("2026-10-01", ["hl one"]))
        self.assertEqual(len(x["text"]), exa.SEARCH_TEXT_CHARS + 1)
        self.assertIn("note", r)

    def test_search_without_filters(self):
        with mock.patch("exa.requests.post", return_value=resp({"results": []})) as p:
            exa.call_tool("exa_search", {"query": "q", "count": 0, "recent_days": 0, "domains": "", "text_chars": 99999})
        body = p.call_args.kwargs["json"]
        self.assertNotIn("startPublishedDate", body)
        self.assertNotIn("includeDomains", body)
        self.assertEqual(body["numResults"], 8)
        self.assertEqual(body["contents"]["text"]["maxCharacters"], exa.MAX_SEARCH_TEXT)

    def test_contents_and_answer(self):
        data = {"results": [{"id": "https://a.com", "url": "https://a.com", "title": "A", "text": "body"}],
                "statuses": [{"id": "https://a.com", "status": "success"},
                             {"id": "https://b.com", "status": "error", "error": {"tag": "CRAWL_NOT_FOUND"}}]}
        with mock.patch("exa.requests.post", return_value=resp(data)) as p:
            r = exa.call_tool("exa_contents", {"urls": "https://a.com, b.com", "text_chars": 0})
        self.assertEqual(p.call_args.args[0], "https://api.exa.ai/contents")
        self.assertEqual(p.call_args.kwargs["json"], {"urls": ["https://a.com", "https://b.com"],
                                                       "text": {"maxCharacters": exa.PAGE_CHARS}})
        self.assertEqual(r["pages"][0]["text"], "body")
        self.assertEqual(r["failed"], [{"url": "https://b.com", "error": "CRAWL_NOT_FOUND"}])
        too_many = ",".join(f"https://x{i}.com" for i in range(6))
        self.assertEqual(exa.call_tool("exa_contents", {"urls": too_many, "text_chars": 0})["status"], "error")

        ans = {"answer": "42", "citations": [{"title": "Src", "url": "https://s.com", "text": "long"}]}
        with mock.patch("exa.requests.post", return_value=resp(ans)) as p:
            r = exa.call_tool("exa_answer", {"query": "meaning?"})
        self.assertEqual(p.call_args.kwargs["json"], {"query": "meaning?", "text": False})
        self.assertEqual(r["answer"], "42")
        self.assertEqual(r["citations"][0]["url"], "https://s.com")

    def test_error_mapping(self):
        args = {"query": "q", "count": 0, "recent_days": 0, "domains": "", "text_chars": 0}
        with mock.patch("exa.requests.post", return_value=resp({"error": "Invalid API key"}, 401)) as p:
            r = exa.call_tool("exa_search", args)
        self.assertEqual(p.call_count, 1)
        self.assertIn("EXA_API_KEY", r["detail"])
        self.assertIn("Invalid API key", r["detail"])
        with mock.patch("exa.requests.post", return_value=resp({"error": "rate limited"}, 429)) as p:
            r = exa.call_tool("exa_search", args)
        self.assertEqual(p.call_count, 1)  # 不自动重试
        self.assertIn("429", r["detail"])
        self.assertIn("再试", r["detail"])
        with mock.patch("exa.requests.post", return_value=resp(ValueError("not json"), 500)):
            self.assertIn("500", exa.call_tool("exa_answer", {"query": "q"})["detail"])
        with mock.patch("exa.requests.post", side_effect=requests.Timeout("slow")):
            self.assertIn("连不上", exa.call_tool("exa_answer", {"query": "q"})["detail"])


class RSSParseTest(unittest.TestCase):
    def test_rss2(self):
        f = rss.parse(RSS2)
        self.assertEqual((f["title"], f["site"]), ("Demo Blog", "https://blog.example.com/"))
        a, b, c = f["items"]
        self.assertEqual(a["title"], "New post & more")
        self.assertEqual(a["author"], "Alice")
        self.assertNotIn("<", a["summary"])
        self.assertNotIn("bad()", a["summary"])
        self.assertTrue(a["summary"].startswith("Hello world"))
        self.assertTrue(a["published"])
        self.assertEqual(b["author"], "Bob")
        self.assertEqual(b["summary"], "escaped & html")
        self.assertEqual(c["link"], "https://blog.example.com/nodate")
        self.assertEqual(c["published"], "")
        self.assertEqual(len(c["summary"]), rss.SUMMARY_CHARS + 1)

    def test_atom(self):
        f = rss.parse(ATOM, base="https://github.com/o/r/releases.atom")
        self.assertEqual((f["title"], f["site"]), ("Release notes from repo", "https://github.com/o/r/releases"))
        a, b = f["items"]
        self.assertEqual(a["link"], "https://github.com/o/r/releases/tag/v1.2.0")  # 相对链接补全
        self.assertEqual((a["author"], a["summary"]), ("octocat", "Features faster"))
        self.assertEqual(b["title"], "Old one")
        self.assertEqual(b["published"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(b["summary"], "first release")

    def test_rdf(self):
        f = rss.parse(RDF)
        self.assertEqual(f["title"], "RDF Feed")
        (a,) = f["items"]
        self.assertEqual((a["title"], a["author"], a["link"]), ("Paper A", "Smith, J.", "https://example.org/a"))
        self.assertEqual(a["published"], "2024-05-01T10:00:00+02:00")

    def test_malformed_fallback_and_garbage(self):
        bad = (b'<?xml version="1.0"?><rss><channel><title>A & B\x01</title>'
               b"<item><title>x&nbsp;y</title><link>https://e.com/?a=1&b=2</link></item></channel></rss>")
        f = rss.parse(bad)
        self.assertEqual(f["title"], "A & B")
        self.assertEqual(f["items"][0]["link"], "https://e.com/?a=1&b=2")
        gbk = '<?xml version="1.0" encoding="gbk"?><rss><channel><title>中文 & 源</title></channel></rss>'.encode("gbk")
        self.assertEqual(rss.parse(gbk)["title"], "中文 & 源")
        with self.assertRaises(rss.RSSError):
            rss.parse(b"<<<not xml")
        with self.assertRaises(rss.RSSError):
            rss.parse(b"<html><body/></html>")

    def test_dates(self):
        self.assertEqual(rss._parse_date("Tue, 01 Oct 2024 12:00:00 GMT").isoformat(), "2024-10-01T12:00:00+00:00")
        self.assertEqual(rss._parse_date("2024-10-01T12:00:00.123Z").year, 2024)
        self.assertEqual(rss._parse_date("2024-10-01").day, 1)
        self.assertIsNone(rss._parse_date("yesterday"))


class RSSFetchTest(unittest.TestCase):
    def test_presets(self):
        r = rss.resolve
        self.assertEqual(r("hn"), "https://hnrss.org/frontpage")
        self.assertEqual(r("HN:rust lang"), "https://hnrss.org/newest?q=rust%20lang")
        self.assertEqual(r("github:openai/openai-python"), "https://github.com/openai/openai-python/releases.atom")
        self.assertEqual(r("github:https://github.com/o/r.git"), "https://github.com/o/r/releases.atom")
        self.assertEqual(r("youtube:UC_x5XG1OV2P6uZZ5FSM9Ttw"),
                         "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw")
        self.assertEqual(r("youtube:@veritasium"), "https://www.youtube.com/@veritasium")
        self.assertEqual(r("substack:astralcodexten"), "https://astralcodexten.substack.com/feed")
        self.assertEqual(r("medium:@karpathy"), "https://medium.com/feed/@karpathy")
        self.assertEqual(r("medium:karpathy"), "https://medium.com/feed/@karpathy")
        self.assertEqual(r("arxiv:cs.AI"), "https://rss.arxiv.org/rss/cs.AI")
        self.assertEqual(r("sspai"), "https://sspai.com/feed")
        self.assertEqual(r("36kr"), "https://www.36kr.com/feed")
        self.assertEqual(r("simonwillison.net"), "https://simonwillison.net")
        self.assertEqual(r("https://e.com/feed"), "https://e.com/feed")
        for bad in ["", "youtube:somebody", "not a url"]:
            with self.assertRaises(rss.RSSError):
                r(bad)

    def test_read_since_and_count(self):
        with mock.patch("rss.requests.get", return_value=resp(content=RSS2, url="https://blog.example.com/feed")) as g:
            r = rss.call_tool("rss_read", {"source": "https://blog.example.com/feed", "count": 0, "since_hours": 24})
        self.assertIn("Mozilla", g.call_args.kwargs["headers"]["User-Agent"])
        self.assertEqual(g.call_args.kwargs["timeout"], rss.TIMEOUT)
        self.assertEqual(r["status"], "ok")
        self.assertEqual([i["title"] for i in r["items"]], ["New post & more", "No date"])  # 旧的去掉，没日期的保留
        self.assertEqual(r["total"], 3)
        self.assertNotIn("_dt", r["items"][0])
        self.assertIn("note", r)
        with mock.patch("rss.requests.get", return_value=resp(content=RSS2)):
            r = rss.call_tool("rss_read", {"source": "https://blog.example.com/feed", "count": 1, "since_hours": 0})
        self.assertEqual(r["count"], 1)

    def test_autodiscovery(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            if url == "https://blog.example.com/":
                return resp(content=HTML_PAGE, url=url, ctype="text/html; charset=utf-8")
            if url == "https://blog.example.com/atom/everything/":
                return resp(content=ATOM, url=url)
            return resp(status=404)

        with mock.patch("rss.requests.get", side_effect=get):
            r = rss.call_tool("rss_read", {"source": "https://blog.example.com/", "count": 5, "since_hours": 0})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["url"], "https://blog.example.com/atom/everything/")
        self.assertEqual(calls, ["https://blog.example.com/", "https://blog.example.com/atom/everything/"])
        self.assertEqual(rss.discover(HTML_PAGE.decode(), "https://blog.example.com/x/"),
                         ["https://blog.example.com/atom/everything/", "https://blog.example.com/rss.xml"])

    def test_common_path_guess_and_failure(self):
        page = b"<!doctype html><html><head></head><body>no feed link</body></html>"

        def get(url, **kw):
            if url == "https://site.example/":
                return resp(content=page, url=url, ctype="text/html")
            if url == "https://site.example/feed.xml":
                return resp(content=RSS2, url=url)
            if url == "https://site.example/rss":
                raise requests.Timeout("slow")
            return resp(status=404)

        with mock.patch("rss.requests.get", side_effect=get):
            r = rss.call_tool("rss_read", {"source": "site.example/", "count": 0, "since_hours": 0})
        self.assertEqual(r["url"], "https://site.example/feed.xml")

        with mock.patch("rss.requests.get", side_effect=lambda url, **kw: resp(content=page, url=url, ctype="text/html")
                        if url == "https://site.example/" else resp(status=404)):
            r = rss.call_tool("rss_read", {"source": "https://site.example/", "count": 0, "since_hours": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("没找到", r["detail"])

        with mock.patch("rss.requests.get", side_effect=requests.ConnectionError("down")):
            r = rss.call_tool("rss_read", {"source": "hn", "count": 0, "since_hours": 0})
        self.assertIn("连不上", r["detail"])


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="cu-test-")
        p = mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": home})
        p.start()
        self.addCleanup(p.stop)
        self.file = os.path.join(home, "feeds.json")

    def test_subscribe_unsubscribe_digest(self):
        def get(url, **kw):
            if url == "https://blog.example.com/":
                return resp(content=HTML_PAGE, url=url, ctype="text/html")
            if url == "https://blog.example.com/atom/everything/":
                return resp(content=ATOM, url=url)
            if url == "https://blog.example.com/feed":
                return resp(content=RSS2, url=url)
            if url == "https://down.example/feed":
                raise requests.ConnectionError("down")
            return resp(status=404)

        with mock.patch("rss.requests.get", side_effect=get):
            r = rss.call_tool("rss_subscribe", {"name": "blog", "source": "https://blog.example.com/"})
            self.assertEqual((r["status"], r["url"]), ("ok", "https://blog.example.com/atom/everything/"))
            rss.call_tool("rss_subscribe", {"name": "rss2", "source": "https://blog.example.com/feed"})
            bad = rss.call_tool("rss_subscribe", {"name": "nope", "source": "https://blog.example.com/missing"})
            self.assertEqual(bad["status"], "error")  # 读不到就不保存
            with open(self.file, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"blog": "https://blog.example.com/atom/everything/",
                                                "rss2": "https://blog.example.com/feed"})
            subs = rss.load_subs()
            subs["down"] = "https://down.example/feed"
            rss._save_subs(subs)

            d = rss.call_tool("rss_digest", {"since_hours": 0, "per_feed": 0})
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["since_hours"], 24)
        by = {f["name"]: f for f in d["feeds"]}
        self.assertEqual([i["title"] for i in by["blog"]["items"]], ["v1.2.0"])
        self.assertEqual([i["title"] for i in by["rss2"]["items"]], ["New post & more", "No date"])
        self.assertIn("连不上", by["down"]["error"])
        self.assertEqual((d["failed"], d["total_new"]), (1, 3))

        self.assertEqual(rss.call_tool("rss_unsubscribe", {"name": "down"})["status"], "ok")
        r = rss.call_tool("rss_unsubscribe", {"name": "down"})
        self.assertEqual(r["status"], "error")
        self.assertIn("blog", r["detail"])
        self.assertEqual(set(rss.load_subs()), {"blog", "rss2"})

    def test_empty_digest(self):
        d = rss.call_tool("rss_digest", {"since_hours": 6, "per_feed": 3})
        self.assertEqual((d["status"], d["feeds"]), ("ok", []))
        self.assertIn("rss_subscribe", d["hint"])


if __name__ == "__main__":
    unittest.main()
