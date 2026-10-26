"""X 刷推荐 / LinkedIn 搜索的解析逻辑：离线测试，数据取自真实页面的辅助功能文字，不打开浏览器。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))
os.environ["TYPESAFE_API_KEY"] = ""

import browser  # noqa: E402
import linkedin  # noqa: E402
import tools  # noqa: E402
import twitter  # noqa: E402

URL = "https://x.com/Huanusa/status/2107477728559878411"


class XParseTest(unittest.TestCase):
    def test_post_with_quote_and_stats(self):
        texts = ["Huan", "@Huanusa", "10月6日", "清华心理系的学生，用9分钟！", "8:53", "引用", "RiveX", "@RivexPro",
                 "10月6日", "翻译自 英语", "下一代加密货币图表和市场分析平台。", "rivexcore.com", "#RiveX",
                 "111", "514", "2,169", "13万"]
        p = twitter.parse(texts, URL)
        self.assertEqual((p["author"], p["handle"], p["time"]), ("Huan", "@Huanusa", "10月6日"))
        self.assertEqual(p["text"], "清华心理系的学生，用9分钟！")  # 视频时长、翻译提示去掉
        self.assertEqual(p["quoted"]["handle"], "@RivexPro")
        self.assertIn("rivexcore.com #RiveX", p["quoted"]["text"])
        self.assertEqual((p["replies"], p["reposts"], p["likes"], p["views"]), ("111", "514", "2,169", "13万"))

    def test_ad_skipped_and_repost_context(self):
        self.assertIsNone(twitter.parse(["Jakob", "@jakobgreenfeld", "広告", "Type your website."], URL))
        p = twitter.parse(["向阳乔木 已转帖", "花叔", "@AlchainHust", "21時間", "正文", "3", "21", "153", "1.4万"], URL)
        self.assertEqual(p["context"], "向阳乔木 已转帖")
        self.assertEqual(p["author"], "花叔")

    def test_missing_counts_kept_raw(self):
        p = twitter.parse(["samim", "@samim", "15時間", "850k views", "12", "3"], URL)
        self.assertEqual(p["stats"], "12 / 3")
        self.assertNotIn("likes", p)

    def test_no_handle_is_not_a_post(self):
        self.assertIsNone(twitter.parse(["本日のニュース", "Close"], URL))


class LinkedInTest(unittest.TestCase):
    def test_kinds_and_keys(self):
        self.assertEqual(set(linkedin.KINDS), {"people", "companies", "posts", "jobs"})
        post = {"kind": "posts", "url": "https://www.linkedin.com/in/a", "text": "A | 正文一"}
        other = dict(post, text="A | 正文二")
        self.assertNotEqual(linkedin._key(post), linkedin._key(other))  # 同一作者的两条帖子不能当成一条
        self.assertEqual(linkedin._key({"kind": "people", "url": "u", "text": "x"}), "u")
        self.assertTrue(linkedin.AD_TEXT.search("Salesforce | Promoted | Learn more"))
        self.assertTrue(linkedin.POST_HEADING.match("Feed post number 3"))

    def test_bad_input(self):
        self.assertEqual(linkedin.call_tool("linkedin_search", {"kind": "groups", "keywords": "x", "count": 1})["status"], "error")
        self.assertEqual(linkedin.call_tool("linkedin_search", {"kind": "people", "keywords": " ", "count": 1})["status"], "error")

    def test_paged_search_stops_at_count(self):
        pages = [[{"url": f"https://www.linkedin.com/in/p{i}", "text": f"P{i}", "title": f"P{i}", "kind": "people"}
                  for i in range(10 * k, 10 * k + 10)] for k in range(3)]
        it = iter(pages)
        with mock.patch.object(linkedin.browser, "open_url") as op, \
                mock.patch.object(linkedin.browser, "front_browser", return_value=mock.Mock(processIdentifier=lambda: 1)), \
                mock.patch.object(linkedin.axui, "_enable_web_ax"), mock.patch.object(linkedin.axui, "_main_window"), \
                mock.patch.object(linkedin, "_wait_cards", side_effect=lambda *a: (next(it), None)):
            r = linkedin.search("people", "AI agent memory", 15)
        self.assertEqual(r["count"], 15)
        self.assertNotIn("kind", r["results"][0])
        self.assertIn("page=2", op.call_args_list[-1].args[0])  # 人按页翻，不滚动


class RegistryTest(unittest.TestCase):
    def test_tools_registered(self):
        names = [t["name"] for t in tools.specs()]
        self.assertIn("x_feed", names)
        self.assertIn("linkedin_search", names)
        for t in twitter.TOOLS + linkedin.TOOLS:
            self.assertEqual(set(t["parameters"]["required"]), set(t["parameters"]["properties"]))
        self.assertIn("x_feed", tools.rules())
        self.assertIn("linkedin_search", tools.rules())


if __name__ == "__main__":
    unittest.main()
