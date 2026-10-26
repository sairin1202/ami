"""papers.py（Hugging Face 论文 / 模型、arXiv 网页和 RSS）的离线测试：mock 掉 requests.get，不联网。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import requests  # noqa: E402

import papers  # noqa: E402


def resp(data=None, status=200, text=""):
    r = mock.Mock(status_code=status, text=text, content=text.encode())
    r.json.return_value = data
    return r


def hf_item(aid, upvotes, day="2026-10-08", **extra):
    paper = {"id": aid, "title": f"Paper {aid}", "upvotes": upvotes, "summary": "s" * 1000,
             "authors": [{"name": f"A{i}"} for i in range(8)], "publishedAt": "2026-10-06T20:00:00.000Z",
             "submittedOnDailyAt": f"{day}T00:00:00.000Z", **extra}
    return {"paper": paper, "title": paper["title"], "numComments": 2}


ABS = """<html><head>
<meta name="citation_title" content="MemGPT: Towards LLMs as Operating Systems" />
<meta name="citation_author" content="Packer, Charles" /><meta name="citation_author" content="Stoica, Ion" />
<meta name="citation_date" content="2023/10/12" />
<meta name="citation_pdf_url" content="https://arxiv.org/pdf/2310.08560" />
<meta name="citation_arxiv_id" content="2310.08560" />
<meta name="citation_abstract" content="Large language models &amp; the LLM&#39;s limited
 context window." />
</head><body>
<table summary="Additional metadata"><tr>
  <td class="tablecell label">Comments:</td>
  <td class="tablecell comments mathjax">Code at <a href="https://research.memgpt.ai" rel="external">this https URL</a></td>
</tr><tr><td class="tablecell label">Subjects:</td>
  <td class="tablecell subjects"><span class="primary-subject">Artificial Intelligence (cs.AI)</span></td></tr>
</table>
<div class="submission-history">
  <h2>Submission history</h2> From: Charles Packer <br/>
  <strong><a href="/abs/2310.08560v1" rel="nofollow">[v1]</a></strong>
    Thu, 12 Oct 2023 17:51:32 UTC (391 KB)<br/>
  <strong>[v2]</strong>
    Mon, 12 Feb 2024 18:59:46 UTC (419 KB)<br/>
</div>
<li><a href="/pdf/2310.08560" class="abs-button download-pdf">View PDF</a></li>
<li><a href="https://arxiv.org/html/2310.08560v2" class="abs-button" id="latexml-download-link">HTML</a></li>
</body></html>"""

RSS = """<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <title>cs.AI updates on arXiv.org</title>
    <pubDate>Wed, 07 Oct 2026 00:00:00 -0400</pubDate>
    <item>
      <title>GAMEGO: Training Game-Dev Agents</title>
      <link>https://arxiv.org/abs/2610.06910</link>
      <description>arXiv:2610.06910v1 Announce Type: new
Abstract: Recent advances in Large Language Models (LLMs) have demonstrated
 remarkable capabilities.</description>
      <category>cs.AI</category><category>cs.SE</category>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Haoyue Yang, Jingyao Li, Zhengfan Wu, Jing Liu, Xuanle Zhao, Kang Liu</dc:creator>
    </item>
    <item>
      <title>Older Work, New Version</title>
      <link>https://arxiv.org/abs/2501.00001</link>
      <description>arXiv:2501.00001v3 Announce Type: replace
Abstract: Short.</description>
      <category>cs.LG</category>
      <arxiv:announce_type>replace</arxiv:announce_type>
      <dc:creator>Solo Author</dc:creator>
    </item>
  </channel>
</rss>"""

FEED_ERROR = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel><title>Feed error for query: http://rss.arxiv.org/rss/cs.XX</title></channel></rss>"""


