"""jackwener 的几个平台命令行工具（xiaohongshu-cli、bilibili-cli、twitter-cli、tg-cli）的公共调用方式。

- 这些工具用你本人的账号（浏览器 cookie / Telegram 会话）走网站的接口（多是逆向出来的），不开浏览器、不动鼠标。
  安装：uv tool install xiaohongshu-cli bilibili-cli twitter-cli kabi-tg-cli（装到 ~/.local/bin）。
  登录由你本人在终端里做（xhs login、bili login、tg chats 第一次会问手机号和验证码；twitter 自动读浏览器 cookie），
  这里不登录、不登出。
- 每次调用：<命令> <子命令> <参数…> --json，解析统一的 {ok, schema_version, data | error} 信封。
  子命令只允许各模块列出的白名单；删除类、登录登出、写本地文件的选项（--output / --input / --yes 等）一律不让用。
- 输出太长时截短（长字符串截断、列表只留前面的），免得把模型的上下文撑爆。
"""

import json
import os
import shutil
import subprocess

TIMEOUT = 90
MAX_CHARS = 12000                 # 返回给模型的 JSON 最多这么长
BIN_DIRS = [os.path.expanduser("~/.local/bin")]  # uv tool / pipx 默认装在这里，后台进程的 PATH 里可能没有
# 不让模型用的选项：写 / 读本地文件、跳过确认
BLOCKED_OPTS = {"--output", "-o", "--input", "--from-file", "--yes", "-y"}


class CLIError(RuntimeError):
    pass


