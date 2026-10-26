"""本地状态目录：缓存、经验、发送记录、运行报告等都放在这里，只在本机。可用 COMPUTER_USE_HOME 改位置。"""

import os


def state_dir(*sub: str) -> str:
    d = os.path.join(os.environ.get("COMPUTER_USE_HOME") or os.path.expanduser("~/.computer-use"), *sub)
    os.makedirs(d, exist_ok=True)
    return d


def resource(*parts: str) -> str:
    """项目目录里随代码一起的文件（SOUL.md 等）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def load_env() -> list[str]:
    """读配置（.env）：先读项目目录的，再读 ~/.computer-use/.env 补上项目里没有的项。
    已经设置的环境变量不覆盖。返回读到的文件。"""
    from dotenv import load_dotenv
    found = []
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), os.path.join(state_dir(), ".env")):
        if os.path.isfile(p):
            load_dotenv(p, override=False)
            found.append(p)
    return found
