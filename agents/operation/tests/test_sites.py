"""sites.py 的网站配置：链接识别 / 去重 / 噪音过滤、发布按钮、频率上限。离线测试，不打开浏览器。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))
os.environ["TYPESAFE_API_KEY"] = ""

import browser  # noqa: E402
import sites  # noqa: E402
import tools  # noqa: E402

STATUS = "https://x.com/denziideng/status/2107252757698908360"


class SitesTest(unittest.TestCase):
    def test_every_site_content_pattern_matches_an_example(self):
        examples = {
            "reddit.com": "https://www.reddit.com/r/test/comments/1wzlnr7/test_post/",
            "linux.do": "https://linux.do/t/topic/2989619/8",
            "rednote.com": "https://www.rednote.com/discovery/item/6ac1d0c4000000000200e061?xsec_token=AB",
            "x.com": STATUS,
            "zhihu.com": "https://www.zhihu.com/question/2091112305123767019/answer/123",
            "linkedin.com": "https://www.linkedin.com/feed/update/urn:li:activity:7513259005318725632/",
            "weibo.com": "https://weibo.com/1234567890/PaBcD1234",
            "bilibili.com": "https://www.bilibili.com/video/BV1xx411c7mD",
            "weixin.sogou.com": "https://weixin.sogou.com/link?url=dn9a_-gY295K0Rci&type=2",
            "okjike.com": "https://web.okjike.com/originalPost/5f0d6a3e8f1c2b0017e1c2d3",
            "v2ex.com": "https://www.v2ex.com/t/1234567#reply3",
            "sspai.com": "https://sspai.com/post/117460",
            "juejin.cn": "https://juejin.cn/post/7301234567890123456",
        }
        self.assertEqual(set(examples), {s.key for s in sites.SITES})
        for key, url in examples.items():
            self.assertEqual(sites.site_for(url).key, key, url)
            self.assertTrue(browser.CONTENT_LINK.search(url), url)

    def test_x_subpages_dedupe_and_noise(self):
        keys = {browser._link_key(u) for u in (STATUS, STATUS + "/analytics", STATUS + "/photo/1",
                                               STATUS.replace("x.com", "twitter.com").replace("x.com", "x.com"))}
        self.assertEqual(len({k.replace("twitter.com", "x.com") for k in keys}), 1)
        self.assertTrue(browser._skip_link("31858 件の表示", STATUS + "/analytics"))
        self.assertTrue(browser._skip_link("ホーム", "https://x.com/home"))
        self.assertFalse(browser._skip_link("10月6日", STATUS))
        self.assertFalse(browser.CONTENT_LINK.search("https://x.com/i/premium_sign_up"))

    def test_noise_per_site(self):
        self.assertTrue(browser._skip_link("Hong Chen", "https://www.linkedin.com/in/hong-chen-5785a5175/"))
        self.assertTrue(browser._skip_link("小阿柒", "https://www.zhihu.com/people/14-68-12-42-45"))
        self.assertFalse(browser._skip_link("问题", "https://www.zhihu.com/question/2091112305123767019"))
        self.assertFalse(browser._skip_link("别的网站", "https://example.com/people/1"))
        self.assertTrue(browser._skip_link("广告视频", "https://cm.bilibili.com/cm/api/fees/pc/sync/v2?msg=a"))
        self.assertFalse(browser._skip_link("视频", "https://www.bilibili.com/video/BV1wDhj6wEa8/"))

    def test_same_site_across_redirect_hosts(self):
        self.assertTrue(browser._same_site("https://mp.weixin.qq.com/s/abc", "weixin.sogou.com"))
        self.assertTrue(browser._same_site("https://twitter.com/a", "x.com"))
        self.assertFalse(browser._same_site("https://dearly.im/book/1", "weixin.sogou.com"))

    def test_site_aliases(self):
        self.assertEqual(sites.site_for("https://twitter.com/a/status/1").key, "x.com")
        self.assertEqual(sites.site_for("https://creator.rednote.com/publish").key, "rednote.com")
        self.assertEqual(sites.site_for("https://mp.weixin.qq.com/s/abc").key, "weixin.sogou.com")
        self.assertIsNone(sites.site_for("https://example.com/x"))

    def test_registry(self):
        names = [t["name"] for t in tools.specs()]
        for n in ("browser_open", "browser_read_page", "browser_load_more", "rednote_feed", "hn_stories"):
            self.assertIn(n, names)
        self.assertEqual(len(names), len(set(names)))  # 工具名不重复
        for t in tools.specs():
            if t.get("strict"):
                self.assertEqual(set(t["parameters"]["required"]), set(t["parameters"]["properties"]), t["name"])
        for task in ("看看知乎热榜", "刷一下推特", "LinkedIn 上有什么新动态", "搜一下公众号文章"):
            self.assertTrue(tools.skip_jev(task), task)
        self.assertFalse(tools.skip_jev("打开备忘录"))
        self.assertTrue({"rednote_publish", "wechat_send"} <= tools.write_tools())
        self.assertIn("browser_load_more", tools.rules())


class LoadMoreTest(unittest.TestCase):
    def test_reports_new_posts(self):
        app = mock.Mock()
        app.processIdentifier.return_value = 1
        pages = iter([({"a"}, object()), ({"a", "b", "c"}, object())])
        with mock.patch.object(browser, "front_browser", return_value=app), \
                mock.patch.object(browser, "_page_window", return_value=(None, object())), \
                mock.patch.object(browser, "_content_links", side_effect=lambda area: next(pages)), \
                mock.patch.object(browser.AS, "AXUIElementPerformAction") as act, \
                mock.patch.object(browser.time, "sleep"):
            r = browser.call_tool("browser_load_more", {})
        self.assertEqual(r["new_posts"], 2)
        act.assert_called_once()


if __name__ == "__main__":
    unittest.main()
