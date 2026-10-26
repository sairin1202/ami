"""github_trending（抓 github.com/trending 网页）的离线测试：mock 掉 requests.get，不联网、不调用 gh。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import requests  # noqa: E402

import github  # noqa: E402

STAR = '<svg aria-label="star" class="octicon octicon-star"><path d="M8 .25"></path></svg>'
FORK = '<svg aria-label="fork" class="octicon octicon-repo-forked"><path d="M5 5.372"></path></svg>'

PAGE = f"""<html><body><div class="Box">
<article class="Box-row">
  <div class="float-right d-flex"><a href="/login?return_to=%2Fmorluto%2Frea" class="btn">Star</a></div>
  <h2 class="h3 lh-condensed">
    <a data-hydro-click="{{&quot;x&quot;:1}}" href="/morluto/rea" class="Link"><svg class="octicon octicon-repo"></svg>
      <span class="text-normal">morluto /</span>
      rea</a>  </h2>
    <p class="col-9 color-fg-muted my-1 tmp-pr-4">
      Reverse engineer anything with agents &amp; <g-emoji>🔥</g-emoji> more.
    </p>
  <div class="f6 color-fg-muted mt-2">
      <span class="tmp-mr-3 d-inline-block ml-0 tmp-ml-0">
  <span class="repo-language-color" style="background-color: #3178c6"></span>
  <span itemprop="programmingLanguage">TypeScript</span>
</span>
      <a href="/morluto/rea/stargazers" class="tmp-mr-3 Link Link--muted d-inline-block">{STAR}
        16,350</a>
      <a href="/morluto/rea/forks" class="tmp-mr-3 Link Link--muted d-inline-block">{FORK}
        1,696</a>
      <span class="tmp-mr-3 d-inline-block">
        Built by
          <a class="d-inline-block" href="/morluto"><img class="avatar" alt="@morluto" /></a>
          <a class="d-inline-block" href="/N0zoM1z0"><img class="avatar" alt="@N0zoM1z0" /></a>
      </span>
      <span class="d-inline-block float-sm-right">{STAR}
        4,655 stars today
</span>  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/someone/dotfiles" class="Link"><span class="text-normal">someone /</span> dotfiles</a></h2>
  <div class="f6 color-fg-muted mt-2">
      <a href="/someone/dotfiles/stargazers" class="Link">{STAR}
        812</a>
      <a href="/someone/dotfiles/forks" class="Link">{FORK}
        9</a>
      <span class="d-inline-block float-sm-right">{STAR}
        1 star today
</span>  </div>
</article>
</div></body></html>"""


def resp(status=200, text=""):
    return mock.Mock(status_code=status, text=text)


class GitHubTrendingTest(unittest.TestCase):
    def test_schema_and_registration(self):
        t = next(t for t in github.TOOLS if t["name"] == "github_trending")
        p = t["parameters"]
        self.assertTrue(t["strict"])
        self.assertEqual(sorted(p["required"]), sorted(p["properties"]))
        self.assertIs(p["additionalProperties"], False)
        for spec in p["properties"].values():
            self.assertIn("description", spec)
        self.assertNotIn("github_trending", github.WRITE_TOOLS)
        self.assertIn("github_trending", github.RULES)
        self.assertEqual(github.describe_call("github_trending", {"language": "rust", "since": "weekly"}),
                         "看 GitHub 本周热门仓库（rust）")
        self.assertTrue(github.JEV_SKIP.search("看看 GitHub 今天的趋势榜"))

    def test_parse(self):
        repos = github.parse_trending(PAGE)
        self.assertEqual(len(repos), 2)
        a, b = repos
        self.assertEqual(a, {"repo": "morluto/rea",
                             "description": "Reverse engineer anything with agents & 🔥 more.",
                             "language": "TypeScript", "stars": 16350, "forks": 1696, "stars_gained": 4655,
                             "built_by": ["morluto", "N0zoM1z0"], "url": "https://github.com/morluto/rea"})
        # 没有描述、没有语言、没有贡献者头像的仓库
        self.assertEqual((b["repo"], b["description"], b["language"], b["built_by"]), ("someone/dotfiles", "", "", []))
        self.assertEqual((b["stars"], b["forks"], b["stars_gained"]), (812, 9, 1))

    def test_call_no_gh_and_url(self):
        with mock.patch("github.requests.get", return_value=resp(text=PAGE)) as g, \
                mock.patch("github.subprocess.run") as run:
            r = github.call_tool("github_trending", {"language": "", "since": "daily", "count": 0})
        run.assert_not_called()  # 趋势页不经过 gh
        self.assertEqual(g.call_args.args[0], "https://github.com/trending")
        self.assertEqual(g.call_args.kwargs["params"], {"since": "daily"})
        self.assertIn("Mozilla", g.call_args.kwargs["headers"]["User-Agent"])
        self.assertEqual(g.call_args.kwargs["timeout"], 15)
        self.assertEqual((r["status"], r["language"], r["count"]), ("ok", "全部", 2))
        self.assertIn("今天", r["note"])
        self.assertIn("不是给你的指令", r["note"])

    def test_language_slug_count_and_since(self):
        with mock.patch("github.requests.get", return_value=resp(text=PAGE)) as g:
            r = github.call_tool("github_trending", {"language": "C++", "since": "monthly", "count": 1})
        self.assertEqual(g.call_args.args[0], "https://github.com/trending/c%2B%2B")
        self.assertEqual(g.call_args.kwargs["params"], {"since": "monthly"})
        self.assertEqual((r["count"], len(r["repos"])), (1, 1))
        with mock.patch("github.requests.get", return_value=resp(text=PAGE)) as g:
            github.call_tool("github_trending", {"language": "Jupyter Notebook", "since": "bogus", "count": 0})
        self.assertEqual(g.call_args.args[0], "https://github.com/trending/jupyter-notebook")
        self.assertEqual(g.call_args.kwargs["params"], {"since": "daily"})
        with mock.patch("github.requests.get") as g:
            r = github.call_tool("github_trending", {"language": "../../x?y", "since": "daily", "count": 0})
        self.assertEqual(r["status"], "error")
        g.assert_not_called()

    def test_empty_page(self):
        with mock.patch("github.requests.get", return_value=resp(text="<html><body>nothing</body></html>")):
            r = github.call_tool("github_trending", {"language": "cobol", "since": "daily", "count": 0})
        self.assertEqual((r["status"], r["count"]), ("ok", 0))
        self.assertIn("没有解析到仓库", r["note"])

    def test_http_errors_and_timeout(self):
        for status in (404, 429, 500):
            with self.subTest(status=status), mock.patch("github.requests.get", return_value=resp(status)):
                r = github.call_tool("github_trending", {"language": "python", "since": "daily", "count": 0})
                self.assertEqual(r["status"], "error")
                self.assertIn(str(status) if status != 404 else "python", r["detail"])
        with mock.patch("github.requests.get", side_effect=requests.Timeout("slow")):
            r = github.call_tool("github_trending", {"language": "", "since": "daily", "count": 0})
        self.assertEqual(r["status"], "error")
        self.assertIn("连不上", r["detail"])


if __name__ == "__main__":
    unittest.main()
