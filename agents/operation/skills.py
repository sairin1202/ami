"""技能（skill）：写给 Ami 的一份份操作手册，教它把某类事从头做到尾（用哪些工具、按什么顺序、注意什么）。

- 每个技能是一个目录，里面一份 SKILL.md，开头是元信息：
      ---
      name: 发帖
      description: 一句话说明什么时候用
      ---
      正文（Markdown）
- 放在两个地方：项目里的 skills/（随代码一起）和 ~/.computer-use/skills/（你自己加的，只在本机；同名时它优先）。
- 系统提示里只列出每个技能的名字和一句话说明，省上下文；Ami 要用时调 skill_load 读全文。
  新加或改了技能要重启 Ami 才会出现在列表里（skill_load 每次都读最新的文件）。
"""

import os
import re

import paths

PROJECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
MAX_CHARS = 20000


def _dirs() -> list[str]:
    return [PROJECT_DIR, paths.state_dir("skills")]


def parse(text: str) -> tuple[dict, str]:
    """SKILL.md → (元信息, 正文)。没有元信息块时元信息为空。"""
    m = FRONT.match(text)
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, text[m.end():].strip()


def load_all() -> dict[str, dict]:
    """{名字: {name, description, path, body}}。后面的目录（用户自己的）覆盖前面的同名技能。"""
    out: dict[str, dict] = {}
    for d in _dirs():
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            path = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    meta, body = parse(f.read())
            except (OSError, UnicodeDecodeError):
                continue
            name = meta.get("name") or entry
            out[name] = {"name": name, "description": meta.get("description", ""), "path": path, "body": body,
                         "dir": entry}
    return out


def find(name: str) -> dict | None:
    name = (name or "").strip()
    all_ = load_all()
    if name in all_:
        return all_[name]
    return next((s for s in all_.values() if s["dir"] == name or s["name"].lower() == name.lower()), None)


def _rules() -> str:
    skills = load_all()
    if not skills:
        return ""
    lines = "\n".join(f"  · {s['name']}：{s['description']}" for s in skills.values())
    return ("\n- 技能（skill）：下面这些事有专门的操作手册。碰到对应的事，先用 skill_load 读完整的手册再照着做，"
            "不要凭印象做：\n" + lines)


RULES = _rules()
JEV_SKIP = re.compile(r"发帖|发推|发笔记|发动态|发布到|发到(?:小红书|rednote|推特|X|B站|微博|即刻|知乎|Reddit|LinkedIn|V2EX|少数派|掘金)", re.I)
WRITE_TOOLS: set = set()

TOOLS = [
    {"type": "function", "name": "skill_load", "strict": True,
     "description": "读一个技能（skill）的完整操作手册。系统提示里列出了有哪些技能；碰到对应的事先读手册再做。",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "技能名，和列表里的一致，例如「发帖」"}},
         "required": ["name"], "additionalProperties": False}},
]


def available() -> bool:
    return bool(load_all())


def call_tool(name: str, args: dict) -> dict:
    if name != "skill_load":
        return {"status": "error", "detail": f"未知工具 {name}"}
    s = find(args.get("name", ""))
    if s is None:
        names = "、".join(load_all()) or "（没有）"
        return {"status": "error", "detail": f"没有叫「{args.get('name', '')}」的技能，现在有：{names}"}
    body = s["body"]
    return {"status": "ok", "name": s["name"], "description": s["description"],
            "content": body if len(body) <= MAX_CHARS else body[:MAX_CHARS] + "\n…（太长，已截断）"}


def describe_call(name: str, args: dict) -> str:
    return f"读技能「{args.get('name', '')}」"