def which(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for d in BIN_DIRS:
        p = os.path.join(d, binary)
        if os.access(p, os.X_OK):
            return p
    return None


def check_args(args) -> list[str]:
    if not isinstance(args, list):
        raise CLIError("args 要是字符串列表")
    out = [str(a) for a in args]
    for a in out:
        opt = a.split("=", 1)[0]
        if opt in BLOCKED_OPTS:
            raise CLIError(f"不支持 {opt} 选项")
        if opt in ("--json", "--yaml"):
            raise CLIError("不用自己加 --json / --yaml，结果会自动按 JSON 解析")
    return out


def clamp_option(args: list[str], opt: str, most: int) -> list[str]:
    """把 args 里 opt 的数值压到 most 以内（例如 --max 不超过 50，请求量太大容易被封）。"""
    out = list(args)
    for i, a in enumerate(out):
        if a == opt and i + 1 < len(out):
            try:
                out[i + 1] = str(min(int(out[i + 1]), most))
            except ValueError:
                pass
        elif a.startswith(opt + "="):
            try:
                out[i] = f"{opt}={min(int(a.split('=', 1)[1]), most)}"
            except ValueError:
                pass
    return out


def _clip(v, n: int):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    if isinstance(v, list):
        return [_clip(x, n) for x in v]
    if isinstance(v, dict):
        return {k: _clip(x, n) for k, x in v.items()}
    return v


def _cap_lists(v, n: int):
    if isinstance(v, list):
        return [_cap_lists(x, n) for x in v[:n]]
    if isinstance(v, dict):
        return {k: _cap_lists(x, n) for k, x in v.items()}
    return v


def shrink(data, limit: int = MAX_CHARS) -> tuple[object, bool]:
    """数据序列化后超过 limit 字时截短：先截长字符串，再让每个列表只留前 n 项（n 逐次减半）。返回 (数据, 是否截短)。"""
    size = lambda d: len(json.dumps(d, ensure_ascii=False, default=str))  # noqa: E731
    if size(data) <= limit:
        return data, False
    data = _clip(data, 800)
    n = 50
    while n >= 1:
        d = _cap_lists(data, n)
        if size(d) <= limit:
            return d, True
        n //= 2
    return json.dumps(data, ensure_ascii=False, default=str)[:limit] + "…", True


def _parse(out: str) -> dict | None:
    """stdout 里的 JSON 信封（前面可能混着几行提示，从第一个 { 开始找）。"""
    out = out.strip()
    for i in [0] + [k for k, c in enumerate(out) if c == "{" and k > 0][:5]:
        try:
            env = json.loads(out[i:])
        except ValueError:
            continue
        if isinstance(env, dict) and "ok" in env:
            return env
    return None


def run(binary: str | list[str], command: str, args: list[str], *, login_hint: str = "",
        timeout: float = TIMEOUT) -> dict:
    """执行 <binary> <command> <args…> --json，返回给模型的结果字典。
    binary 也可以是现成的命令前缀（例如 [解释器, 脚本]），这时不再去 PATH 里找。"""
    if isinstance(binary, list):
        prefix, path = list(binary), binary[0]
        if not os.access(path, os.X_OK):
            raise CLIError(f"找不到 {path}")
        binary = os.path.basename(binary[-1])
    else:
        path = which(binary)
        if path is None:
            raise CLIError(f"没有安装 {binary} 命令行工具")
        prefix = [path]
    argv = [*prefix, command, *args, "--json"]
    env = {**os.environ, "NO_COLOR": "1", "OUTPUT": "json"}
    env["PATH"] = os.pathsep.join([os.path.dirname(path), env.get("PATH", "")])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise CLIError(f"{binary} {command} 超过 {timeout:.0f} 秒没有结束，已放弃") from None
    envelope = _parse(proc.stdout)
    if envelope is None:
        text = (proc.stdout.strip() or proc.stderr.strip())[-1500:]
        if proc.returncode == 0:
            return {"status": "ok", "text": text}
        raise CLIError(f"{binary} {command} 出错（退出码 {proc.returncode}）：{text or '没有输出'}")
    if not envelope.get("ok"):
        err = envelope.get("error") or {}
        code = str(err.get("code") or "error")
        out = {"status": "error", "code": code, "detail": str(err.get("message") or err)[:1500]}
        if code in ("not_authenticated", "auth_required", "unauthorized") and login_hint:
            out["hint"] = login_hint
        return out
    data, cut = shrink(envelope.get("data"))
    out = {"status": "ok", "data": data}
    if envelope.get("pagination"):
        out["pagination"] = envelope["pagination"]
    if cut:
        out["note"] = "结果太长，已截短（长文字截断、列表只保留前面几项）"
    return out


# ---------------------------------------------------------------------------
# 各模块共用的工具定义 / 分发：每个平台一个只读工具 <name> 和一个会发出内容的 <name>_write，参数都是 command + args
def tool_specs(name: str, about: str, read: dict[str, str], write: dict[str, str]) -> list[dict]:
    def spec(tool: str, cmds: dict[str, str], what: str) -> dict:
        lines = "\n".join(f"- {c}：{d}" for c, d in cmds.items())
        return {"type": "function", "name": tool, "strict": True,
                "description": f"{about}{what}\n子命令和参数（args 按命令行的顺序写，选项和值分开写，例如 "
                               f"[\"关键词\", \"--max\", \"10\"]；结果自动按 JSON 返回，不用加 --json）：\n{lines}",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "string", "enum": list(cmds), "description": "子命令"},
                    "args": {"type": "array", "items": {"type": "string"},
                             "description": "子命令后面的参数和选项，没有就传 []"}},
                    "required": ["command", "args"], "additionalProperties": False}}
    out = [spec(name, read, "这个工具只读，不会改动任何东西。")]
    if write:
        out.append(spec(f"{name}_write", write, "这个工具会用用户本人的账号发出或改动内容，直接生效。"))
    return out


def dispatch(tool: str, args: dict, *, binary: str | list[str], read: dict, write: dict, login_hint: str,
             prepare=None, timeout: float = TIMEOUT) -> dict:
    """按 tool（<name> 或 <name>_write）检查子命令白名单和参数，再执行。出错返回 {"status": "error", ...}。"""
    try:
        cmds = write if tool.endswith("_write") else read
        command = str(args.get("command") or "")
        if command not in cmds:
            raise CLIError(f"{tool} 不支持子命令「{command}」，可用的有：{'、'.join(cmds)}")
        a = check_args(args.get("args") or [])
        if prepare:
            a = prepare(command, a)
        return run(binary, command, a, login_hint=login_hint, timeout=timeout)
    except CLIError as e:
        return {"status": "error", "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}


def describe(label: str, args: dict) -> str:
    text = f"{label} {args.get('command', '')} {' '.join(str(a) for a in args.get('args') or [])}".strip()
    return text if len(text) <= 80 else text[:80] + "…"
