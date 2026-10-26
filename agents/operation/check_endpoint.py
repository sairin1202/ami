"""检查当前配置：GPT 接口是否支持 Responses API 的 computer 工具；填了 TYPESAFE_API_KEY 时也检查 Jev。

用法: .venv/bin/python check_endpoint.py   （读取 .env 中的 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL / TYPESAFE_API_KEY）
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from agent import DEFAULT_MODEL

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def check_gpt() -> bool:
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    print(f"[GPT] base_url={base_url or '(OpenAI 官方)'}  model={model}")
    client = OpenAI(base_url=base_url)
    try:
        resp = client.responses.create(
            model=model,
            tools=[{"type": "computer"}],
            input="Take a screenshot to see the current screen. Use the computer tool.",
        )
    except Exception as e:  # noqa: BLE001
        print(f"✗ 请求失败: {type(e).__name__}: {e}")
        return False
    print("output 类型:", [item.type for item in resp.output])
    calls = [i for i in resp.output if i.type == "computer_call"]
    if calls:
        print("✓ 支持 computer 工具，返回动作:", [a.type for a in (calls[0].actions or [])])
        return True
    print("✗ 没有返回 computer_call，这个接口/模型可能不支持 computer 工具")
    return False


def check_jev() -> bool:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        print("[Jev] 未设置 TYPESAFE_API_KEY，跳过（Jev 加速不可用）")
        return True
    import axui
    import jev

    # 用一张假的控件表，不读屏幕、不执行任何动作
    def el(i, role, label, ops):
        return axui.Element(index=str(i), ref=None, role=role, label=label, value=None,
                            frame=(0, 0, 10, 10), operations=ops)
    snap = axui.Snapshot("TextEdit", "com.apple.TextEdit", 0, "未命名", [
        el(1, "AXButton", "关闭", ["CLICK"]),
        el(2, "AXButton", "粗体", ["CLICK"]),
        el(3, "AXTextArea", "正文", ["TYPE_TEXT", "CLICK"]),
    ], "")
    client = jev.JevClient(key, os.environ.get("TYPESAFE_URL"), os.environ.get("TYPESAFE_MODEL"))
    print(f"[Jev] url={client.url}  model={client.model}")
    try:
        d = client.choose("在正文里写一句 hello", snap, [])
    except jev.JevError as e:
        print(f"✗ {e}")
        return False
    target = d["element"].describe() if d["element"] else ""
    print(f"✓ Jev 可用：{d['op']} {target}  置信度 {d['confidence']:.2f}  {d['latency_ms']}ms")
    return True


if __name__ == "__main__":
    ok = check_jev()
    ok = check_gpt() and ok
    sys.exit(0 if ok else 1)
