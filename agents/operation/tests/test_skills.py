"""技能（skills.py）：读 SKILL.md、列进系统提示、skill_load 读全文；用户目录里的同名技能覆盖项目里的。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skills  # noqa: E402
import tools  # noqa: E402


def write(root, d, text):
    os.makedirs(os.path.join(root, d), exist_ok=True)
    with open(os.path.join(root, d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(text)


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "project")
        self.home = os.path.join(self.tmp.name, "home")
        mock.patch.object(skills, "PROJECT_DIR", self.project).start()
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": self.home}).start()
        self.addCleanup(mock.patch.stopall)

    def test_parse(self):
        meta, body = skills.parse("---\nname: 发帖\ndescription: \"发东西时用\"\n---\n\n# 正文\n")
        self.assertEqual(meta, {"name": "发帖", "description": "发东西时用"})
        self.assertEqual(body, "# 正文")
        self.assertEqual(skills.parse("没有元信息"), ({}, "没有元信息"))

    def test_load_override_and_tool(self):
        write(self.project, "post", "---\nname: 发帖\ndescription: 项目里的\n---\n项目版")
        write(self.project, "nometa", "只有正文")
        write(os.path.join(self.home, "skills"), "post", "---\nname: 发帖\ndescription: 我自己的\n---\n我的版本")
        all_ = skills.load_all()
        self.assertEqual(set(all_), {"发帖", "nometa"})
        self.assertEqual(all_["发帖"]["body"], "我的版本")  # 用户目录优先
        r = skills.call_tool("skill_load", {"name": "post"})  # 也认目录名
        self.assertEqual((r["status"], r["name"], r["content"]), ("ok", "发帖", "我的版本"))
        r = skills.call_tool("skill_load", {"name": "不存在"})
        self.assertEqual(r["status"], "error")
        self.assertIn("发帖", r["detail"])
        self.assertIn("我自己的", skills._rules())
        self.assertIn("skill_load", skills._rules())

    def test_no_skills(self):
        self.assertFalse(skills.available())
        self.assertEqual(skills._rules(), "")


class BundledPostSkillTest(unittest.TestCase):
    def test_post_skill_shipped_and_registered(self):
        s = skills.find("发帖")
        self.assertIsNotNone(s)
        for word in ("问清楚", "给用户过目", "核对", "rednote_publish", "x.com/compose/post", "bili_write", "重复发"):
            self.assertIn(word, s["body"])
        self.assertIn("skill_load", [t["name"] for t in tools.specs()])
        self.assertIn("发帖", tools.rules())
        self.assertTrue(skills.JEV_SKIP.search("帮我发帖到小红书"))


if __name__ == "__main__":
    unittest.main()
