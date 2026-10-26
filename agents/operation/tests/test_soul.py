"""SOUL.md 人设加载：默认读仓库里的 SOUL.md，可换文件或关闭，人设排在规则之后且声明规则优先。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COMPUTER_USE_HOME", tempfile.mkdtemp(prefix="cu-test-"))  # 不写真实目录

import agent  # noqa: E402


class SoulTest(unittest.TestCase):
    def test_default_soul_after_rules(self):
        with mock.patch.dict(os.environ, {"COMPUTER_USE_SOUL": ""}):
            text = agent.build_instructions()
        self.assertIn("Ami", text)
        self.assertLess(text.index("所有操作"), text.index("你是 Ami"))
        self.assertIn("以上面的规则为准", text)

    def test_off_and_custom_file(self):
        with mock.patch.dict(os.environ, {"COMPUTER_USE_SOUL": "off"}):
            self.assertEqual(agent.load_soul(), "")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("你是阿木，说话简洁。")
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {"COMPUTER_USE_SOUL": f.name}):
            self.assertIn("阿木", agent.load_soul())
        with mock.patch.dict(os.environ, {"COMPUTER_USE_SOUL": "/no/such/soul.md"}):
            self.assertEqual(agent.load_soul(), "")


if __name__ == "__main__":
    unittest.main()
