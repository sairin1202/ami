"""Computer use 主循环：发送任务 → 执行模型返回的动作 / 函数调用 → 回传截图或结果，直到模型不再调用工具。"""

import concurrent.futures
import json
import os
import platform
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import openai
from openai import OpenAI

import action_cache
import amients
import computer
import jev
import memory
import paths
import report
import tools

DEFAULT_MODEL = "gpt-6-luna"

# 模型请求：流式接收，连续 MODEL_TIMEOUT 秒没内容算卡住，失败只重试 MODEL_RETRIES 次
# （之前是一次性等全部生成完，180 秒 × 4 次，中转出问题时会干等十几分钟）
MODEL_TIMEOUT = float(os.environ.get("MODEL_TIMEOUT", "90"))       # 流式接收时：连续多少秒没有新内容算卡住
MODEL_MAX_SECONDS = float(os.environ.get("MODEL_MAX_SECONDS", "300"))  # 单次回复的总时长上限
MODEL_RETRIES = int(os.environ.get("MODEL_RETRIES", "1"))
PROGRESS_EVERY = 3  # 等模型时每隔几秒更新一次状态栏
# 正常 2~4 秒就会收到第一个事件。超过这么久一个事件都没有，多半是中转卡在了某段内容上（实测网页正文 + 链接的组合
# 会让 nevatoken 一声不响地挂起），这时把历史里过长的工具结果精简后重试一次，不干等到超时
FIRST_EVENT_TIMEOUT = float(os.environ.get("FIRST_EVENT_TIMEOUT", "12"))
STALL_TIMEOUT = float(os.environ.get("STALL_TIMEOUT", "25"))      # 回复中途多久没有新内容算卡住
MAX_ATTEMPTS = int(os.environ.get("MODEL_ATTEMPTS", "3"))          # 卡住时最多发几次
SHRINK_TOOL_OUTPUT = 3000

# 对话历史里只保留最近几张截图，更早的替换成占位小图，控制请求体积
KEEP_IMAGES = 3
# 历史（不算图片）超过这么多字符时，把较早的部分压缩成摘要；最近的若干条原样保留
SUMMARY_TRIGGER_CHARS = int(os.environ.get("HISTORY_SUMMARY_CHARS", "150000"))
KEEP_RECENT_ITEMS = 16
_PLACEHOLDER = computer.placeholder_data_url()


SOUL_PATH = paths.resource("SOUL.md")


def load_soul() -> str:
    """助理的人设（SOUL.md）：只影响和用户说话的语气。COMPUTER_USE_SOUL 可指定别的文件，设为 off 关闭。"""
    path = os.environ.get("COMPUTER_USE_SOUL") or SOUL_PATH
    if path.strip().lower() == "off":
        return ""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            soul = f.read().strip()
    except OSError:
        return ""
    if not soul:
        return ""
    return ("\n\n# 你的人设\n下面的人设只决定你和用户说话的语气；和上面的规则冲突时，以上面的规则为准。"
            "替用户发出去的内容按人设里「替用户发消息」那一节来。\n\n" + soul)


def build_instructions() -> str:
    w, h = computer.image_size()
    return f"""你在用户自己的 macOS 电脑上通过 computer 工具操作图形界面，帮用户完成任务。
- 屏幕截图尺寸 {w}x{h}，坐标以截图像素为准。系统: macOS {platform.mac_ver()[0]}。
- macOS 快捷键用 CMD（不是 CTRL）。打开应用可以用 CMD+SPACE 唤起 Spotlight，输入应用名后回车。
- 做一小组动作后检查截图确认效果，不要盲目连续操作。
- 所有操作（发消息、发帖、提交、删除等）都直接执行，不需要先问用户、等用户确认。
- 任务完成或需要用户决定时，不要再调用工具，直接用中文简短回复结果或问题。""" + tools.rules() + load_soul()





@dataclass
class AgentCallbacks:
    log: Callable[[str, str], None]                  # (kind, text)
    screenshot: Callable[[bytes], None] = lambda png: None


WECHAT_NAMES = re.compile(r"微信|wechat|weixin", re.I)
WECHAT_SPOTLIGHT = ("不要自己用 Spotlight（⌘空格）打开微信，已关掉 Spotlight。读 / 发微信用 wechat_read / wechat_send，"
                    "要看微信界面先调用 wechat_open。它们都会先检查微信有没有被锁定或风控。")


