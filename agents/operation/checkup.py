"""自检：看各平台的登录状态，要重新登录的报给 AMI 中控（AMI 中控派 ami.check 任务时，workflows 的调度器调这里）。

- 命令行工具（rednote / 小红书 xhs、B 站 bili、Telegram tg、X twitter）：跑只读的 status，不开浏览器、不动鼠标；没装的跳过。
- 浏览器里的网站：在你已登录的浏览器里打开一个要登录才能看的页面，被跳到登录页（网址里有 login / signin / passport …）、
  页面上有密码框、或者出现这个网站没登录时才有的字，就算掉线了。自己新开的标签页看完就关掉。
不调模型，不输密码、不替你登录。
自检项（check_names）启动时报给中控（PUT /api/v1/checks），中控每天派 ami.check 任务，结果按 to_checks 的格式回报。
浏览器里看哪些网站：CHECKUP_SITES（逗号分隔，off 不看浏览器），默认全部。

  python checkup.py          # 两样都查
  python checkup.py cli      # 只查命令行工具（不碰浏览器）
"""

import os
import re
import time
from dataclasses import dataclass

AUTH_CODES = {"not_authenticated", "auth_required", "unauthorized", "auth_error", "session_expired"}
AUTH_TEXT = re.compile(r"登录|登陆|log ?in|sign ?in|expired|过期|unauthori[sz]ed", re.I)
LOGIN_URL = re.compile(r"login|signin|sign[-_]in|passport\.|authwall|/i/flow/", re.I)
NETWORK_TEXT = re.compile(r"timed? ?out|超时|connection|network|网络|resolve|SSL", re.I)


def _cli_tools() -> list[tuple[str, object, str]]:
    import bili
    import tg
    import xcli
    import xhs
    return [(xhs.NAME, xhs, "xhs"), ("B 站", bili, "bili"), ("Telegram", tg, "tg"), ("X", xcli, "twitter")]


def account_for_tool(tool: str) -> str:
    """工具名（xhs / xhs_write / bili …）→ 它用的账号在自检里的名字；不是命令行工具就返回空。"""
    base = tool.removesuffix("_write")
    return next((check_name({"name": n, "kind": "cli"}) for n, _, t in _cli_tools() if t == base), "")


def check_cli(name: str, mod, tool: str) -> dict:
    """跑一次 <tool> status。返回 {name, kind, state: ok / login / error / skipped, detail, hint}。"""
    out = {"name": name, "kind": "cli", "tool": tool}
    if not mod.available():
        return {**out, "state": "skipped", "detail": "没装命令行工具"}
    r = mod.call_tool(tool, {"command": "status", "args": []})
    data = r.get("data")
    if r.get("status") == "ok" and not (isinstance(data, dict) and False in
                                        (data.get("authenticated"), data.get("logged_in"), data.get("valid"))):
        return {**out, "state": "ok", "detail": "已登录"}
    detail = str(r.get("detail") or r.get("text") or data or "")[:300]
    if r.get("status") == "ok" or str(r.get("code")) in AUTH_CODES or AUTH_TEXT.search(detail):
        return {**out, "state": "login", "detail": detail or "没登录", "hint": r.get("hint") or mod.LOGIN_HINT}
    return {**out, "state": "error", "detail": detail or "查不了"}


@dataclass(frozen=True)
class Probe:
    site: str                 # 网站名（和 sites.py 一致）
    url: str                  # 要登录才能看的页面
    logged_out: str = ""      # 没登录时页面上才有的字（整行匹配），网址和密码框看不出来时用


PROBES = [
    Probe("rednote.com", "https://www.rednote.com/notification", r"登录|Log in|ログイン"),
    Probe("x.com", "https://x.com/home"),
    Probe("reddit.com", "https://www.reddit.com/settings/"),
    Probe("linkedin.com", "https://www.linkedin.com/feed/"),
    Probe("zhihu.com", "https://www.zhihu.com/"),
    Probe("bilibili.com", "https://account.bilibili.com/account/home"),
    Probe("linux.do", "https://linux.do/my/preferences"),
    Probe("weibo.com", "https://weibo.com/"),
    Probe("okjike.com", "https://web.okjike.com/"),
]
SITES = [p.site for p in PROBES]
SETTLE = 6.0  # 打开后等几秒，让页面跳转（被重定向到登录页）


def logged_out(url: str, has_password: bool, text: str, probe: Probe) -> str:
    """看出没登录就返回理由，否则返回空。网址只看域名和路径（参数里的 redirect_after_login 之类不算）。"""
    m = re.match(r"https?://([^?#]*)", url or "")
    if m and LOGIN_URL.search(m.group(1)):
        return f"被跳到了登录页 {m.group(1)[:80]}"
    if has_password:
        return "页面上有密码框"
    if probe.logged_out:
        hit = next((line for line in text.splitlines() if re.fullmatch(probe.logged_out, line.strip())), None)
        if hit:
            return f"页面上有「{hit.strip()}」按钮"
    return ""


