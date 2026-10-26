"""跨任务经验：任务完成后让模型用一两句话总结「怎么做成的 / 踩了什么坑」，下次遇到相似指令时作为参考附上。

参考 Agent-S 的 narrative memory。相似度用字符二元组的 Jaccard，不需要向量模型。
只存在本机：~/.computer-use/experiences.jsonl。设置 COMPUTER_USE_MEMORY=off 可关闭。
"""

import json
import os
import time

from paths import state_dir

MIN_SCORE = 0.3
MAX_ENTRIES = 500

SUMMARY_PROMPT = """你在帮一个操作 macOS 电脑的助手积累经验。根据下面这次任务的指令、执行过的动作和结果，
用中文写 1~2 句可复用的经验：完成这类任务的关键路径或要避开的坑（例如该用哪个应用、哪个菜单、哪种快捷键）。
不要复述具体的个人数据（人名、消息内容、账号、地址等），不要写客套话。
如果这次任务太简单、没有值得记住的经验，只回复「无」。"""


def enabled() -> bool:
    return os.environ.get("COMPUTER_USE_MEMORY", "on").lower() not in ("off", "0", "false")


def _path() -> str:
    return os.path.join(state_dir(), "experiences.jsonl")


def _grams(s: str) -> set[str]:
    s = "".join(s.lower().split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def similarity(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def _load() -> list[dict]:
    out = []
    try:
        with open(_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def recall(task: str, k: int = 2) -> list[dict]:
    if not enabled():
        return []
    scored = [(similarity(task, e.get("task", "")), e) for e in _load() if e.get("lesson")]
    scored = [x for x in scored if x[0] >= MIN_SCORE]
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:k]]


def with_hints(task: str) -> str:
    hits = recall(task)
    if not hits:
        return task
    lines = "\n".join(f"- {e['lesson']}" for e in hits)
    return f"{task}\n\n（以往类似任务的经验，仅供参考，以当前屏幕为准：\n{lines}）"


def remember(client, model: str, task: str, actions: list[str], final: str) -> str | None:
    """让模型总结经验并保存。返回经验文本；没有值得记的返回 None。"""
    if not enabled():
        return None
    transcript = json.dumps({"task": task, "actions": actions[-40:], "result": final[:1000]}, ensure_ascii=False)
    resp = client.responses.create(model=model, instructions=SUMMARY_PROMPT, input=transcript, store=False)
    lesson = (resp.output_text or "").strip()
    if not lesson or lesson.startswith("无") or len(lesson) > 300:
        return None
    entries = _load()[-(MAX_ENTRIES - 1):] + [{"task": task, "lesson": lesson, "ts": time.time()}]
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    os.replace(tmp, _path())
    return lesson