def wechat_guard(a, state: dict) -> str | None:
    """computer 工具每个动作执行前的微信检查，返回拦下的原因（None 表示放行）。
    - 不许用 Spotlight 打开微信（⌘空格 之后输入「微信」）
    - 最前面是微信时，先确认没有暂停、没被锁定、没有安全风控弹窗；有就不再碰它（窗口会被收起来）
    state 在一次运行里共用，记着上一个动作是不是 ⌘空格。"""
    kind = computer._get(a, "type")
    if kind in ("screenshot", "wait", "move"):
        return None
    spotlight, state["spotlight"] = state.get("spotlight", False), False
    if kind == "keypress":
        keys = {str(k).upper() for k in computer._get(a, "keys") or []}
        if "SPACE" in keys and keys & {"CMD", "COMMAND", "META", "SUPER"}:
            state["spotlight"] = True
            return None
    if kind == "type" and spotlight and WECHAT_NAMES.search(computer._get(a, "text") or ""):
        computer.execute({"type": "keypress", "keys": ["ESC"]})
        return WECHAT_SPOTLIGHT
    import wechat
    try:
        f = computer.frontmost_app()
        if f and f[1] == wechat.BUNDLE_ID:
            wechat.guard()
    except wechat.WeChatError as e:
        return str(e)
    except Exception:  # noqa: BLE001  看不了最前面是谁：不拦
        pass
    return None


MINIMIZE_AFTER_RUN = os.environ.get("MINIMIZE_AFTER_RUN", "on").strip().lower() not in ("off", "0", "false", "no")


class UsedApps:
    """一次运行里被切到最前面过的应用（每个动作后看一眼最前面是谁）。运行结束时把它们的窗口都最小化，
    浏览器也一样（网页窗口收进 Dock）。开始时就在最前面的那个应用，只有在它在最前面时做过动作（真的操作过它）才最小化，
    光是截图时看到它不算；本程序和 Finder 等不动。"""

    def __init__(self):
        self.start = self._front()
        self.apps: dict[int, str] = {}

    @staticmethod
    def _front():
        try:
            return computer.frontmost_app()
        except Exception:  # noqa: BLE001
            return None

    def seen(self, acting: bool = False) -> None:
        f = self._front()
        if f is None or f[0] == os.getpid() or f[1] in computer.KEEP_OPEN_BUNDLES:
            return
        if not acting and self.start is not None and f[0] == self.start[0]:
            return
        self.apps[f[0]] = f[2]

    def minimize(self) -> list[str]:
        if not MINIMIZE_AFTER_RUN:
            return []
        done = []
        for pid, name in self.apps.items():
            try:
                if computer.minimize_windows(pid):
                    done.append(name)
            except Exception:  # noqa: BLE001  应用已经退出等
                pass
        return done