def check_site(probe: Probe, on_step=None) -> dict:
    """在浏览器里打开 probe.url 看有没有掉线。on_step：每次切到浏览器后调一下（记下用过的应用，最后最小化）。"""
    import browser
    out = {"name": probe.site, "kind": "site", "url": probe.url}
    try:
        opened = browser.open_url(probe.url)
        time.sleep(SETTLE)
        try:
            page = browser.read_page(max_chars=3000)
            reason = logged_out(page.get("url") or "", browser.has_password_field(), page.get("text") or "", probe)
        finally:
            if on_step:
                on_step()
            if opened.get("tab") == "新标签页":
                browser.close_tab()
    except Exception as e:  # noqa: BLE001  浏览器没开、没权限、页面打不开
        return {**out, "state": "error", "detail": f"{type(e).__name__}: {e}"[:300]}
    if reason:
        return {**out, "state": "login", "detail": reason,
                "hint": f"请用户在浏览器里重新登录 {probe.site}（打开 {probe.url} 看看）"}
    return {**out, "state": "ok", "detail": "已登录"}


def run(sites: list[str] | None = None, browser_too: bool = True, on_step=None, stop=lambda: False) -> list[dict]:
    """查一遍。sites：要在浏览器里看的网站（默认全部）；stop() 为真时不再看剩下的网站（你开始用电脑了）。"""
    results = [check_cli(*t) for t in _cli_tools()]
    wanted = default_sites() if sites is None else sites
    for p in PROBES if browser_too else []:
        if p.site in wanted and not stop():
            results.append(check_site(p, on_step))
    return results


def summary(results: list[dict]) -> str:
    bad = [r["name"] for r in results if r["state"] == "login"]
    err = [r["name"] for r in results if r["state"] == "error"]
    ok = [r["name"] for r in results if r["state"] == "ok"]
    parts = [f"{'、'.join(bad)} 要重新登录" if bad else "登录都正常"]
    if err:
        parts.append(f"{'、'.join(err)} 查不了")
    parts.append(f"{len(ok)} 个正常")
    return "；".join(parts)


def default_sites() -> list[str]:
    """要在浏览器里看的网站：CHECKUP_SITES（逗号分隔；off 不看浏览器），默认全部。"""
    v = os.environ.get("CHECKUP_SITES", "").strip()
    if not v:
        return list(SITES)
    if v.lower() in ("off", "none", "no", "0"):
        return []
    return [x.strip() for x in v.split(",") if x.strip() in SITES]


def check_name(r: dict) -> str:
    return f"{r['name']} 命令行登录" if r["kind"] == "cli" else f"{r['name']} 网页登录"


def check_names(sites: list[str] | None = None) -> list[str]:
    """报给中控的自检项：装了的命令行工具 + 要在浏览器里看的网站。"""
    wanted = default_sites() if sites is None else sites
    return ([check_name({"name": n, "kind": "cli"}) for n, mod, _ in _cli_tools() if mod.available()]
            + [check_name({"name": p.site, "kind": "site"}) for p in PROBES if p.site in wanted])


def to_checks(results: list[dict], names: list[str]) -> list[dict]:
    """结果 → 中控的格式 [{name, ok, kind, message, hint}]，kind：auth / network / other。
    声明过但这次没查到的（被打断）也报上，算没通过，免得中控以为都正常。"""
    out, seen = [], set()
    for r in results:
        if r["state"] == "skipped":
            continue
        name = check_name(r)
        seen.add(name)
        if r["state"] == "ok":
            out.append({"name": name, "ok": True})
            continue
        kind = "auth" if r["state"] == "login" else "network" if NETWORK_TEXT.search(r.get("detail") or "") else "other"
        out.append({"name": name, "ok": False, "kind": kind, "message": r.get("detail") or "",
                    **({"hint": r["hint"]} if r.get("hint") else {})})
    out += [{"name": n, "ok": False, "kind": "other", "message": "这次没查到（被电脑前的操作打断了）",
             "hint": "等会儿在中控点「立即自检」再查一次"} for n in names if n not in seen]
    return out


if __name__ == "__main__":
    import sys

    import paths
    paths.load_env()
    res = run(browser_too=sys.argv[1:2] != ["cli"])
    for r in res:
        print(f"{r['state']:8} {r['name']:14} {r['detail']}" + (f"\n         → {r['hint']}" if r.get("hint") else ""))
    print(summary(res))
