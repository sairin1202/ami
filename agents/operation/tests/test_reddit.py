"""Reddit（浏览器操作）的离线测试：不打开浏览器、不点击、不联网。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录
os.environ["TYPESAFE_API_KEY"] = ""

import browser  # noqa: E402
import rednote  # noqa: E402
import tools  # noqa: E402

URL = "https://www.reddit.com/r/test/comments/abc/hello/"


class BrowserToolsTest(unittest.TestCase):
    def test_registry_and_rules(self):
        names = [t["name"] for t in tools.specs()]
        self.assertIn("browser_open", names)
        self.assertIn("browser_read_page", names)
        self.assertNotIn("reddit_submit", names)  # 不用 API
        self.assertTrue(tools.skip_jev("帮我看看 r/LocalLLaMA 今天的热帖"))
        self.assertFalse(tools.skip_jev("打开备忘录"))
        self.assertIn("reddit.com/r/", tools.rules())
        for t in browser.TOOLS:  # strict 模式要求所有参数都列为必填
            self.assertEqual(set(t["parameters"]["required"]), set(t["parameters"]["properties"]))

    def test_link_filter_keeps_posts_skips_noise(self):
        sub = "https://www.reddit.com/r/LocalLLaMA"
        skip = [("Home", "https://www.reddit.com/"),
                ("Skip to main content", sub + "/hot/"),
                ("Create post", sub + "/submit/"),
                ("Create Post", sub + "/submit"),
                ("Start a conversation...", sub + "/submit?composer_entry=posting_top_of_feed"),
                ("Open inbox", "https://www.reddit.com/notifications"),
                ("Author: markpronkin", "https://www.reddit.com/user/markpronkin/"),
                ("Flair: Discussion", sub + "/?f=flair_name%3A%22Discussion%22"),
                ("Advertisement: Join Attio", "https://alb.reddit.com/cr?za=xyz"),
                ("0 Go to comments", "https://www.reddit.com/user/attio/comments/1ntp8h5/x/?p=1&impressionid=54")]
        keep = [("54gb vram for 35$", sub + "/comments/1wz6ist/54gb_vram_for_35/"),
                ("r/Vllm", "https://www.reddit.com/r/Vllm/"),
                ("gizmodo", "https://gizmodo.com/florida-woman-2000821756")]
        for label, href in skip:
            self.assertTrue(browser._skip_link(label, href), href)
        for label, href in keep:
            self.assertFalse(browser._skip_link(label, href), href)

    def test_content_links_and_topic_dedupe(self):
        self.assertTrue(browser.CONTENT_LINK.search("https://linux.do/t/topic/2989619"))
        self.assertTrue(browser.CONTENT_LINK.search(URL))
        self.assertFalse(browser.CONTENT_LINK.search("https://linux.do/c/develop/4"))
        t = "https://linux.do/t/topic/2989619"
        self.assertEqual(len({browser._link_key(u) for u in (t, t + "/1", t + "/8", t + "/8/")}), 1)

    def test_open_reuses_tab_only_on_same_site(self):
        app = mock.Mock()
        app.bundleIdentifier.return_value = "com.google.Chrome"
        with mock.patch.object(browser, "front_browser", return_value=app), \
                mock.patch.object(browser, "page_url", return_value="https://www.rednote.com/"), \
                mock.patch.object(browser, "_set_tab_url", return_value=True) as set_url, \
                mock.patch.object(browser.subprocess, "run") as run, mock.patch.object(browser.time, "sleep"), \
                mock.patch.object(browser, "ensure_visible"):
            self.assertEqual(browser.open_url("https://www.rednote.com/search_result?keyword=x")["tab"], "当前标签页")
            run.assert_not_called()
            self.assertEqual(browser.open_url("https://linux.do/latest")["tab"], "新标签页")  # 别的网站不覆盖
            set_url.assert_called_once()
            run.assert_called_once()

    def test_open_rejects_non_http(self):
        r = browser.call_tool("browser_open", {"url": "file:///etc/passwd"})
        self.assertEqual(r["status"], "error")


class LinuxDoTest(unittest.TestCase):
    TOPIC = "https://linux.do/t/topic/2989619"

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def test_registered(self):
        self.assertIn("linux.do/latest", tools.rules())
        self.assertTrue(tools.skip_jev("看看 linux.do 今天的热帖"))
        self.assertTrue(tools.skip_jev("L站有什么新消息"))

class RednoteTest(unittest.TestCase):
    NOTE_URL = "https://www.rednote.com/discovery/item/6ac1d0c4000000000200e061?xsec_token=ABG=&xsec_source="

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def test_registered(self):
        self.assertIn("rednote_feed", [t["name"] for t in tools.specs()])
        self.assertIn("rednote.com", tools.rules())
        self.assertTrue(tools.skip_jev("刷一下小红书"))
        for t in rednote.TOOLS:
            self.assertEqual(set(t["parameters"]["required"]), set(t["parameters"]["properties"]))

    def test_note_links(self):
        search = "https://www.rednote.com/search_result/6a713fcd00000000240255e3?xsec_token=ABj=&xsec_source="
        for u in (self.NOTE_URL, search):
            self.assertTrue(rednote.NOTE.search(u))
            self.assertTrue(browser.CONTENT_LINK.search(u))
        self.assertFalse(rednote.NOTE.search("https://www.rednote.com/user/profile/5d8f182b?xsec_token=x"))
        # 封面和标题链接 token 不同也算同一篇
        self.assertEqual(browser._link_key(self.NOTE_URL), browser._link_key(self.NOTE_URL.replace("ABG=", "XYZ=")))
        self.assertTrue(browser._skip_link("要获取缺失的图片说明，请打开上下文菜单。", self.NOTE_URL))

    def test_author_date_suffix(self):
        for raw, name in (("马哥 08-04", "马哥"), ("just say 昨天 14:11", "just say"),
                          ("CoderJia 14分钟前", "CoderJia"), ("海哥影院", "海哥影院"), ("Day 2026-01-02", "Day")):
            self.assertEqual(rednote.DATE_SUFFIX.sub("", raw), name)

    def _publish(self, **kw):
        args = {"title": "测试标题", "body": "正文", "card_text": "卡片文字", **kw}
        pressed = []
        with mock.patch.object(rednote.browser, "open_url"), \
                mock.patch.object(rednote, "_app_root", return_value=(mock.Mock(), object())), \
                mock.patch.object(rednote.axui, "_enable_web_ax"), \
                mock.patch.object(rednote.axui, "_main_window"), \
                mock.patch.object(rednote, "_press", side_effect=lambda root, text, *a, **k: pressed.append(text)), \
                mock.patch.object(rednote, "_fill"), \
                mock.patch.object(rednote.browser, "read_page", return_value={"text": "发布成功", "url": ""}), \
                mock.patch.object(rednote.time, "sleep"):
            r = rednote.call_tool("rednote_publish", args)
        return r, pressed

    def test_publish_presses_publish(self):
        r, pressed = self._publish()
        self.assertEqual(r["status"], "posted")
        self.assertEqual(pressed, ["上传图文", "文字配图", "生成图片", "下一步", "发布"])

    def test_publish_validation(self):
        for kw in ({"title": "字" * 21}, {"body": ""}):
            r, pressed = self._publish(**kw)
            self.assertEqual((r["status"], pressed), ("error", []))


if __name__ == "__main__":
    unittest.main()