class SchemaTest(unittest.TestCase):
    def test_strict_schema(self):
        for t in papers.TOOLS:
            with self.subTest(tool=t["name"]):
                p = t["parameters"]
                self.assertTrue(t["strict"])
                self.assertEqual(t["type"], "function")
                self.assertEqual(sorted(p["required"]), sorted(p["properties"]))
                self.assertIs(p["additionalProperties"], False)
                for spec in p["properties"].values():
                    self.assertIn("description", spec)

    def test_module_interface(self):
        self.assertTrue(papers.RULES.startswith("\n- "))
        self.assertIn("数据", papers.RULES)
        self.assertEqual(papers.WRITE_TOOLS, set())
        self.assertTrue(papers.available())
        for t in papers.TOOLS:
            self.assertIsInstance(papers.describe_call(t["name"], {}), str)
        self.assertEqual(papers.call_tool("no_such_tool", {})["status"], "error")

    def test_jev_skip(self):
        for s in ("总结一下今天 HF 上的论文", "看看 arXiv cs.CL 最新", "Hugging Face 热门模型", "huggingface trending",
                  "搜 agent memory 的 papers"):
            with self.subTest(s=s):
                self.assertTrue(papers.JEV_SKIP.search(s))
        for s in ("打开备忘录", "给张三发微信", "open newspapers app", "把 shf 文件夹压缩"):
            with self.subTest(s=s):
                self.assertFalse(papers.JEV_SKIP.search(s))


class IdTest(unittest.TestCase):
    def test_normalize(self):
        cases = {"2310.08560": ("2310.08560", ""), "2310.08560v2": ("2310.08560", "v2"),
                 "arXiv:2310.08560": ("2310.08560", ""), " https://arxiv.org/abs/2310.08560v2 ": ("2310.08560", "v2"),
                 "https://arxiv.org/pdf/2310.08560v1.pdf": ("2310.08560", "v1"),
                 "https://arxiv.org/html/2310.08560v2/": ("2310.08560", "v2"),
                 "https://huggingface.co/papers/2310.08560?foo=1": ("2310.08560", ""),
                 "cs/0112017": ("cs/0112017", ""), "https://arxiv.org/abs/hep-th/9901001v1": ("hep-th/9901001", "v1")}
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(papers.arxiv_id(s), want)

    def test_bad_id(self):
        for bad in ("", "memgpt", "2310.085", "https://example.com/abs/2310.08560x"):
            with self.subTest(s=bad):
                r = papers.call_tool("paper_detail", {"id_or_url": bad})
                self.assertEqual(r["status"], "error")


