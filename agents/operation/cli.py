"""Ami 的终端入口：在终端里下指令，后台照常跑常驻工作流（消息巡检 + 盯菜单栏图标）。

  ./run.sh                 交互模式：输入指令回车执行；后台同时跑消息巡检
  ./run.sh "打开备忘录"     只执行这一条指令，做完就退出（不跑后台巡检）
  ./run.sh serve           不接收指令，只跑后台工作流（消息巡检、领中控任务）

后台工作流除了消息巡检（Lark、微信），还有「领中控任务」：去 AMI 中控（amients.com）领派给这个 agent 的任务，
和巡检排队轮流跑，谁也不打断谁，做完把结果报回去。

交互模式里的命令（其他输入都当成指令）：
  /stop                停止正在跑的指令（也可以按 Ctrl+C）
  /pause               当前这一步结束后暂停，你自己操作电脑
  /resume [你做了什么]  继续，把你说的操作告诉模型
  /new                 新对话（清空上下文）
  /status              看巡检状态和最近的动态
  /inbox               立刻跑一次消息巡检
  /patrol on|off       打开 / 暂停消息巡检
  /tasks on|off        打开 / 暂停领中控任务
  /check               立刻自检一次（看各平台要不要重新登录，结果报给中控；平时由中控派 checkup 任务）
  /wechat [on]         看微信自动操作是否暂停着（被锁定 / 风控时会自动暂停）；on 恢复
  /quit                退出（也可以在空闲时按 Ctrl+C / Ctrl+D）

权限：macOS 把「辅助功能」「屏幕录制」记在启动它的终端 App 上（Terminal / iTerm / Claude），给那个 App 打开后重开它。
配置读项目里的 .env（和 ~/.computer-use/.env）。日志追加写到 ~/.computer-use/app.log。
"""

import fcntl
import os
import sys
import threading
import time
import traceback

import paths

paths.load_env()

import amients  # noqa: E402
import workflows  # noqa: E402
from agent import DEFAULT_MODEL, Agent, AgentCallbacks, api_key_from_env  # noqa: E402

LOG_PATH = os.path.join(paths.state_dir(), "app.log")
LABEL = {"user": "你", "assistant": "Ami", "action": "动作", "status": "状态", "error": "出错", "stats": "统计",
         "report": "报告", "thinking": "思考"}
COLOR = {"user": "36", "assistant": "32", "action": "34", "error": "31", "stats": "90", "report": "90", "thinking": "90"}
EVENT_LABEL = {"trigger": "新消息", "run": "巡检", "run_done": "处理完", "goal_changed": "改目标", "error": "出错",
               "goal_error": "出错", "icon": "图标", "scheduler": "启动", "task": "领到任务", "task_done": "做完",
               "task_failed": "失败", "task_lost": "被收回", "check": "自检", "check_alert": "要重新登录",
               "registered": "注册能力", "drafted": "起草"}
PRIVACY_PANES = {
    "辅助功能": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "屏幕录制": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}
_print_lock = threading.Lock()


