"""一次运行结束后把用过的应用最小化：运行中被切到最前面过的应用；开始时就在最前面的应用只有真的对它做过动作才算；
不动本程序、Finder。不碰屏幕。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import computer  # noqa: E402

USER = (100, "com.anthropic.claudefordesktop", "Claude")
WECHAT = (200, "com.tencent.xinWeChat", "微信")
CHROME = (300, "com.google.Chrome", "Google Chrome")
FINDER = (400, "com.apple.finder", "访达")


class UsedAppsTest(unittest.TestCase):
    def track(self, fronts):
        seq = iter(fronts)
        with mock.patch.object(computer, "frontmost_app", side_effect=lambda: next(seq)):
            u = agent.UsedApps()
            for _ in fronts[1:]:
                u.seen()
        return u

    def test_minimizes_apps_brought_to_front(self):
        u = self.track([USER, WECHAT, CHROME, WECHAT, USER, FINDER, (os.getpid(), "x", "Ami")])
        self.assertEqual(u.apps, {200: "微信", 300: "Google Chrome"})
        with mock.patch.object(computer, "minimize_windows", side_effect=lambda pid: 1 if pid == 200 else 0) as m:
            self.assertEqual(u.minimize(), ["微信"])
        self.assertEqual(sorted(c.args[0] for c in m.call_args_list), [200, 300])

    def test_start_app_minimized_only_when_acted_on(self):
        seq = iter([USER, USER, USER, WECHAT])
        with mock.patch.object(computer, "frontmost_app", side_effect=lambda: next(seq)):
            u = agent.UsedApps()
            u.seen()             # 截图时看到用户自己的应用：不算
            self.assertEqual(u.apps, {})
            u.seen(acting=True)  # 在它最前面时做了动作：算
            u.seen(acting=True)
        self.assertEqual(u.apps, {100: "Claude", 200: "微信"})

    def test_switch_off(self):
        u = self.track([USER, WECHAT])
        with mock.patch.object(agent, "MINIMIZE_AFTER_RUN", False), \
                mock.patch.object(computer, "minimize_windows") as m:
            self.assertEqual(u.minimize(), [])
        m.assert_not_called()

    def test_errors_are_ignored(self):
        with mock.patch.object(computer, "frontmost_app", side_effect=RuntimeError("no ax")):
            u = agent.UsedApps()
            u.seen()
        self.assertEqual(u.apps, {})
        u.apps = {200: "微信"}
        with mock.patch.object(computer, "minimize_windows", side_effect=RuntimeError("gone")):
            self.assertEqual(u.minimize(), [])


if __name__ == "__main__":
    unittest.main()
