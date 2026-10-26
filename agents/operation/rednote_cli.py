"""让 xiaohongshu-cli（xhs 命令）改连国际版 rednote.com，参数和 xhs 一样：

  python3 rednote_cli.py status          # 用哪个 Python 运行都行，会自动换成 xiaohongshu-cli 自己的解释器
  python3 rednote_cli.py login           # 从浏览器读 www.rednote.com 的登录 cookie

rednote.com 跑的是同一套网页（xhs-pc-web），接口路径一样，只是域名不同：
  接口 edith.xiaohongshu.com → webapi.rednote.com，主页 www.xiaohongshu.com → www.rednote.com，
  创作者中心 creator.xiaohongshu.com → creator.rednote.com。
xiaohongshu-cli 把这些写死在代码里，这里不改它装好的文件（升级会被覆盖），而是在它加载前把常量换掉：
- 域名常量、登录信息目录（~/.rednote-cli，和 xiaohongshu.com 的 ~/.xiaohongshu-cli 分开）
- 读浏览器 cookie：改读 .rednote.com 的 cookie；它只认域名里带 xiaohongshu.com 的 cookie，所以读出来后改个域名标记
  （cookie 的值不变，发请求时只用名字和值）；另起子进程读 cookie 的路径换成在本进程里读，补丁才生效
- 笔记链接：rednote.com 的链接也能解析，生成的链接指向 www.rednote.com
xiaohongshu-cli 升级后这些名字变了会直接报错（不会悄悄连回 xiaohongshu.com）。
扫码登录（login --qrcode）和发笔记的图片上传（上传域名不确定）没有适配。
"""

from __future__ import annotations  # 系统自带的 python3 可能是 3.9，先能启动，再换到 xiaohongshu-cli 的解释器

import os
import shutil
import sys

HOSTS = {
    "EDITH_HOST": "https://webapi.rednote.com",
    "CREATOR_HOST": "https://creator.rednote.com",
    "HOME_URL": "https://www.rednote.com",
}
CONFIG_DIR = ".rednote-cli"


def _patch() -> None:
    import xhs_cli.constants as constants  # 只有常量，先改它，后面的模块 import 时拿到的就是新值
    for name, value in HOSTS.items():
        if not hasattr(constants, name):
            raise SystemExit(f"rednote_cli：xiaohongshu-cli 里找不到 {name}，可能升级后改了结构，请更新 rednote_cli.py")
        setattr(constants, name, value)
    constants.CONFIG_DIR_NAME = CONFIG_DIR

    import browser_cookie3 as bc3

    def wrap(loader):
        def load(*args, domain_name=None, **kwargs):
            if domain_name and "xiaohongshu.com" in domain_name:
                domain_name = domain_name.replace("xiaohongshu.com", "rednote.com")
            jar = loader(*args, domain_name=domain_name, **kwargs)
            for c in jar:
                if c.domain and "rednote.com" in c.domain:
                    c.domain = c.domain.replace("rednote.com", "xiaohongshu.com")
            return jar
        return load
    for name in dir(bc3):
        fn = getattr(bc3, name)
        if callable(fn) and not name.startswith("_") and name.islower() and name not in ("load",) \
                and getattr(fn, "__module__", "") == bc3.__name__ and _takes_domain(fn):
            setattr(bc3, name, wrap(fn))
    if hasattr(bc3, "load"):
        bc3.load = wrap(bc3.load)

    import xhs_cli.cookies as cookies
    for name in ("_extract_in_process", "_extract_via_subprocess"):
        if not hasattr(cookies, name):
            raise SystemExit(f"rednote_cli：xiaohongshu-cli 里找不到 cookies.{name}，请更新 rednote_cli.py")
    cookies._extract_via_subprocess = cookies._extract_in_process

    import xhs_cli.formatter as formatter
    parse = formatter.parse_note_reference
    formatter.parse_note_reference = lambda ref: parse(ref.replace("rednote.com", "xiaohongshu.com")
                                                      if isinstance(ref, str) else ref)
    import xhs_cli.formatter_renderers as renderers
    renderers.HOME_URL = HOSTS["HOME_URL"]


def _takes_domain(fn) -> bool:
    import inspect
    try:
        return "domain_name" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def xhs_python() -> str | None:
    """xiaohongshu-cli 装在哪个 Python 里：读 xhs 命令第一行的 #!。"""
    xhs = shutil.which("xhs") or os.path.expanduser("~/.local/bin/xhs")
    try:
        with open(xhs, encoding="utf-8") as f:
            first = f.readline().strip()
    except OSError:
        return None
    exe = first[2:].strip().split()[0] if first.startswith("#!") else ""
    return exe if exe and os.access(exe, os.X_OK) else None


def main() -> None:
    try:
        import xhs_cli  # noqa: F401
    except ImportError:  # 不是 xiaohongshu-cli 的解释器：换过去重新运行
        exe = xhs_python()
        if exe is None:
            raise SystemExit("rednote_cli：没找到 xiaohongshu-cli，先运行 uv tool install xiaohongshu-cli") from None
        os.execv(exe, [exe, os.path.abspath(__file__), *sys.argv[1:]])
    _patch()
    from xhs_cli.cli import cli
    sys.argv[0] = "rednote"
    cli()


if __name__ == "__main__":
    main()
