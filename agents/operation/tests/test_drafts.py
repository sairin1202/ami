"""起草帖子（post.draft）：只调一次模型，不碰电脑、不调工具；按平台给草稿；V2EX、少数派、掘金的发帖说明。不联网。"""

import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import amients  # noqa: E402
import sites  # noqa: E402
import workflows as wf  # noqa: E402


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(output=[SimpleNamespace(type="message", content=[SimpleNamespace(text=self.text)])])


DRAFTS = {"drafts": [
    {"platform": "V2EX", "where": "分享创造", "title": "做了个住在 Mac 里的电脑助理", "body": "动机……", "tags": []},
    {"platform": "掘金", "where": "人工智能", "title": "让 agent 操作 Mac 的几个坑", "body": "实现思路……", "tags": ["AI"]},
    {"platform": "即刻", "body": ""},  # 没正文的丢掉
]}


class DraftTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home.name, "AMIENTS": "on", "AMIENTS_API_KEY": ""}).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.home.cleanup)

    def task(self, payload):
        return {"id": 9, "token": "t", "kind": wf.DRAFT_KIND, "payload": payload}

    def test_drafts_per_platform_with_rules(self):
        llm = FakeLLM("好的：\n" + json.dumps(DRAFTS, ensure_ascii=False))
        r = wf.draft_posts(self.task({"platforms": "V2EX、掘金", "text": "新版本能领中控任务", "material": "Ami 是…"}),
                           llm, "m")
        self.assertEqual([d["platform"] for d in r["drafts"]], ["V2EX", "掘金"])
        self.assertIn("没发出去", r["text"])
        kw = llm.calls[0]
        self.assertNotIn("tools", kw)                      # 不给工具：不可能发出去
        self.assertIn("分享创造", kw["instructions"])      # 带上 V2EX 的规矩
        self.assertIn("确定并发布", kw["instructions"])    # 带上掘金的规矩
        self.assertNotIn("sspai.com/matrix", kw["instructions"])  # 没要的平台（少数派）的网站规则不带
        self.assertEqual(json.loads(kw["input"])["platforms"], ["V2EX", "掘金"])

    def test_unformatted_reply_is_returned_raw(self):
        r = wf.draft_posts(self.task("写一条即刻动态说新版本上线了"), FakeLLM("新版本上线啦……"), "m")
        self.assertEqual(r["drafts"], [])
        self.assertIn("新版本上线啦", r["raw"])

    def test_draft_task_runs_without_agent(self):
        llm = FakeLLM(json.dumps(DRAFTS, ensure_ascii=False))
        ran = []
        s = wf.Scheduler(lambda: SimpleNamespace(client=llm, model="m", run=lambda *a: ran.append(a)))
        task = self.task({"platforms": ["V2EX"], "text": "x"})
        job = {"task": task, "lease": amients.Lease(task), "done": threading.Event()}
        s.run_task(job)
        self.assertEqual(ran, [])                           # 没跑 agent
        self.assertEqual(len(job["result"]["drafts"]), 2)
        self.assertTrue(job["done"].is_set())
        self.assertEqual(wf.recent_events()[-1]["kind"], "drafted")

    def test_capability_registered(self):
        cap = [c for c in wf.capabilities() if c["kind"] == wf.DRAFT_KIND][0]
        self.assertEqual(cap["title"], "起草帖子")
        self.assertIn("不发出去", cap["description"])
        self.assertIn(wf.DRAFT_KIND, wf.task_kinds())

    def test_new_sites_have_posting_rules(self):
        for key, words in (("v2ex.com", ["分享创造", "发布主题"]), ("sspai.com", ["sspai.com/write", "投稿"]),
                           ("juejin.cn", ["确定并发布", "沸点"])):
            s = [x for x in sites.SITES if x.key == key][0]
            for w in words:
                self.assertIn(w, s.rules, key)
        self.assertTrue(sites.JEV_SKIP.search("帮我在少数派写篇文章"))


if __name__ == "__main__":
    unittest.main()
