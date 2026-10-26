"""小红书 / B 站 / X / Telegram 命令行工具的接入：用一个假的命令行程序代替真的 xhs / bili / twitter / tg，不联网、不碰账号。"""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bili  # noqa: E402
import cli_runner  # noqa: E402
import tg  # noqa: E402
import tools  # noqa: E402
import xcli  # noqa: E402
import xhs  # noqa: E402

# 假命令：把收到的参数原样放进 data 里；第一个参数是 needlogin / boom / text 时模拟没登录、崩溃、非 JSON 输出
FAKE = r'''#!{python}
import json, sys
args = sys.argv[1:]
if args[:1] == ["needlogin"]:
    print(json.dumps({{"ok": False, "schema_version": "1", "error": {{"code": "not_authenticated", "message": "need login"}}}}))
elif args[:1] == ["boom"]:
    sys.stderr.write("Traceback: boom\n"); sys.exit(2)
elif args[:1] == ["text"]:
    print("plain output")
elif args[:1] == ["big"]:
    print(json.dumps({{"ok": True, "schema_version": "1", "data": {{"items": [{{"text": "x" * 3000}} for _ in range(40)]}}}}))
else:
    print("提示：正在读取 cookie")  # JSON 前面混着一行提示
    print(json.dumps({{"ok": True, "schema_version": "1", "data": {{"argv": args}}, "pagination": {{"nextCursor": "c1"}}}}))
'''


class CLIToolsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name in ("xhs", "bili", "twitter", "tg"):
            p = os.path.join(self.dir.name, name)
            with open(p, "w") as f:
                f.write(FAKE.format(python=sys.executable))
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        mock.patch.object(cli_runner, "BIN_DIRS", [self.dir.name]).start()
        mock.patch.object(cli_runner.shutil, "which", return_value=None).start()  # 只认假命令
        mock.patch.dict(os.environ, {"COMPUTER_USE_HOME": os.path.join(self.dir.name, "home"), "X_VIA": "cli"}).start()
        mock.patch.object(xhs, "_command", return_value="xhs").start()  # 不走真的 rednote 启动器
        self.addCleanup(mock.patch.stopall)

    def test_registered_and_available(self):
        names = {t["name"] for t in tools.specs()}
        self.assertTrue({"xhs", "xhs_write", "bili", "bili_write", "twitter", "twitter_write", "tg", "tg_write"} <= names)
        self.assertTrue({"xhs_write", "bili_write", "twitter_write", "tg_write"} <= tools.write_tools())
        for mod in (xhs, bili, xcli, tg):
            for spec in mod.TOOLS:
                self.assertTrue(spec["strict"])
                self.assertEqual(spec["parameters"]["required"], ["command", "args"])
                self.assertFalse(spec["parameters"]["additionalProperties"])

    def test_x_defaults_to_browser(self):
        with mock.patch.dict(os.environ, {"X_VIA": ""}):
            self.assertFalse(xcli.available())  # 默认 X 走浏览器，不提供 twitter-cli 的工具
            self.assertNotIn("twitter_write", {t["name"] for t in tools.specs()})
            self.assertIn("x_feed", {t["name"] for t in tools.specs()})
        self.assertTrue(xcli.available())  # X_VIA=cli

    def test_not_installed(self):
        with mock.patch.object(cli_runner, "BIN_DIRS", []):
            self.assertFalse(xhs.available())
            r = xhs.call_tool("xhs", {"command": "search", "args": ["美食"]})
        self.assertEqual(r["status"], "error")
        self.assertIn("没有安装", r["detail"])

    def test_runs_with_json_and_parses_envelope(self):
        r = xhs.call_tool("xhs", {"command": "search", "args": ["美食", "--sort", "popular"]})
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["data"]["argv"], ["search", "美食", "--sort", "popular", "--json"])
        self.assertEqual(r["pagination"], {"nextCursor": "c1"})

    def test_whitelist(self):
        for tool, cmd in (("xhs", "delete"), ("xhs", "login"), ("xhs", "like"), ("xhs_write", "search"),
                          ("bili", "audio"), ("twitter_write", "delete"), ("tg", "purge"), ("tg_write", "edit")):
            r = {"xhs": xhs, "xhs_write": xhs, "bili": bili, "twitter_write": xcli, "tg": tg, "tg_write": tg}[tool] \
                .call_tool(tool, {"command": cmd, "args": []})
            self.assertEqual(r["status"], "error", (tool, cmd))
            self.assertIn("不支持子命令", r["detail"])
        r = xhs.call_tool("xhs_write", {"command": "like", "args": ["1"]})  # 写操作走 _write 工具
        self.assertEqual(r["data"]["argv"], ["like", "1", "--json"])

    def test_blocked_options(self):
        for a in (["x", "--output", "a.json"], ["x", "-o", "a"], ["1", "--yes"], ["x", "--json"], ["x", "--output=a"]):
            r = xcli.call_tool("twitter", {"command": "search", "args": a})
            self.assertEqual(r["status"], "error", a)

    def test_max_clamped(self):
        r = xcli.call_tool("twitter", {"command": "feed", "args": ["--max", "500"]})
        self.assertEqual(r["data"]["argv"], ["feed", "--max", "50", "--json"])
        r = bili.call_tool("bili", {"command": "hot", "args": ["--max=80"]})
        self.assertEqual(r["data"]["argv"], ["hot", "--max=50", "--json"])

    def test_login_hint_and_errors(self):
        r = cli_runner.run(cli_runner.which("xhs"), "needlogin", [], login_hint="请运行 xhs login")
        self.assertEqual((r["status"], r["code"], r["hint"]), ("error", "not_authenticated", "请运行 xhs login"))
        with self.assertRaises(cli_runner.CLIError) as e:
            cli_runner.run("xhs", "boom", [])
        self.assertIn("boom", str(e.exception))
        self.assertEqual(cli_runner.run("xhs", "text", []), {"status": "ok", "text": "plain output"})

    def test_long_output_shrunk(self):
        r = cli_runner.run("xhs", "big", [])
        self.assertLessEqual(len(json.dumps(r["data"], ensure_ascii=False)), cli_runner.MAX_CHARS)
        self.assertIn("截短", r["note"])
        self.assertTrue(r["data"]["items"][0]["text"].endswith("…"))

    def test_tg_refresh_rate_limited(self):
        r = tg.call_tool("tg", {"command": "refresh", "args": []})
        self.assertEqual(r["data"]["argv"], ["refresh", "--max-chats", "30", "--delay", "3", "--json"])
        r = tg.call_tool("tg", {"command": "refresh", "args": []})  # 刚同步过：拦住，不再跑
        self.assertEqual(r["status"], "error")
        self.assertIn("--sync-first", r["detail"])
        with mock.patch.object(tg.time, "time", return_value=time.time() + tg.REFRESH_GAP * 3600 + 1):
            self.assertEqual(tg.call_tool("tg", {"command": "refresh", "args": ["--max-chats", "5"]})["status"], "ok")
        r = tg.call_tool("tg", {"command": "recent", "args": ["--limit", "5000", "--sync-first"]})
        self.assertEqual(r["data"]["argv"], ["recent", "--limit", "200", "--sync-first", "--json"])

    def test_rednote_mode_uses_launcher(self):
        mock.patch.stopall()
        import rednote_cli
        with mock.patch.object(xhs, "REDNOTE", True), \
                mock.patch.object(rednote_cli, "xhs_python", return_value="/x/python"):
            self.assertEqual(xhs._command(), ["/x/python", xhs.LAUNCHER])
        with mock.patch.object(xhs, "REDNOTE", True), mock.patch.object(rednote_cli, "xhs_python", return_value=None):
            with self.assertRaises(cli_runner.CLIError):
                xhs._command()
        with mock.patch.object(xhs, "REDNOTE", False):
            self.assertEqual(xhs._command(), "xhs")
        if xhs.REDNOTE:  # 默认连 rednote：图片上传没适配，不开放发笔记
            self.assertNotIn("post", xhs.WRITE)
            self.assertIn("rednote", xhs.TOOLS[0]["description"])

    def test_command_prefix(self):
        fake = os.path.join(self.dir.name, "xhs")
        r = cli_runner.run([sys.executable, fake], "search", ["咖啡"])
        self.assertEqual(r["data"]["argv"], ["search", "咖啡", "--json"])
        with self.assertRaises(cli_runner.CLIError):
            cli_runner.run(["/nope/python", fake], "search", [])

    def test_describe(self):
        self.assertEqual(xhs.describe_call("xhs", {"command": "search", "args": ["美食"]}), f"{xhs.NAME} search 美食")
        self.assertEqual(tg.describe_call("tg_write", {"command": "send", "args": ["群", "hi"]}), "Telegram（发） send 群 hi")


if __name__ == "__main__":
    unittest.main()