def say(kind: str, text: str) -> None:
    """打印一行日志，同时追加写到 app.log。等待中的进度刷新不打印。"""
    if kind == "progress":
        return
    stamp = time.strftime("%H:%M:%S")
    label = LABEL.get(kind, kind)
    color = COLOR.get(kind, "0") if sys.stdout.isatty() else ""
    head = f"\033[{color}m[{label}]\033[0m" if color else f"[{label}]"
    with _print_lock:
        print(f"{stamp} {head} {text}", flush=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{stamp} [{kind}] {text}\n")
        except OSError:
            pass


def missing_permissions() -> list[str]:
    import agentstate
    return [k for k, ok in agentstate.permissions().items() if not ok]


def check_permissions() -> bool:
    missing = missing_permissions()
    if not missing:
        return True
    say("error", "缺少 " + "、".join(f"「{m}」" for m in missing) + " 权限。到「系统设置 → 隐私与安全性」里给运行这个终端的 App"
                 "（Terminal / iTerm / Claude）打开，然后完全退出并重新打开那个 App。设置页：\n  "
                 + "\n  ".join(f"{m}：open '{PRIVACY_PANES[m]}'" for m in missing))
    return False


_lock_file = None


def single_instance() -> bool:
    """同一时间只允许一个在跑后台巡检（两个都巡检会抢屏幕）。拿到锁返回 True。"""
    global _lock_file
    _lock_file = open(os.path.join(paths.state_dir(), "app.lock"), "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def make_agent(max_steps: int = 80) -> Agent:
    return Agent(api_key=api_key_from_env(), model=os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL,
                 base_url=os.environ.get("OPENAI_BASE_URL") or None, max_steps=max_steps,
                 use_jev=bool(os.environ.get("TYPESAFE_API_KEY", "").strip()))


def make_scheduler() -> workflows.Scheduler:
    """后台工作流（消息巡检、领中控任务）。没有辅助功能 / 屏幕录制权限时不去中控领任务，留给别的 agent。"""
    return workflows.Scheduler(lambda: make_agent(max_steps=25), can_work=lambda: not missing_permissions())


class Console:
    """交互模式：主线程读输入，指令在后台线程里跑；后台巡检由 workflows.Scheduler 调度。"""

    def __init__(self, with_workflows: bool):
        self.agent = make_agent()
        self.thread: threading.Thread | None = None
        self.scheduler = make_scheduler() if with_workflows else None
        self._last_event = time.time()

    # ---------------- 指令 ----------------
    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, text: str) -> None:
        if self.busy:
            say("status", "上一条指令还在跑，先 /stop 或等它做完")
            return
        if not check_permissions():
            return
        self.thread = threading.Thread(target=self._run, args=(text,), name="task", daemon=True)
        self.thread.start()

    def _run(self, text: str) -> None:
        cb = AgentCallbacks(log=say)
        sched = self.scheduler
        try:
            if sched:
                # 让正在跑的巡检 / 中控任务停下、让出屏幕（被打断的巡检下次再处理，任务让中控重新派发）
                sched.preempt()
                deadline = time.time() + 30
                while sched.running and time.time() < deadline:
                    time.sleep(0.2)
            # 你说的话先当成对常驻目标的修改；里面有要马上做的事，再执行
            r = workflows.update_goals(text, self.agent.client, self.agent.model)
            if r["changed"]:
                say("status", "常驻目标已更新（/status 看详情）")
            task = r["task"] if r["changed"] else (r["task"] or text)
            if task:
                self.agent.run(task, cb)
        except Exception as e:  # noqa: BLE001
            say("error", f"{type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            if sched:
                sched.release()

    def stop(self) -> None:
        if self.busy:
            self.agent.stop()
            say("status", "正在停止…")
        else:
            say("status", "现在没有在跑的指令")

    # ---------------- 后台巡检 ----------------
    def watch_events(self) -> None:
        """把后台巡检的动态（新消息、处理完、出错）打印出来。没有新情况的巡检不打印。"""
        while True:
            time.sleep(2)
            try:
                events = workflows.recent_events(50)
            except Exception:  # noqa: BLE001
                continue
            for e in events:
                if e["ts"] <= self._last_event:
                    continue
                self._last_event = e["ts"]
                if e["kind"] == "run" and not (e.get("data") or {}).get("items"):
                    continue
                text = e["text"]
                actions = (e.get("data") or {}).get("actions") if e["kind"] == "run_done" else None
                if actions:
                    text += "\n    " + "\n    ".join(actions)
                wid = (e.get("data") or {}).get("workflow")
                head = "中控任务" if wid == workflows.TASKS else "自检" if wid == workflows.CHECKUP else "巡检"
                say("error" if "error" in e["kind"] or e["kind"] in ("task_failed", "check_alert") else "status",
                    f"〔{head}·{EVENT_LABEL.get(e['kind'], e['kind'])}〕{text}")

    def run_now(self, wid: str = "inbox") -> None:
        """立刻跑一次消息巡检（/inbox）或自检（/check）。"""
        sched = self.scheduler
        if sched is None:
            say("status", "没有启动后台工作流")
            return
        if sched.running or self.busy:
            say("status", "现在正忙（巡检、中控任务或你的指令在跑），稍后再试")
            return
        name = "自检" if wid == workflows.CHECKUP else "巡检"

        def go():
            try:
                sched.run_checkup() if wid == workflows.CHECKUP else sched.run_once(wid)
            except Exception as e:  # noqa: BLE001
                workflows.log_event("error", f"立即{name}出错：{type(e).__name__}: {e}", {"workflow": wid})
        threading.Thread(target=go, name=f"{wid}-now", daemon=True).start()
        say("status", f"开始{name}…")

    def set_enabled(self, wid: str, on: bool) -> None:
        goals = workflows.load_goals()
        wf = goals["workflows"][wid]
        wf["enabled"] = on
        workflows.save_goals(goals)
        workflows.log_event("goal_changed", f"「{wf['name']}」" + ("已恢复" if on else "已暂停"))

    def status(self) -> None:
        snap = workflows.status_snapshot(self.scheduler)
        st = amients.stats()
        for wf in snap["workflows"]:
            last = time.strftime("%H:%M", time.localtime(wf["last_run"])) if wf["last_run"] else "还没跑过"
            if wf["id"] == workflows.TASKS:
                state = (f"正在做 {st['task'] or ''}" if wf["running"] else f"{st['task']} 排队中" if st["task"]
                         else "已暂停" if not wf["enabled"] else "在等中控派任务" if st["tasks"]
                         else "没启动（没填 AMIENTS_API_KEY、AMIENTS_TASKS=off，或者没跑后台工作流）")
                state += f"；最近领任务失败：{st['task_error']}" if st["task_error"] else ""
            else:
                state = "正在处理" if wf["running"] else ("已暂停" if not wf["enabled"] else f"每 {wf['interval_min']}～{wf['interval_max']} 分钟随机一次（空闲时）")
            print(f"{wf['name']}：{state} · 上次 {last}\n  目标：{wf.get('goal', '')}")
            for r in wf.get("rules") or []:
                print(f"  · {r}")
        import agentstate
        acc = agentstate.load().get("accounts") or {}
        if acc:
            print("账号：" + "；".join(f"{n} {'正常' if a.get('ok') else '有问题（' + (a.get('detail') or '')[:40] + '）'}"
                                    for n, a in acc.items()))
        import wechat
        p = wechat.paused()
        if p:
            print(wechat.paused_detail(p))
        shown = [e for e in snap["events"] if not (e["kind"] == "run" and not (e.get("data") or {}).get("items"))][-10:]
        if shown:
            print("最近的动态：")
            for e in shown:
                print(f"  {time.strftime('%H:%M', time.localtime(e['ts']))} [{EVENT_LABEL.get(e['kind'], e['kind'])}] "
                      f"{e['text'].splitlines()[0][:100]}")
        print(f"上报：{st['endpoint'] or '没填 AMIENTS_API_KEY，只存本地'}；待发 {st['pending']} 条"
              + (f"；上次成功 {st['last_ok']}" if st["last_ok"] else "")
              + (f"；最近失败：{st['last_error']}" if st["last_error"] else ""))

    # ---------------- 主循环 ----------------
    def handle(self, line: str) -> bool:
        """处理一行输入；返回 False 表示退出。"""
        cmd, _, arg = line.partition(" ")
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/stop":
            self.stop()
        elif cmd == "/pause":
            if self.busy:
                self.agent.pause()
                say("status", "当前这一步结束后暂停。你自己操作电脑，完成后输入 /resume 加上你做了什么")
            else:
                say("status", "现在没有在跑的指令")
        elif cmd == "/resume":
            self.agent.resume(arg.strip())
            say("status", "继续")
        elif cmd == "/new":
            if self.busy:
                say("status", "指令还在跑，先 /stop")
            else:
                self.agent.reset()
                say("status", "已开始新对话")
        elif cmd == "/status":
            self.status()
        elif cmd == "/inbox":
            self.run_now("inbox")
        elif cmd == "/check":
            self.run_now(workflows.CHECKUP)
        elif cmd in ("/patrol", "/tasks"):
            wid, name = ("inbox", "消息巡检") if cmd == "/patrol" else (workflows.TASKS, "领中控任务")
            if arg.strip() in ("on", "off"):
                self.set_enabled(wid, arg.strip() == "on")
                say("status", name + "已" + ("打开" if arg.strip() == "on" else "暂停"))
            else:
                say("status", f"用法：{cmd} on 或 {cmd} off")
        elif cmd == "/wechat":
            import wechat
            p = wechat.paused()
            if arg.strip() == "on":
                say("status", "微信自动操作已恢复" if wechat.resume() else "微信自动操作本来就没暂停")
            elif p:
                say("status", wechat.paused_detail(p))
            else:
                say("status", "微信自动操作正常（没有暂停）")
        elif cmd.startswith("/"):
            say("status", "不认识的命令。可用：/stop /pause /resume /new /status /inbox /check /patrol on|off /tasks on|off /wechat [on] /quit")
        else:
            self.start(line)
        return True

    def loop(self) -> None:
        try:
            import readline  # noqa: F401  上下键翻历史
        except ImportError:
            pass
        if self.scheduler:
            self.scheduler.start()
            threading.Thread(target=self.watch_events, name="events", daemon=True).start()
            say("status", "后台消息巡检已启动：盯右上角微信、Lark 图标，有新消息马上处理；空闲时每 30～60 分钟随机整体看一遍，夜里不跑")
        say("status", "输入指令回车执行；/stop 停止，/status 看巡检，/quit 退出（/pause /resume /new /inbox /check /patrol on|off /tasks on|off /wechat [on]）")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                if self.busy:
                    self.stop()
                    continue
                break
            if line and not self.handle(line):
                break
        if self.busy:
            self.agent.stop()
            self.thread.join(timeout=10)
        if self.scheduler:
            self.scheduler.stop()


def main() -> None:
    args = sys.argv[1:]
    if not api_key_from_env():
        say("error", "没有 OPENAI_API_KEY：在项目的 .env 里填上（见 .env.example）")
        sys.exit(1)
    with_workflows = os.environ.get("WORKFLOWS", "on").strip().lower() not in ("off", "0", "false", "no")
    if args and args[0] not in ("serve",):
        # 单条指令：做完就退出，不跑后台巡检
        if not check_permissions():
            sys.exit(1)
        amients.start_sender()
        agent = make_agent()
        try:
            agent.run(" ".join(args), AgentCallbacks(log=say))
        except KeyboardInterrupt:
            agent.stop()
        finally:
            amients.stop_sender()
        return
    if with_workflows and not single_instance():
        say("error", "已经有一个 Ami 在跑后台巡检了（可能是另一个终端，或还开着的桌面版 Ami / 小暖.app），先退出那个")
        sys.exit(1)
    check_permissions()
    amients.start_sender()
    try:
        if args[:1] == ["serve"]:
            sched = make_scheduler()
            sched.start()
            console = Console(with_workflows=False)
            console.scheduler = sched
            threading.Thread(target=console.watch_events, name="events", daemon=True).start()
            say("status", "只跑后台工作流（消息巡检、领中控任务），Ctrl+C 退出")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                sched.stop()
        else:
            Console(with_workflows).loop()
    finally:
        amients.stop_sender()


if __name__ == "__main__":
    main()
