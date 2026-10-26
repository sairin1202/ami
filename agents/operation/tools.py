"""函数工具注册表：各模块按同一套接口接入 GPT 的工具调用。

- 桌面应用：微信（wechat.py，OCR；加好友 / 好友申请在 wechat_friend.py）、Lark / 飞书客户端（lark.py，读网页 DOM 的辅助功能树）
- 本地消息记录（msgstore.py）：call() 里统一把读到的和发出去的消息存到本机，message_history 读回
- 浏览器里操作（用户已登录的会话）：通用工具 browser.py，各网站的链接格式 / 操作说明在 sites.py
  （Reddit、linux.do、X、知乎、LinkedIn、微博、B 站、公众号文章、即刻）；专门工具：rednote.py 刷推荐 / 发笔记，
  twitter.py 刷 X 推荐 / 搜索，linkedin.py 搜人 / 公司 / 帖子 / 职位
- 走命令行工具（jackwener 的 xiaohongshu-cli / twitter-cli / bilibili-cli / tg-cli，用户本人账号，公共调用在 cli_runner.py）：
  xhs.py 小红书、xcli.py X、bili.py B 站、tg.py Telegram 个人账号；没装对应命令时自动不提供
- 走接口（有官方接口的不开浏览器）：网页搜索（Exa）、RSS、Hacker News、V2EX、GitHub（gh 和 Trending 网页）、AI 论文 / 模型（Hugging Face、arXiv）、Discord、Slack、Telegram、飞书

- 技能（skills.py）：skills/<名字>/SKILL.md 和 ~/.computer-use/skills/ 里的操作手册，系统提示只列名字和说明，skill_load 读全文

每个模块提供：TOOLS（工具定义）、RULES（追加到系统提示的使用规则）、JEV_SKIP（指令匹配时跳过 Jev 的正则）、
available()（是否已安装 / 配置）、call_tool(name, args)、describe_call(name, args)；
会发出内容的模块另有 WRITE_TOOLS。所有工具都直接执行，不需要任何人确认。
"""

import bili
import browser
import discord
import exa
import feishu
import github
import hn
import lark
import linkedin
import msgstore
import papers
import rednote
import rss
import sites
import skills
import slack
import telegram
import tg
import twitter
import v2ex
import wechat
import wechat_friend
import xcli
import xhs

# 走命令行接口的小红书 / X / B 站 / Telegram（个人账号）排在对应的浏览器 / 机器人工具前面，规则里也是优先用它们
PROVIDERS = [skills, wechat, wechat_friend, lark, msgstore, browser, sites, xhs, rednote, xcli, twitter, bili, linkedin, exa, rss,
             hn, v2ex, github, papers, discord, slack, tg, telegram, feishu]
WECHAT_WRITE = {"wechat_send"}  # wechat.py 早于 WRITE_TOOLS 约定


def active() -> list:
    return [p for p in PROVIDERS if p.available()]


def specs() -> list[dict]:
    return [t for p in active() for t in p.TOOLS]


def rules() -> str:
    return "".join(p.RULES for p in active())


def write_tools() -> set[str]:
    """会发出内容的工具（发消息、发帖、加好友）。"""
    return WECHAT_WRITE | {n for p in PROVIDERS for n in getattr(p, "WRITE_TOOLS", ())}


def skip_jev(task: str) -> bool:
    """指令明显要用某个工具模块（微信、各网站、各接口）时，Jev 帮不上忙，直接交给 GPT + 工具。"""
    return any(p.JEV_SKIP.search(task) for p in active())


def _owner(name: str):
    for p in active():
        if any(t["name"] == name for t in p.TOOLS):
            return p
    return None


def call(name: str, args: dict) -> dict:
    p = _owner(name)
    if p is None:
        return {"status": "error", "detail": f"未知工具 {name}"}
    result = p.call_tool(name, args)
    try:
        msgstore.record(name, args, result)  # 读到的和发出去的消息存到本地
    except Exception as e:  # noqa: BLE001 存不下来不影响工具结果
        print(f"[msgstore] 保存消息失败：{type(e).__name__}: {e}")
    _note_login(name, result)
    return result


def _note_login(name: str, result: dict) -> None:
    """命令行工具报没登录：记进 Ami 自己的状态（agentstate.accounts），中控来要状态时能看到。只记坏消息。"""
    if result.get("status") != "error":
        return
    try:
        import agentstate
        import checkup
        account = checkup.account_for_tool(name)
        if account and str(result.get("code")) in checkup.AUTH_CODES:
            agentstate.set_account(account, False, kind="auth", detail=str(result.get("detail") or ""),
                                   hint=str(result.get("hint") or ""), source="tool")
    except Exception:  # noqa: BLE001  记不下来不影响工具结果
        pass


def describe(name: str, args: dict) -> str:
    p = _owner(name)
    return p.describe_call(name, args) if p else name