@dataclass
class Agent:
    api_key: str
    model: str = DEFAULT_MODEL
    max_steps: int = 80
    base_url: str | None = None
    # Jev 快速模式：先用 AX 控件表 + Jev 快速操作，卡住或认为完成时再交给 GPT 截图循环
    use_jev: bool = False
    # 完整对话历史保存在本地（很多中转不支持 previous_response_id），每次请求整段发送
    history: list = field(default_factory=list)
    # 被中途停止时尚未回传的 computer_call_output，下次 run 时补上，保证对话可继续
    _pending: list = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _gpt_calls: int = 0
    _jev_steps: int = 0
    _gpt_effective: int = 0     # 本次 GPT 实际执行的操作数（不含截图 / 等待），用于判断能否缓存
    _cache_candidate: tuple | None = None
    _cb: AgentCallbacks | None = None
    # 人工接管：暂停后用户自己操作，继续时把用户做了什么告诉模型
    _paused: threading.Event = field(default_factory=threading.Event)
    _resume_note: str = ""
    _recent_sigs: list = field(default_factory=list)
    _recent_frames: list = field(default_factory=list)
    _nudge_cooldown: int = 0
    _stream_ok: bool = True                       # 中转是否支持流式（不支持时自动退回普通请求）
    # 上报给 amients.com 时标明是谁发起的：user（你在输入框下的指令）或工作流 id（例如 inbox）
    origin: str = "user"

    def __post_init__(self):
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url or None,
                             max_retries=MODEL_RETRIES, timeout=MODEL_TIMEOUT)
        self.jev = jev.from_env(self.client, self.model)
        self.tools = [{"type": "computer"}] + tools.specs()

    def _run_jev(self, task: str, cb: AgentCallbacks) -> str | None:
        """先查动作缓存，再跑 Jev 快速模式。返回交给 GPT 的指令；返回 None 表示已停止。"""
        self._cache_candidate = None
        if not (self.use_jev and self.jev) or self._pending:
            return task
        if tools.skip_jev(task):
            return task  # 微信、Reddit、linux.do、小红书等有专门的工具，Jev 帮不上忙，直接交给 GPT + 工具

        prefix: list[str] = []
        cached = action_cache.get(task)
        if cached:
            cb.log("status", f"命中动作缓存（{len(cached)} 步），直接回放…")
            rr = action_cache.replay(task, cached, cb.log, self._stop)
            if self._stop.is_set():
                cb.log("status", "已停止")
                return None
            if rr.ok:
                self._jev_steps = len(rr.actions)
                self._cache_candidate = (task, cached, True)
                cb.log("status", "缓存回放完成，交给 GPT 核实")
                return self._verify_prompt(task, rr.actions)
            action_cache.invalidate(task)
            cb.log("status", f"缓存回放中断（{rr.reason}），已作废这条缓存，改用 Jev")
            prefix = rr.actions

        cb.log("status", "Jev 快速模式…")
        self.jev.text_fn = jev.make_text_fn(self.client, os.environ.get("JEV_TEXT_MODEL") or self.model)
        r = self.jev.run(task, cb.log, self._stop)
        actions = prefix + r.actions
        self._jev_steps = len(actions)
        if r.status == "stopped":
            cb.log("status", "已停止")
            return None
        if not actions and r.status != "done":
            cb.log("status", f"Jev 未执行动作（{r.reason or r.status}），交给 GPT")
            return task
        if r.status == "done":
            if not prefix:  # 只缓存从头到尾都由 Jev 完成的步骤
                self._cache_candidate = (task, r.steps, False)
            cb.log("status", f"Jev 认为已完成（{len(actions)} 步），交给 GPT 核实")
            return self._verify_prompt(task, actions)
        cb.log("status", f"Jev 停下（{r.reason or r.status}），交给 GPT 继续")
        return (f"{task}\n\n（快速模式已经在当前屏幕上执行了：{'；'.join(actions)}。"
                f"它在这里停下：{r.reason or r.status}。请先截图看当前状态，从这里继续完成任务，不要重复已完成的步骤。）")

    @staticmethod
    def _verify_prompt(task: str, actions: list[str]) -> str:
        return (f"{task}\n\n（快速模式已经在当前屏幕上执行了：{'；'.join(actions) or '无'}。它判断任务已完成。"
                "请先截图核实：确实完成就直接简短回复结果；没完成就从当前状态继续；"
                "如果没完成又无法继续，回复里写明「未完成」。）")

    def _after_success(self, final: str, cb: AgentCallbacks) -> None:
        """GPT 核实时没有再做任何操作 → 快速模式的步骤确实完成了任务，写入动作缓存；
        回放的缓存还需要 GPT 补操作 → 缓存已经不够用，作废。"""
        if not self._cache_candidate:
            return
        task, steps, replayed = self._cache_candidate
        clean = self._gpt_effective == 0 and "未完成" not in final
        if replayed and not clean:
            action_cache.invalidate(task)
            cb.log("status", "缓存的步骤这次没能独立完成任务，已作废")
        elif not replayed and clean and steps:
            action_cache.put(task, steps)
            cb.log("status", f"已记住这条指令的 {len(steps)} 个步骤，下次相同指令会直接回放")

    def stop(self):
        self._stop.set()
        self._paused.clear()

    def pause(self):
        self._paused.set()

    def resume(self, note: str = ""):
        self._resume_note = note
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _wait_if_paused(self, next_input: list, cb: AgentCallbacks) -> bool:
        """暂停时阻塞，直到继续或停止。继续后把截图换成最新的，并告诉模型用户手动做了什么。返回是否被停止。"""
        if not self._paused.is_set():
            return False
        cb.log("status", "已暂停：你可以自己操作电脑，完成后点「继续」")
        while self._paused.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        if self._stop.is_set():
            return True
        url, png = computer.screenshot_data_url()
        cb.screenshot(png)
        for item in next_input:
            if isinstance(item, dict) and item.get("type") == "computer_call_output":
                item["output"]["image_url"] = url
        note = self._resume_note.strip() or "（没有记录到具体操作）"
        next_input.append({"role": "user", "content":
                           f"我刚才暂停了你，自己手动操作了电脑：\n{note}\n请以当前屏幕为准继续完成任务，不要撤销我的操作。"})
        cb.log("status", "继续执行")
        return False

    # ------------------------------------------------------------------
    # 卡住检测（参考 browser-use 的 ActionLoopDetector）：重复同样的动作，或做了动作但屏幕没变化
    @staticmethod
    def _action_sig(a) -> str:
        t = computer._get(a, "type")
        x, y = computer._get(a, "x"), computer._get(a, "y")
        pos = f"{round(x / 30)},{round(y / 30)}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) else ""
        return f"{t}|{pos}|{computer._get(a, 'text', '')}|{'+'.join(computer._get(a, 'keys') or [])}"

    @staticmethod
    def _frame(png: bytes):
        from PIL import Image
        import io
        return list(Image.open(io.BytesIO(png)).convert("L").resize((48, 27)).getdata())

    def _loop_nudge(self, sigs: list[str], png: bytes | None) -> str | None:
        if self._nudge_cooldown > 0:
            self._nudge_cooldown -= 1
        effective = [x for x in sigs if not x.startswith(("screenshot", "wait", "move"))]
        self._recent_sigs.extend(effective)
        self._recent_sigs = self._recent_sigs[-8:]
        if png is not None and effective:
            self._recent_frames.append(self._frame(png))
            self._recent_frames = self._recent_frames[-3:]
        if self._nudge_cooldown > 0:
            return None
        tail = self._recent_sigs[-6:]
        if tail and max(tail.count(x) for x in tail) >= 3:
            msg = "最近几步在重复同样的操作"
        elif len(self._recent_frames) == 3 and all(
                sum(abs(p - q) for p, q in zip(f, self._recent_frames[0])) / len(f) < 1.5
                for f in self._recent_frames[1:]):
            msg = "最近几次操作后屏幕几乎没有变化，操作可能没有生效"
        else:
            return None
        self._nudge_cooldown = 4
        self._recent_sigs, self._recent_frames = [], []
        return f"（系统提示：{msg}。请换一种方法，比如换个位置点击、用键盘快捷键或菜单；如果确实做不到，停下来告诉用户遇到了什么问题。）"

    def reset(self):
        self.history = []
        self._pending = []

    @staticmethod
    def _screenshot_output(call_id, url):
        return {
            "type": "computer_call_output",
            "call_id": call_id,
            "output": {"type": "computer_screenshot", "image_url": url, "detail": "original"},
        }

    # ------------------------------------------------------------------
    def _trim_images(self):
        seen = 0
        for item in reversed(self.history):
            if not isinstance(item, dict):
                continue
            holders = []
            if item.get("type") == "computer_call_output":
                holders = [item["output"]]
            elif isinstance(item.get("content"), list):
                holders = [c for c in item["content"] if isinstance(c, dict) and c.get("type") == "input_image"]
            for h in holders:
                if h.get("image_url") == _PLACEHOLDER:
                    continue
                seen += 1
                if seen > KEEP_IMAGES:
                    h["image_url"] = _PLACEHOLDER

    # ------------------------------------------------------------------
    # 历史摘要：中转不支持 previous_response_id，每轮都要重发完整历史；历史太长时把较早的部分压成一条摘要
    def _history_chars(self) -> int:
        def strip(o):
            if isinstance(o, dict):
                return {k: ("" if k in ("image_url", "encrypted_content") else strip(v)) for k, v in o.items()}
            if isinstance(o, list):
                return [strip(v) for v in o]
            return o
        return len(json.dumps(strip(self.history), ensure_ascii=False))

    @staticmethod
    def _transcript(items: list) -> str:
        lines = []
        for it in items:
            t = it.get("type") if isinstance(it, dict) else None
            if isinstance(it, dict) and it.get("role") in ("user", "assistant"):
                c = it.get("content")
                text = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("text"))
                lines.append(f"{'用户' if it['role'] == 'user' else '助手'}: {text}")
            elif t == "computer_call":
                acts = it.get("actions") or ([it["action"]] if it.get("action") else [])
                lines.append("动作: " + "; ".join(computer.describe(a) for a in acts))
            elif t == "function_call":
                lines.append(f"调用 {it.get('name')}: {it.get('arguments', '')[:300]}")
            elif t == "function_call_output":
                lines.append(f"结果: {str(it.get('output', ''))[:300]}")
        return "\n".join(lines)

    def _maybe_summarize(self, cb: AgentCallbacks | None) -> None:
        if self._history_chars() <= SUMMARY_TRIGGER_CHARS or len(self.history) <= KEEP_RECENT_ITEMS + 2:
            return
        outputs = ("computer_call_output", "function_call_output")
        cut = len(self.history) - KEEP_RECENT_ITEMS
        # 切点之后不能以「调用结果」开头（它的调用会被摘要掉，接口会报错）
        while cut > 1 and isinstance(self.history[cut], dict) and self.history[cut].get("type") in outputs:
            cut -= 1
        while cut > 1 and isinstance(self.history[cut], dict) and self.history[cut].get("type") == "reasoning":
            cut -= 1  # reasoning 必须和它后面的输出一起保留
        if cut <= 1:
            return
        transcript = self._transcript(self.history[:cut])[-60000:]
        resp = self.client.responses.create(
            model=self.model, store=False, input=transcript,
            instructions="把下面这段电脑操作助手和用户的对话压缩成要点：用户要求了什么、已经完成了哪些步骤、"
                         "当前停在哪里、还有什么没做、用户的偏好或拒绝过的操作。用中文，不超过 400 字。")
        summary = (resp.output_text or "").strip()
        if not summary:
            return
        self.history = [{"role": "user", "content": [{"type": "input_text",
                                                       "text": f"（较早的对话已压缩成摘要）\n{summary}"}]}] \
            + self.history[cut:]
        if cb:
            cb.log("status", f"对话较长，已把较早的 {cut} 条记录压缩成摘要")

    def _create(self, new_items):
        self._gpt_calls += 1
        self.history.extend(new_items)
        try:
            self._maybe_summarize(self._cb)
        except Exception:  # noqa: BLE001  摘要失败不影响主流程，照常发送完整历史
            pass
        self._trim_images()
        kwargs = dict(model=self.model, tools=self.tools, instructions=build_instructions(), input=list(self.history),
                      store=False, include=["reasoning.encrypted_content"])
        # 请求放到后台线程里流式接收：只要模型还在往回传内容就继续等，连续 MODEL_TIMEOUT 秒没有新内容才算卡住；
        # 等待期间状态栏显示进度，用户点停止时立刻放弃，不用干等到超时
        attempt, started_all, stuck_ids = 1, time.time(), []

        def submit():
            live = {"last": time.time(), "sent": time.time(), "chars": 0, "events": 0, "stream": None}
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(self._request, kwargs, live)
            pool.shutdown(wait=False)  # 卡住的请求留在自己的线程里自行结束，重发用新线程，不用排队
            return fut, live

        future, live = submit()
        started, last_tick = time.time(), 0
        while True:
            try:
                resp = future.result(timeout=0.5)
                break
            except concurrent.futures.TimeoutError:
                pass
            except openai.APITimeoutError:
                raise RuntimeError(f"模型连续 {MODEL_TIMEOUT:.0f} 秒没有任何回复（中转可能很慢），已放弃；"
                                   "可以点「运行」重试") from None
            now = time.time()
            if self._stop.is_set():
                self._close_stream(live)
                return None
            # 中转会随机卡住（实测约 4 成的请求要么迟迟不开始、要么回到一半停住），正常请求 1~4 秒就开始回复；
            # 所以卡住时快速放弃、马上重发，而不是干等到超时。回复完整之前不会执行任何动作，重发是安全的。
            no_start = self._stream_ok and live["events"] == 0 and now - started > FIRST_EVENT_TIMEOUT
            stalled = self._stream_ok and live["events"] > 0 and now - live["last"] > STALL_TIMEOUT
            if no_start or stalled:
                ids = self._ids(live)
                if ids:
                    stuck_ids.append(ids)
            if (no_start or stalled) and attempt < MAX_ATTEMPTS:
                self._close_stream(live)
                attempt += 1
                shrunk = attempt == 2 and self._shrink_history()  # 第 2 次起顺便精简过长的网页结果
                if shrunk:
                    kwargs["input"] = list(self.history)
                if self._cb:
                    why = (f"{FIRST_EVENT_TIMEOUT:.0f} 秒没有开始回复" if no_start
                           else f"回复中途 {STALL_TIMEOUT:.0f} 秒没有新内容")
                    self._cb.log("status", f"模型 {why}（中转卡住了），第 {attempt} 次重发"
                                           + ("，已精简过长的网页内容" if shrunk else "")
                                           + (f"。卡住的请求：{stuck_ids[-1]}" if stuck_ids and stuck_ids[-1] else ""))
                future, live = submit()
                started, last_tick = time.time(), 0
                continue
            if (no_start or stalled) or now - started_all > MODEL_MAX_SECONDS:
                self._close_stream(live)
                raise RuntimeError(f"模型请求连续 {MAX_ATTEMPTS} 次都卡住了（中转不稳定），已放弃；"
                                   "可以稍后点「运行」重试，或换一个更稳定的接口地址。"
                                   + (f"卡住的请求（可报给中转排查）：{'；'.join(stuck_ids)}" if stuck_ids
                                      else "卡住的请求都没收到中转返回的 ID（连接阶段就卡住了）"))
            waited = int(now - started)
            if self._cb and waited >= last_tick + PROGRESS_EVERY:
                last_tick = waited
                tag = f"第 {attempt} 次，" if attempt > 1 else ""
                if live["chars"]:
                    msg = f"模型正在回复… {tag}{waited} 秒（已收到 {live['chars']} 字，可点「停止」）"
                else:
                    msg = f"等待模型开始回复… {tag}{waited} 秒（可点「停止」）"
                self._cb.log("progress", msg)
        self.history.extend(item.model_dump(exclude_none=True) for item in resp.output)
        return resp

    @staticmethod
    def _ids(live: dict) -> str:
        parts = []
        if live.get("request_id"):
            parts.append(f"request id {live['request_id']}")
        if live.get("response_id"):
            parts.append(f"response {live['response_id']}")
        return "，".join(parts) + (f"，{time.strftime('%H:%M:%S', time.localtime(live['sent']))} 发出" if parts and live.get("sent") else "")

    @staticmethod
    def _close_stream(live: dict) -> None:
        stream = live.get("stream")
        if stream is not None:
            try:
                stream.close()  # 断开连接，后台线程随之结束
            except Exception:  # noqa: BLE001
                pass

    def _shrink_history(self) -> bool:
        """把历史里过长的函数工具结果（主要是网页内容）精简：去掉链接、正文截短。返回是否有改动。"""
        changed = False
        for item in self.history:
            if not (isinstance(item, dict) and item.get("type") == "function_call_output"):
                continue
            out = item.get("output")
            if not isinstance(out, str) or len(out) <= SHRINK_TOOL_OUTPUT:
                continue
            try:
                data = json.loads(out)
            except ValueError:
                data = None
            if isinstance(data, dict):
                data.pop("links", None)
                for k, v in data.items():
                    if isinstance(v, str) and len(v) > SHRINK_TOOL_OUTPUT:
                        data[k] = v[:SHRINK_TOOL_OUTPUT] + "…（内容过长，已截断）"
                new = json.dumps(data, ensure_ascii=False)
            else:
                new = out[:SHRINK_TOOL_OUTPUT] + "…（内容过长，已截断）"
            if new != out:
                item["output"] = new
                changed = True
        return changed

    def _request(self, kwargs: dict, live: dict):
        """发请求并流式接收，返回完整的 response。中转不支持流式时退回普通请求。"""
        if not self._stream_ok:
            return self.client.responses.create(**kwargs)
        try:
            raw_api = getattr(self.client.responses, "with_raw_response", None)
            if raw_api is not None:  # 拿响应头里的请求 ID，卡住时写进日志，方便报给中转排查
                raw = raw_api.create(**kwargs, stream=True)
                live["request_id"] = raw.headers.get("x-oneapi-request-id") or raw.headers.get("x-request-id") or ""
                stream = raw.parse()
            else:
                stream = self.client.responses.create(**kwargs, stream=True)
        except (openai.BadRequestError, openai.UnprocessableEntityError, openai.NotFoundError):
            self._stream_ok = False
            return self.client.responses.create(**kwargs)
        if hasattr(stream, "output"):  # 中转忽略了 stream 参数，直接返回了完整结果
            return stream
        live["stream"] = stream
        final = None
        for ev in stream:
            live["last"] = time.time()
            live["events"] = live.get("events", 0) + 1
            delta = getattr(ev, "delta", None)
            if isinstance(delta, str):
                live["chars"] += len(delta)
            if ev.type == "response.created":
                live["response_id"] = getattr(getattr(ev, "response", None), "id", "") or ""
            if ev.type == "response.completed":
                final = ev.response
            elif ev.type in ("response.failed", "response.incomplete", "error"):
                err = getattr(getattr(ev, "response", None), "error", None) or getattr(ev, "message", "")
                raise RuntimeError(f"模型回复失败：{err or ev.type}")
        if final is None:
            raise RuntimeError("模型回复中断（没有收到完整结果），可以点「运行」重试")
        return final

    def _report_output(self, resp, cb: AgentCallbacks) -> str:
        final_text = []
        for item in resp.output:
            if item.type == "message":
                for c in item.content or []:
                    text = getattr(c, "text", None)
                    if text:
                        final_text.append(text)
                        cb.log("assistant", text)
            elif item.type == "reasoning":
                for s in getattr(item, "summary", None) or []:
                    text = getattr(s, "text", None)
                    if text:
                        cb.log("thinking", text)
        return "\n".join(final_text)

    def _stopped_outputs(self, resp) -> list:
        """被停止时，给模型尚未得到结果的调用补上输出，保证下次可以接着对话。"""
        out = []
        calls = [i for i in resp.output if i.type == "computer_call"]
        if calls:
            url, _ = computer.screenshot_data_url()
            out += [self._screenshot_output(c.call_id, url) for c in calls]
        out += [{"type": "function_call_output", "call_id": f.call_id,
                 "output": json.dumps({"status": "stopped", "detail": "用户停止了任务，未执行"}, ensure_ascii=False)}
                for f in resp.output if f.type == "function_call"]
        return out

    def _run_function(self, fc, cb: AgentCallbacks) -> dict:
        try:
            args = json.loads(fc.arguments or "{}")
        except ValueError:
            args = {}
        desc = tools.describe(fc.name, args)
        cb.log("action", desc)
        result = tools.call(fc.name, args)
        status = result.get("status")
        if status == "error":
            cb.log("error", result["detail"])
        elif "messages" in result:
            cb.log("status", f"读到 {len(result['messages'])} 条消息")
        elif "posts" in result:
            cb.log("status", f"读到 {len(result['posts'])} 个帖子")
        elif "comment_list" in result:
            cb.log("status", f"读到帖子《{result.get('title', '')[:40]}》和 {len(result['comment_list'])} 条评论")
        elif status == "posted":
            cb.log("status", f"已发布：{result.get('url', '')}")
        elif result.get("detail"):
            cb.log("status", result["detail"])
        return result

    def run(self, task: str, cb: AgentCallbacks) -> str:
        """执行一条指令，阻塞直到完成/停止。可多次调用以在同一对话中继续。"""
        self._gpt_calls = self._jev_steps = self._gpt_effective = 0
        started = time.time()
        rec = report.RunRecorder(task)
        outer = cb
        run_id = amients.new_run_id()
        amients.emit("run_start", task, run_id, self.origin, {"model": self.model})
        amients.set_status("running", task if self.origin == "user" else f"后台「{self.origin}」：{task.splitlines()[0]}",
                           origin=self.origin, run_id=run_id, model=self.model)
        used = UsedApps()
        cb = AgentCallbacks(log=lambda k, t: (rec.log(k, t), outer.log(k, t), amients.emit(k, t, run_id, self.origin),
                                              used.seen(acting=True) if k == "action" else None),
                            screenshot=lambda png: (rec.screenshot(png), outer.screenshot(png), used.seen()))
        final = ""
        try:
            final = self._run(task, cb)
            return final
        finally:
            try:
                stuck = computer.release_inputs()
                if stuck:
                    cb.log("status", f"已松开仍按着的按键: {', '.join(stuck)}")
            except Exception:  # noqa: BLE001
                pass
            used.seen()
            minimized = used.minimize()
            if minimized:
                cb.log("status", f"已把用过的应用最小化：{'、'.join(minimized)}")
            self._learn(task, final, rec, cb)
            parts = [f"用时 {time.time() - started:.1f} 秒"]
            if self.use_jev and self.jev:
                parts.append(f"Jev {self._jev_steps} 步")
            parts.append(f"GPT 请求 {self._gpt_calls} 次")
            cb.log("stats", " · ".join(parts))
            amients.set_status("idle", final or ("已停止" if self._stop.is_set() else "完成"), origin=self.origin,
                               run_id=run_id, model=self.model, seconds=round(time.time() - started, 1))
            amients.emit("run_end", final, run_id, self.origin,
                         {"seconds": round(time.time() - started, 1), "model_calls": self._gpt_calls,
                          "stopped": self._stop.is_set()})
            try:
                outer.log("report", rec.write())
            except OSError as e:
                outer.log("error", f"写运行报告失败: {e}")

    def _learn(self, task: str, final: str, rec, cb: AgentCallbacks) -> None:
        """有一定步数、正常结束的任务，总结一条经验留给以后的相似任务（失败不影响主流程）。"""
        if self._stop.is_set() or not final or self._gpt_calls < 3 or not memory.enabled():
            return
        try:
            lesson = memory.remember(self.client, self.model, task, rec.actions(), final)
            if lesson:
                cb.log("status", f"记下经验：{lesson}")
        except Exception:  # noqa: BLE001
            pass

    def _run(self, task: str, cb: AgentCallbacks) -> str:
        self._stop.clear()
        self._cb = cb
        self._recent_sigs, self._recent_frames, self._nudge_cooldown = [], [], 0
        self._wechat_state = {}
        task = self._run_jev(task, cb)
        if task is None:
            return ""
        task = memory.with_hints(task)
        content = [{"type": "input_text", "text": task}]
        # 已有 computer 截图回传时，接口只允许整个对话里有一张用户图片，
        # 所以只在新对话的第一条指令附截图；之后模型会自己请求 screenshot
        if not self.history:
            url, png = computer.screenshot_data_url()
            cb.screenshot(png)
            content.append({"type": "input_image", "image_url": url, "detail": "original"})
        input_items = self._pending + [{"role": "user", "content": content}]
        self._pending = []
        cb.log("status", "思考中…")
        resp = self._create(input_items)
        if resp is None:
            cb.log("status", "已停止（放弃等待模型回复）")
            return ""

        for step in range(1, self.max_steps + 1):
            final = self._report_output(resp, cb)
            calls = [i for i in resp.output if i.type == "computer_call"]
            fcalls = [i for i in resp.output if i.type == "function_call"]
            if not calls and not fcalls:
                cb.log("status", "完成")
                self._after_success(final, cb)
                return final
            if self._stop.is_set():
                self._pending = self._stopped_outputs(resp)
                cb.log("status", "已停止")
                return final

            next_input = []
            failed = None
            step_sigs, last_png = [], None
            for fc in fcalls:
                if self._stop.is_set():
                    result = {"status": "skipped", "detail": "没有执行（用户停止了任务）"}
                else:
                    result = self._run_function(fc, cb)
                    self._gpt_effective += 1
                next_input.append({"type": "function_call_output", "call_id": fc.call_id,
                                   "output": json.dumps(result, ensure_ascii=False)})
            for call in calls:
                actions = list(getattr(call, "actions", None) or [])
                legacy = getattr(call, "action", None)
                if not actions and legacy is not None:
                    actions = [legacy]
                step_sigs += [self._action_sig(a) for a in actions]

                # OpenAI 的安全检查直接确认通过，不问用户
                ack = [{"id": check.id, "code": getattr(check, "code", None), "message": getattr(check, "message", None)}
                       for check in getattr(call, "pending_safety_checks", None) or []]

                done = []
                if not failed:
                    for a in actions:
                        if self._stop.is_set():
                            break
                        blocked = wechat_guard(a, self._wechat_state)
                        if blocked:
                            cb.log("error", blocked)
                            failed = f"动作「{computer.describe(a)}」没有执行：{blocked}。已执行: {', '.join(done) or '无'}。"
                            break
                        cb.log("action", computer.describe(a))
                        try:
                            computer.execute(a)
                            done.append(computer.describe(a))
                            kind = computer._get(a, "type")
                            if kind not in ("screenshot", "wait", "move"):
                                self._gpt_effective += 1
                        except Exception as e:  # noqa: BLE001
                            cb.log("error", f"动作执行失败: {e}")
                            failed = f"动作「{computer.describe(a)}」执行失败: {e}。已执行: {', '.join(done) or '无'}。"
                            break
                        time.sleep(0.3)

                time.sleep(0.8)  # 等界面刷新
                url, png = computer.screenshot_data_url()
                cb.screenshot(png)
                last_png = png
                out = self._screenshot_output(call.call_id, url)
                if ack:
                    out["acknowledged_safety_checks"] = ack
                next_input.append(out)

            if self._stop.is_set():
                self._pending = next_input
                cb.log("status", "已停止")
                return ""
            if failed:
                cb.log("status", failed)
                next_input.append({"role": "user", "content": failed + " 看一下当前截图，换个办法继续。"})
            else:
                nudge = self._loop_nudge(step_sigs, last_png)
                if nudge:
                    cb.log("status", nudge.strip("（）"))
                    next_input.append({"role": "user", "content": nudge})

            if self._wait_if_paused(next_input, cb):
                self._pending = next_input
                cb.log("status", "已停止")
                return ""
            cb.log("status", f"第 {step} 步完成，思考中…")
            resp = self._create(next_input)
            if resp is None:
                cb.log("status", "已停止（放弃等待模型回复）")
                return ""

        self._pending = self._stopped_outputs(resp)
        cb.log("status", f"已达到最大步数 {self.max_steps}，停止（可输入「继续」接着做）")
        return ""


def api_key_from_env() -> str:
    return os.environ.get("OPENAI_API_KEY", "")