class DailySearchTest(unittest.TestCase):
    def test_daily_latest_keeps_newest_day_sorted(self):
        data = [hf_item("2610.00001", 3), hf_item("2610.00002", 50, ai_summary="短摘要", ai_keywords=list("abcdefghij"),
                                                   githubRepo="https://github.com/o/r"),
                hf_item("2610.00003", 99, day="2026-10-07")]
        with mock.patch("papers.requests.get", return_value=resp(data)) as g:
            r = papers.call_tool("papers_daily", {"date": "", "count": 0})
        self.assertNotIn("date", g.call_args.kwargs["params"])
        self.assertEqual(r["date"], "2026-10-08")
        self.assertEqual([p["id"] for p in r["papers"]], ["2610.00002", "2610.00001"])  # 前一天的不混进来
        p = r["papers"][0]
        self.assertEqual(p["authors"], "A0, A1, A2, A3, A4 等 8 人")
        self.assertEqual(len(p["summary"]), papers.SNIPPET_CHARS + 1)
        self.assertEqual(len(p["keywords"]), 8)
        self.assertEqual((p["github"], p["comments"]), ("https://github.com/o/r", 2))
        self.assertEqual(p["pdf"], "https://arxiv.org/pdf/2610.00002")
        self.assertEqual(p["hf"], "https://huggingface.co/papers/2610.00002")
        self.assertIn("note", r)

    def test_daily_date(self):
        with mock.patch("papers.requests.get", return_value=resp([])) as g:
            r = papers.call_tool("papers_daily", {"date": "2026-10-04", "count": 5})
        self.assertEqual(g.call_args.kwargs["params"]["date"], "2026-10-04")
        self.assertEqual((r["status"], r["count"]), ("ok", 0))
        self.assertIn("没有论文", r["note"])
        self.assertEqual(papers.call_tool("papers_daily", {"date": "10/04", "count": 5})["status"], "error")

    def test_search(self):
        data = [hf_item(f"2512.1281{i}", i) for i in range(5)]
        with mock.patch("papers.requests.get", return_value=resp(data)) as g:
            r = papers.call_tool("papers_search", {"query": "agent memory", "count": 3})
        self.assertEqual(g.call_args.kwargs["params"], {"q": "agent memory", "limit": 3})
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["papers"][0]["id"], "2512.12810")  # 保持相关度顺序
        self.assertEqual(papers.call_tool("papers_search", {"query": " ", "count": 0})["status"], "error")

    def test_http_error_and_timeout(self):
        with mock.patch("papers.requests.get", return_value=resp({"error": "Invalid ISO date"}, 400)):
            r = papers.call_tool("papers_daily", {"date": "", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("400", r["detail"])
        self.assertIn("Invalid ISO date", r["detail"])
        with mock.patch("papers.requests.get", side_effect=requests.Timeout("slow")):
            r = papers.call_tool("papers_search", {"query": "x", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("连不上", r["detail"])


HF_PAPER = {"id": "2310.08560", "title": "MemGPT", "upvotes": 9, "summary": "hf abstract",
            "authors": [{"name": "Charles Packer"}], "publishedAt": "2023-10-12T17:51:32.000Z",
            "ai_summary": "AI 摘要", "ai_keywords": ["memory tiers"], "numTotalModels": 0, "numTotalDatasets": 8,
            "numTotalSpaces": 6}


class DetailTest(unittest.TestCase):
    def route(self, hf=None, abs_page=None):
        def get(url, params=None, **kw):
            if "huggingface.co/api/papers/" in url:
                return hf if isinstance(hf, mock.Mock) else resp(hf) if hf else resp({"error": "Paper not found"}, 404)
            if isinstance(abs_page, Exception):
                raise abs_page
            return resp(text=abs_page) if abs_page else resp(status=404, text="not found")
        return mock.patch("papers.requests.get", side_effect=get)

    def test_parse_abs(self):
        info = papers.parse_abs(ABS)
        self.assertEqual(info["title"], "MemGPT: Towards LLMs as Operating Systems")
        self.assertEqual(info["authors"], ["Charles Packer", "Ion Stoica"])
        self.assertEqual(info["abstract"], "Large language models & the LLM's limited context window.")
        self.assertEqual(info["submitted"], "2023-10-12")
        self.assertEqual(info["versions"], [{"version": "v1", "date": "Thu, 12 Oct 2023 17:51:32 UTC", "size": "391 KB"},
                                            {"version": "v2", "date": "Mon, 12 Feb 2024 18:59:46 UTC", "size": "419 KB"}])
        self.assertEqual(info["comments"], "Code at https://research.memgpt.ai")
        self.assertEqual(info["primary_category"], "Artificial Intelligence (cs.AI)")
        self.assertEqual(info["html"], "https://arxiv.org/html/2310.08560v2")
        self.assertNotIn("journal_ref", info)
        with self.assertRaises(papers.PapersError):
            papers.parse_abs("<html>nothing</html>")

    def test_detail_combines_hf_and_arxiv(self):
        with self.route(HF_PAPER, ABS) as g:
            r = papers.call_tool("paper_detail", {"id_or_url": "https://arxiv.org/abs/2310.08560v2"})
        urls = [c.args[0] for c in g.call_args_list]
        self.assertIn("https://huggingface.co/api/papers/2310.08560", urls)
        self.assertIn("https://arxiv.org/abs/2310.08560v2", urls)  # 指定版本时读那个版本
        p = r["paper"]
        self.assertEqual((p["version"], p["upvotes"], p["keywords"]), ("v2", 9, ["memory tiers"]))
        self.assertEqual(p["authors"], ["Charles Packer", "Ion Stoica"])  # 以 arXiv 为准
        self.assertEqual(p["hf_linked"], {"models": 0, "datasets": 8, "spaces": 6})
        self.assertEqual(r["links"]["html"], "https://arxiv.org/html/2310.08560v2")
        self.assertEqual(r["links"]["hf"], "https://huggingface.co/papers/2310.08560")
        self.assertIn("note", r)

    def test_detail_not_on_hf(self):
        with self.route(None, ABS):
            r = papers.call_tool("paper_detail", {"id_or_url": "2310.08560"})
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("upvotes", r["paper"])
        self.assertNotIn("hf", r["links"])

    def test_detail_arxiv_down_falls_back_to_hf(self):
        with self.route(HF_PAPER, requests.ConnectionError("timeout")):
            r = papers.call_tool("paper_detail", {"id_or_url": "2310.08560"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["paper"]["abstract"], "hf abstract")
        self.assertIn("连不上", r["paper"]["arxiv_error"])

    def test_detail_both_missing(self):
        with self.route(None, None):
            r = papers.call_tool("paper_detail", {"id_or_url": "2310.99999"})
        self.assertEqual(r["status"], "error")
        self.assertIn("arXiv 上没有", r["detail"])

    def test_long_abstract_and_many_authors(self):
        page = ABS.replace("context window.", "x" * 5000) + "".join(
            f'<meta name="citation_author" content="Z, A{i}" />' for i in range(50))
        with self.route(None, page):
            r = papers.call_tool("paper_detail", {"id_or_url": "2310.08560"})
        self.assertEqual(len(r["paper"]["abstract"]), papers.ABSTRACT_CHARS + 1)
        self.assertEqual(len(r["paper"]["authors"]), 31)
        self.assertEqual(r["paper"]["authors"][-1], "…共 52 人")


class ArxivNewTest(unittest.TestCase):
    def test_rss(self):
        with mock.patch("papers.requests.get", return_value=resp(text=RSS)) as g:
            r = papers.call_tool("arxiv_new", {"category": "cs.AI", "count": 0})
        self.assertEqual(g.call_args.args[0], "https://rss.arxiv.org/rss/cs.AI")
        self.assertEqual((r["status"], r["total"], r["count"]), ("ok", 2, 2))
        self.assertEqual(r["by_type"], {"new": 1, "replace": 1})
        p = r["papers"][0]
        self.assertEqual((p["id"], p["type"]), ("2610.06910", "new"))
        self.assertEqual(p["authors"], "Haoyue Yang, Jingyao Li, Zhengfan Wu, Jing Liu, Xuanle Zhao 等 6 人")
        self.assertEqual(p["categories"], ["cs.AI", "cs.SE"])
        self.assertEqual(p["abstract"], "Recent advances in Large Language Models (LLMs) have demonstrated remarkable capabilities.")
        self.assertEqual(p["arxiv"], "https://arxiv.org/abs/2610.06910")
        with mock.patch("papers.requests.get", return_value=resp(text=RSS)):
            r = papers.call_tool("arxiv_new", {"category": "cs.AI", "count": 1})
        self.assertEqual((r["count"], r["total"]), (1, 2))

    def test_category_validation(self):
        for ok in ("cs.AI", "stat.ML", "astro-ph.GA", "physics", "cs.AI+cs.CL", "https://arxiv.org/list/cs.LG"):
            with self.subTest(c=ok):
                self.assertTrue(papers._category(ok))
        self.assertEqual(papers._category("https://arxiv.org/list/cs.LG"), "cs.LG")
        with mock.patch("papers.requests.get") as g:
            for bad in ("", "cs AI", "../x", "cs.AI/new", "CS.AI?x=1"):
                with self.subTest(c=bad):
                    self.assertEqual(papers.call_tool("arxiv_new", {"category": bad, "count": 0})["status"], "error")
            g.assert_not_called()

    def test_feed_error_and_bad_xml(self):
        with mock.patch("papers.requests.get", return_value=resp(text=FEED_ERROR)):
            r = papers.call_tool("arxiv_new", {"category": "cs.XX", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("cs.XX", r["detail"])
        with mock.patch("papers.requests.get", return_value=resp(text="<html>oops")):
            self.assertEqual(papers.call_tool("arxiv_new", {"category": "cs.AI", "count": 0})["status"], "error")
        with mock.patch("papers.requests.get", return_value=resp(status=503)):
            self.assertEqual(papers.call_tool("arxiv_new", {"category": "cs.AI", "count": 0})["status"], "error")

    def test_empty_weekend(self):
        empty = RSS.split("<item>")[0] + "</channel></rss>"
        with mock.patch("papers.requests.get", return_value=resp(text=empty)):
            r = papers.call_tool("arxiv_new", {"category": "cs.AI", "count": 0})
        self.assertEqual((r["status"], r["count"]), ("ok", 0))
        self.assertIn("周末", r["note"])


class HFTrendingTest(unittest.TestCase):
    def test_models(self):
        data = [{"id": "o/m", "likes": 5, "downloads": 100, "pipeline_tag": "text-generation", "library_name": "transformers",
                 "tags": ["transformers", "region:us", "base_model:x", "license:apache-2.0"],
                 "lastModified": "2020-01-02T03:04:05.000Z"}]
        with mock.patch("papers.requests.get", return_value=resp(data)) as g:
            r = papers.call_tool("hf_trending", {"kind": "models", "query": "qwen", "count": 50})
        self.assertEqual(g.call_args.args[0], "https://huggingface.co/api/models")
        params = g.call_args.kwargs["params"]
        self.assertIn(("sort", "trendingScore"), params)
        self.assertIn(("limit", papers.MAX_TRENDING), params)
        self.assertIn(("search", "qwen"), params)
        self.assertIn(("expand[]", "lastModified"), params)
        it = r["items"][0]
        self.assertEqual(it["tags"], ["transformers", "license:apache-2.0"])
        self.assertEqual((it["task"], it["downloads"], it["updated"]), ("text-generation", 100, "2020-01-02"))
        self.assertEqual(it["url"], "https://huggingface.co/o/m")

    def test_spaces_and_datasets(self):
        data = [{"id": "u/s", "likes": 3, "sdk": "gradio", "tags": [],
                 "cardData": {"title": "Demo", "short_description": "做视频"}}]
        with mock.patch("papers.requests.get", return_value=resp(data)) as g:
            r = papers.call_tool("hf_trending", {"kind": "spaces", "query": "", "count": 0})
        self.assertNotIn("search", dict(g.call_args.kwargs["params"]))
        it = r["items"][0]
        self.assertEqual((it["title"], it["description"], it["sdk"]), ("Demo", "做视频", "gradio"))
        self.assertNotIn("downloads", it)
        self.assertEqual(it["url"], "https://huggingface.co/spaces/u/s")
        with mock.patch("papers.requests.get", return_value=resp([{"id": "u/d"}])):
            r = papers.call_tool("hf_trending", {"kind": "datasets", "query": "", "count": 0})
        self.assertEqual(r["items"][0]["url"], "https://huggingface.co/datasets/u/d")

    def test_bad_kind_and_errors(self):
        self.assertEqual(papers.call_tool("hf_trending", {"kind": "papers", "query": "", "count": 0})["status"],
                         "error")
        with mock.patch("papers.requests.get", side_effect=requests.ConnectionError("down")):
            r = papers.call_tool("hf_trending", {"kind": "models", "query": "", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("连不上", r["detail"])
        with mock.patch("papers.requests.get", return_value=resp({"error": "boom"}, 500)):
            self.assertEqual(papers.call_tool("hf_trending", {"kind": "models", "query": "", "count": 0})["status"],
                             "error")


if __name__ == "__main__":
    unittest.main()
