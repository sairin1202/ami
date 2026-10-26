"""每次运行生成一份 HTML 报告：每一步的时间、类型、内容，以及缩小后的截图，方便事后排查。

参考 midscene / openai-cua-sample-app 的运行回放。报告只存在本机：~/.computer-use/runs/。
"""

import base64
import html
import io
import os
import time

from PIL import Image

from paths import state_dir

MAX_SHOTS = 60          # 报告里最多保留的截图张数
SHOT_WIDTH = 640        # 截图缩小到的宽度
KEEP_REPORTS = 50       # 最多保留最近多少份报告

STYLE = {
    "user": "#2563eb", "assistant": "#16a34a", "thinking": "#9ca3af", "action": "#d97706",
    "status": "#6b7280", "error": "#dc2626", "stats": "#7c3aed",
}


def runs_dir() -> str:
    return state_dir("runs")


class RunRecorder:
    def __init__(self, task: str):
        self.task = task
        self.started = time.time()
        self.events: list[tuple[float, str, str]] = []
        self.shots = 0

    def log(self, kind: str, text: str) -> None:
        if kind == "progress":
            return
        self.events.append((time.time() - self.started, kind, text))

    def screenshot(self, data: bytes) -> None:
        if self.shots >= MAX_SHOTS:
            return
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((SHOT_WIDTH, SHOT_WIDTH))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
        except Exception:  # noqa: BLE001
            return
        self.shots += 1
        self.events.append((time.time() - self.started, "screenshot", base64.b64encode(buf.getvalue()).decode()))

    def actions(self) -> list[str]:
        return [t for _, k, t in self.events if k == "action"]

    def write(self) -> str:
        rows = []
        for t, kind, text in self.events:
            if kind == "screenshot":
                body = f'<img src="data:image/jpeg;base64,{text}" alt="截图">'
            else:
                body = html.escape(text).replace("\n", "<br>")
            color = STYLE.get(kind, "#333")
            rows.append(f'<tr><td class="t">{t:6.1f}s</td><td class="k" style="color:{color}">{kind}</td>'
                        f'<td>{body}</td></tr>')
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started))
        page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>运行报告 {stamp}</title>
<style>
body{{font:14px -apple-system,"PingFang SC",sans-serif;margin:16px;color:#111;background:#fff}}
h1{{font-size:18px}} table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #eee;padding:6px 8px;vertical-align:top}}
td.t{{color:#999;white-space:nowrap;font-variant-numeric:tabular-nums}} td.k{{white-space:nowrap;font-weight:600}}
img{{max-width:100%;border:1px solid #ddd;border-radius:4px}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}td{{border-color:#333}}}}
</style></head><body>
<h1>{html.escape(self.task[:200])}</h1><p>开始于 {stamp}，共 {time.time() - self.started:.1f} 秒</p>
<table>{''.join(rows)}</table></body></html>"""
        path = os.path.join(runs_dir(), time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started)) + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        old = sorted(n for n in os.listdir(runs_dir()) if n.endswith(".html"))
        for n in old[:-KEEP_REPORTS]:
            try:
                os.remove(os.path.join(runs_dir(), n))
            except OSError:
                pass
        return path
