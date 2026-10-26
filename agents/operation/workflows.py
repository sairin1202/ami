"""常驻工作流：agent 不再只是「一条指令跑一次」，而是几个按自己的节奏循环的工作流（「消息巡检」和「领中控任务」）。

- 目标（goals.json）：每个工作流一段目标和若干条规则（例如「陈宏找我就回收到」「好友申请附言像广告的不通过」）。
  用户说话时先交给 update_goals：模型判断是在改常驻目标，还是一次性任务，改动直接写进 goals.json，不打扰用户；
  一次性任务照旧马上执行。
- 调度（Scheduler）：后台一个线程，一次只跑一个工作流（只有一块屏幕和一套键鼠）。用户正在用电脑（最近有键鼠操作）
  或者前台在跑用户的指令时自动延后。
- 消息巡检：先用代码便宜地看一眼（Lark 会话列表、微信「新的朋友」），和上次看过的比，没有新情况就不调模型；
  有新情况才跑一次 agent loop，输入是目标 + 规则 + 这次的新情况。
  两种触发：每隔几秒盯一下右上角菜单栏的聊天软件图标（menubar.py，只截几十个像素），图标变了让模型判断是不是
  新消息，是就跑一次；另外用户空闲时每隔 interval_min～interval_max（默认 30～60）分钟里随机的一段时间整体看一遍。
  不用固定间隔：按固定节奏反复去点微信界面太像机器，容易被微信风控锁定。
- 领中控任务：amients 的领任务线程在调度空闲时去 AMI 中控（amients.com）领派给这个 agent 的任务，领到就排进调度，
  和消息巡检轮流跑，谁也不打断谁。任务有时限，所以不等用户空闲、夜里也做；前台下了指令照样让路（让中控重新派发）。
  启动时把能接的任务类型（capabilities）注册给中控，只领这些类型；同时声明自检项（checkup.check_names），中控每天派
  ami.check 任务：不交给 agent，直接用代码（checkup.py）看 rednote / B 站 / Telegram / X 命令行工具和浏览器里常用网站的
  登录状态，不调模型；按中控的格式回报每一项，要重新登录的另外记一条动态（warn）。
- 回消息、同意好友申请等都直接做，不需要任何人审批。
- 状态：每次运行做了什么、目标改动都写进 status.jsonl，客户端的「状态」页读这里。
"""

import copy
import json
import os
import random
import re
import threading
import time

import checkup
import paths

DEFAULT_GOALS = {
    "workflows": {
        "inbox": {
            "name": "消息巡检",
            "enabled": True,
            "interval_min": 30,
            "interval_max": 60,
            "goal": "替用户照看 Lark 和微信：有人找他时，能简单回复的用他的口吻回复（闲聊配梗图，工作消息正式一点），"
                    "微信里新的好友申请，看起来是正常的人就通过。",
            "rules": [
                "好友申请附言像广告、推销、诈骗的不通过",
                "机器人、系统通知不用回复",
            ],
        },
        "tasks": {
            "name": "领中控任务",
            "enabled": True,
            "goal": "做 AMI 中控（amients.com）派给这个 agent 的任务，做完用一两句话说结果，会原样回报给中控。",
            "rules": [
                "要用户点头才能做的事（付款、删东西、替他答应事情）先别做，在结果里说清楚要他确认什么",
            ],
        },
    },
}

STATE_LOCK = threading.RLock()


def _path(name: str) -> str:
    return os.path.join(paths.state_dir(), name)


# ---------------------------------------------------------------------------
# 目标
def load_goals() -> dict:
    with STATE_LOCK:
        try:
            with open(_path("goals.json"), encoding="utf-8") as f:
                goals = json.load(f)
        except (OSError, ValueError):
            goals = copy.deepcopy(DEFAULT_GOALS)
        for k, v in DEFAULT_GOALS["workflows"].items():  # 新加的默认工作流补进去
            goals.setdefault("workflows", {}).setdefault(k, copy.deepcopy(v))
        for k, wf in goals["workflows"].items():  # 旧配置只有固定间隔 interval_min（以前默认 5 分钟）：换成默认的随机区间
            if "interval_max" not in wf and "interval_max" in DEFAULT_GOALS["workflows"].get(k, {}):
                for f in ("interval_min", "interval_max"):
                    wf[f] = DEFAULT_GOALS["workflows"][k][f]
        return goals


def _minutes(v, default: int) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


def next_run_after(t: float, wf: dict) -> float:
    """下一次定时巡检的时间：t 之后 interval_min～interval_max 分钟里随机挑一个。"""
    lo = _minutes(wf.get("interval_min"), 30)
    hi = max(lo, _minutes(wf.get("interval_max"), 60))
    return t + random.uniform(lo, hi) * 60


def save_goals(goals: dict) -> None:
    with STATE_LOCK:
        goals["updated_at"] = time.time()
        tmp = _path("goals.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(goals, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path("goals.json"))


GOAL_PROMPT = """你是一个常驻电脑助理的「目标管理」。助理有几个按自己节奏循环的工作流，每个工作流有一段目标和若干条规则。
用户对助理说了一句话。判断它是：
- 修改常驻目标：以后都要这样（「以后陈宏找我就回收到」「好友申请都别通过了」「巡检改成 10 分钟一次」「先别巡检了」）
- 一次性任务：现在做一件事（「给 hong 发个消息」「看看谁找我」「查一下 HN」）
- 或者两者都有。

只输出一个 JSON 对象，不要别的文字：
{"workflows": {工作流 id: 修改后的完整工作流对象，只列出有改动的；字段同输入：name, enabled, interval_min, interval_max, goal, rules},
 "task": "需要马上执行的一次性任务，原样转述用户的要求；没有就是空字符串"}
规则要写成简短、可执行的中文句子；不要删除用户没提到的规则。
定时巡检每次在 interval_min 到 interval_max 分钟之间随机挑时间（单位分钟，最小 1）；用户只说一个数 N 时，设成 N 到 2N。"""


def update_goals(text: str, client, model: str) -> dict:
    """用户的一句话 → 改目标（直接生效）+ 一次性任务。返回 {"changed": [工作流 id], "task": str}。
    模型出错时整句话当成一次性任务，不改目标。"""
    goals = load_goals()
    try:
        resp = client.responses.create(
            model=model, instructions=GOAL_PROMPT, store=False,
            input=json.dumps({"current_workflows": goals["workflows"], "user_said": text}, ensure_ascii=False))
        raw = "".join(getattr(c, "text", "") or "" for item in resp.output if item.type == "message"
                      for c in item.content or [])
        m = re.search(r"\{.*\}", raw, re.S)
        out = json.loads(m.group(0) if m else raw)
    except Exception as e:  # noqa: BLE001
        log_event("goal_error", f"理解指令出错，按一次性任务处理：{type(e).__name__}: {e}")
        return {"changed": [], "task": text}
    changed = []
    for wid, wf in (out.get("workflows") or {}).items():
        if not isinstance(wf, dict):
            continue
        cur = goals["workflows"].get(wid, {})
        new = {**cur, **{k: wf[k] for k in ("name", "enabled", "interval_min", "interval_max", "goal", "rules")
                         if k in wf}}
        new["interval_min"] = _minutes(new.get("interval_min"), 30)
        new["interval_max"] = max(new["interval_min"], _minutes(new.get("interval_max"), 60))
        new["rules"] = [str(r) for r in new.get("rules") or []]
        if new != cur:
            goals["workflows"][wid] = new
            changed.append(wid)
            log_event("goal_changed", f"「{new.get('name', wid)}」的目标改了", {"workflow": wid, "before": cur, "after": new,
                                                                                 "user_said": text})
    if changed:
        save_goals(goals)
    return {"changed": changed, "task": str(out.get("task") or "").strip()}


# ---------------------------------------------------------------------------
# 状态事件（客户端「状态」页读这里）
_listeners: list = []


def log_event(kind: str, text: str, data: dict | None = None) -> dict:
    ev = {"ts": time.time(), "kind": kind, "text": text, **({"data": data} if data else {})}
    try:
        import amients
        amients.emit(f"workflow.{kind}", text, origin=(data or {}).get("workflow") or "workflow", data=data)
    except Exception:  # noqa: BLE001  上报出问题不影响工作流
        pass
    with STATE_LOCK:
        with open(_path("status.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    for fn in list(_listeners):
        try:
            fn(ev)
        except Exception:  # noqa: BLE001
            pass
    return ev


def recent_events(limit: int = 200) -> list[dict]:
    try:
        with open(_path("status.jsonl"), encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def load_state() -> dict:
    with STATE_LOCK:
        try:
            with open(_path("workflow_state.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


def save_state(state: dict) -> None:
    with STATE_LOCK:
        tmp = _path("workflow_state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _path("workflow_state.json"))


# ---------------------------------------------------------------------------
# 消息巡检：先便宜地看一眼有没有新情况
def inbox_snapshot(on_step=None, judge_model=None) -> list[dict]:
    """Lark 会话列表里「最后一条不是自己发的」会话，和微信「新的朋友」里等待验证的申请。每项带一个指纹。
    微信只把窗口切到前台截一张图，让模型判断通讯录图标上有没有红点，有才打开「新的朋友」；平时不点。
    judge_model：(client, model)，没有就不看微信好友申请。
    on_step：每看完一个应用调一次（用来记下切到前台过的应用，看完把它们最小化）。"""
    items = []
    try:
        import lark
        if lark.available():
            for c in lark.chats()["chats"]:
                if c.get("last_from_me"):
                    continue
                items.append({"app": "Lark", "kind": "message", "chat": c["name"], "preview": c.get("preview", ""),
                              "time": c.get("time", ""), "unread": c.get("unread", 0), "tags": c.get("tags", []),
                              "key": f"lark|{c['name']}|{c.get('preview', '')}"})
    except Exception as e:  # noqa: BLE001
        log_event("error", f"看 Lark 会话列表出错：{type(e).__name__}: {e}")
    if on_step:
        on_step()
    try:
        import wechat
        import wechat_friend
        verdict = None
        if wechat_friend.available() and not wechat.paused() and judge_model:
            verdict = wechat_friend.contacts_badge(*judge_model)
            log_event("icon", f"微信通讯录红点：{'有' if verdict['badge'] else '没有'}（{verdict['reason']}）", verdict)
        if verdict and verdict["badge"]:
            r = wechat_friend.list_requests()
            for q in r.get("requests", []):
                if q["status"] == "等待验证":
                    items.append({"app": "微信", "kind": "friend_request", "name": q["name"], "message": q["message"],
                                  "key": f"wechat-friend|{q['name']}|{q['message']}"})
    except Exception as e:  # noqa: BLE001
        log_event("error", f"看微信好友申请出错：{type(e).__name__}: {e}")
    if on_step:
        on_step()
    return items


def new_items(items: list[dict], seen: list[str]) -> list[dict]:
    seen_set = set(seen)
    return [i for i in items if i["key"] not in seen_set]


def inbox_task(wf: dict, items: list[dict]) -> str:
    lines = []
    for i in items:
        if i["kind"] == "message":
            lines.append(f"- Lark「{i['chat']}」{('（' + '、'.join(i['tags']) + '）') if i['tags'] else ''}："
                         f"最新一条（{i['time']}）预览「{i['preview']}」" + (f"，未读 {i['unread']}" if i["unread"] else ""))
        elif i["kind"] == "wechat_unread":
            lines.append("- 微信菜单栏图标显示有新消息" + (f"（未读 {i['unread']}）" if i.get("unread") else "")
                         + "：先用 wechat_open 把微信切到前台（不要用 Spotlight 或程序坞），再截图看左侧会话列表里"
                           "哪些聊天有未读红点，再用 wechat_read 读，"
                           "按目标和规则决定要不要用 wechat_send 回")
        elif i["kind"] == "lark_unread":
            lines.append("- Lark 菜单栏图标显示有新消息" + (f"（未读 {i['unread']}）" if i.get("unread") else "")
                         + "：先用 lark_chats 看会话列表里哪些会话有未读，再用 lark_read 读完整上下文，"
                           "按目标和规则决定要不要用 lark_send 回")
        else:
            lines.append(f"- 微信好友申请「{i['name']}」，附言「{i['message']}」")
    rules = "\n".join(f"  · {r}" for r in wf.get("rules") or [])
    import wechat
    p = wechat.paused()
    if p:
        lines.append(f"- {wechat.paused_detail(p)}")
    return (f"你正在执行常驻工作流「{wf['name']}」（后台自动运行，用户不在旁边，不要问用户问题）。\n"
            f"目标：{wf['goal']}\n规则：\n{rules}\n\n这次巡检看到的新情况：\n" + "\n".join(lines) +
            "\n\n逐个处理：Lark 的先用 lark_read 读完整上下文（预览可能被截断），按目标和规则决定回不回、回什么，"
            "要回就用 lark_send（闲聊 meme 填 auto）；微信好友申请按规则决定是否用 wechat_accept_friend 通过。"
            "不需要处理的就跳过。只处理上面列出的这些新情况，没列出来的不要主动去看（尤其不要为了「顺便看看」打开微信、"
            "看好友申请：多余的微信操作容易被风控）。最后用一两句话总结这次做了什么。")


def capabilities() -> list[dict]:
    """注册给中控的能力（Ami 能接哪些类型的任务）：都交给 agent 按说明做，描述里写上这台电脑上现在能用的应用。"""
    import tools
    have = {t["name"] for t in tools.specs()}
    apps = [n for n, t in (("Lark", "lark_send"), ("微信", "wechat_send"), ("Telegram", "tg_write"), ("Slack", "slack_send"),
                           ("Discord", "discord_send"), ("Telegram 机器人", "telegram_send"), ("飞书群机器人", "feishu_send"))
            if t in have]
    import xhs
    feeds = list(dict.fromkeys(n for n, t in (
        ("rednote", "rednote_feed"), (xhs.NAME, "xhs"), ("X", "x_feed"), ("B 站", "bili"), ("LinkedIn", "linkedin_search"),
        ("Hacker News", "hn_stories"), ("V2EX", "v2ex_topics"), ("GitHub", "github_search"), ("AI 论文", "papers_daily"),
        ("RSS", "rss_read"), ("网页搜索", "exa_search")) if t in have))
    done = {"text": "Ami 最后那句话：做了什么、结果是什么"}
    return [
        {"kind": "computer.do", "title": "在这台 Mac 上做一件事",
         "description": "通用：像对助理说话一样写要做的事，Ami 看着屏幕操作应用、网页去完成（最多 80 步）。"
                        "付款、删东西、替用户答应事情这类要用户点头的不会做，会在结果里写清楚要确认什么。",
         "input": {"text": "要做的事"}, "output": done, "example": {"text": "打开备忘录，新建一条「明天十点开会」"}},
        {"kind": "message.send", "title": "替用户发消息",
         "description": f"用用户本人的账号发消息，现在能用：{'、'.join(apps) or '无'}。收件人名字要和应用里显示的一致，"
                        "给了原话就一字不改照发。",
         "input": {"app": f"用哪个：{' / '.join(apps)}", "to": "收件人", "text": "要发的话"}, "output": done,
         "example": {"app": apps[0] if apps else "Lark", "to": "Hong Chen", "text": "明天十点开会"}},
        {"kind": "message.inbox", "title": "看看谁找用户",
         "description": f"看 {'、'.join(a for a in apps if a in ('Lark', '微信')) or 'Lark / 微信'} 最近谁找用户、说了什么，"
                        "汇报要点和建议怎么回（只看不回）。",
         "input": {"text": "可选：只看谁、要特别注意什么"}, "output": {"text": "谁找、说了什么、建议怎么回"},
         "example": {"text": "看看今天谁找我"}},
        {"kind": DRAFT_KIND, "title": "起草帖子",
         "description": "产品运营用：按各平台的写法起草要发的帖子（V2EX、少数派、掘金、即刻、小红书 / rednote、X、知乎、Reddit、"
                        "LinkedIn、B 站、微博、linux.do 等），同一件事每个平台写法不一样。只起草，不操作电脑、不发出去；"
                        "看过要发的话在终端里让 Ami 发（发之前还会给用户过目）。",
         "input": {"platforms": "发到哪些平台（列表）；不写就挑最合适的两三个", "text": "要说什么：要点、立场、想达到的效果",
                   "material": "可选：背景资料（产品介绍、更新日志、链接）"},
         "output": {"text": "一句话总结", "drafts": "[{platform, where, title, body, tags, notes}]"},
         "example": {"platforms": ["V2EX", "即刻", "掘金"], "text": "Ami 新版本：能从 AMI 中控领任务，每天自检各平台的登录状态",
                     "material": "Ami 是住在 Mac 里的电脑助理，替你发消息、回消息、上网找信息、把信息提交出去"}},
        {"kind": "agent.state", "title": "查看 Ami 状态",
         "description": "交回 Ami 自己维护的状态（agent_state.json）：各平台账号能不能用、正在做什么、工作流、领任务、能用的工具、"
                        "权限。不占屏幕，Ami 忙的时候也马上回。fresh 为 true 时先把命令行工具的登录状态重新查一遍（十几秒，不碰浏览器）。",
         "input": {"fresh": "可选，true：先重新查命令行工具的登录"},
         "output": {"text": "一句话：在做什么、哪些账号有问题", "state": "整份状态 JSON"},
         "example": {"fresh": False}},
        {"kind": "web.research", "title": "上网查资料",
         "description": f"在网上和这些来源里查资料：{'、'.join(feeds + ['浏览器里的网站'])}；先给结论，再给要点，每条附链接。",
         "input": {"text": "要查什么"}, "output": {"text": "结论 + 要点 + 链接"},
         "example": {"text": "这周 Hacker News 上关于 AI agent 的热门讨论"}},
    ]


DRAFT_PROMPT = """你是用户的产品运营助手，替用户起草要发到各个平台的帖子。只起草，不发出去。
按下面「发帖」技能里第 1、2 步和「各平台怎么发」的长度、格式来写：
- 同一件事发到几个平台，每个平台写法要不一样（看平台的受众和习惯），不要一字不差地复制
- 只写任务里给的事实、观点和经历，不编数据、不编亲身经历、不夸大；推广自己做的东西要写清楚是自己做的，守各平台对自我推广的规矩
- 用用户的口吻，像他本人在说话，不要 AI 腔、营销号腔
- 没指定平台就挑最合适的两三个；信息不够写不好的地方，在 notes 里说清楚缺什么
只输出一个 JSON，不要别的文字：
{"drafts": [{"platform": "平台", "where": "发到哪（版块 / 节点 / 分类，可空）", "title": "标题（没有就空）",
  "body": "正文", "tags": ["话题 / 标签"], "notes": "发之前要注意或要用户补的（可空）"}]}"""


def _platform_rules(platforms: list[str]) -> str:
    """要发的平台在 sites.py 里的操作说明（帮模型按平台的规矩写）。"""
    import sites
    want = [p.lower() for p in platforms]
    return "".join(s.rules for s in sites.SITES if s.rules and any(
        w in s.key or any(w in h for h in s.hosts) or (s.jev_skip and re.search(s.jev_skip, w, re.I)) for w in want))


def draft_posts(task: dict, client, model: str) -> dict:
    """起草帖子：一次模型调用，不碰电脑、不调工具，所以不可能发出去。返回 {text, drafts}。"""
    import amients
    import skills
    p = task.get("payload") if isinstance(task.get("payload"), dict) else {"text": amients.task_text(task)}
    platforms = p.get("platforms") or p.get("platform") or []
    if isinstance(platforms, str):
        platforms = [x for x in re.split(r"[,，、/\s]+", platforms) if x]
    skill = (skills.find("发帖") or {}).get("body", "")
    resp = client.responses.create(
        model=model, store=False,
        instructions=DRAFT_PROMPT + "\n\n# 「发帖」技能\n" + skill + _platform_rules(platforms),
        input=json.dumps({"platforms": platforms, "what": p.get("text") or amients.task_text(task),
                          "material": p.get("material") or ""}, ensure_ascii=False))
    raw = "".join(getattr(c, "text", "") or "" for item in resp.output if item.type == "message" for c in item.content or [])
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        drafts = [d for d in json.loads(m.group(0) if m else raw).get("drafts") or []
                  if isinstance(d, dict) and d.get("platform") and d.get("body")]
    except ValueError:
        drafts = []
    if not drafts:  # 模型没按格式给：原样交回去，至少能看
        return {"text": "草稿没按格式整理出来，原文在 raw 里（只是草稿，没发出去）", "drafts": [], "raw": raw.strip()[:8000]}
    return {"text": f"起草了 {len(drafts)} 条：{'、'.join(d['platform'] for d in drafts)}（只是草稿，没发出去）", "drafts": drafts}


def task_kinds() -> list[str] | None:
    """去中控只领这些类型：注册过的能力和自检。AMIENTS_TASK_KINDS=all 什么类型都领。"""
    if os.environ.get("AMIENTS_TASK_KINDS", "").strip().lower() == "all":
        return None
    return [c["kind"] for c in capabilities() if c["kind"] not in STATE_KINDS] + sorted(CHECKUP_KINDS)


def task_prompt(wf: dict, task: dict) -> str:
    """中控派来的任务 → 给 agent 的指令：「领中控任务」的目标、规则，任务类型的说明，加上任务本身。"""
    import amients
    who = task.get("from_name") or task.get("from_agent")
    body = amients.task_text(task) or f"（没有写内容，按类型「{task.get('kind') or 'task'}」理解要做什么）"
    rules = "\n".join(f"  · {r}" for r in wf.get("rules") or [])
    cap = next((c for c in capabilities() if c["kind"] == task.get("kind")), None)
    return (f"你正在执行常驻工作流「{wf['name']}」（后台自动运行，用户可能不在旁边，不要问用户问题）。\n"
            f"目标：{wf['goal']}\n规则：\n{rules}\n\n"
            + (f"任务类型「{cap['title']}」：{cap['description']}\n" if cap else "")
            + f"中控派来的任务 {amients.task_label(task)}" + (f"，来自「{who}」" if who else "") + f"：\n{body}")


# ---------------------------------------------------------------------------
# 调度
def user_idle_seconds() -> float:
    try:
        import Quartz
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)
    except Exception:  # noqa: BLE001
        return 1e9


# 夜里（默认 23 点到早上 8 点）不跑巡检，图标报的新消息也攒到早上再处理：半夜还在规律地操作微信太像机器。
# 设成空串或 off 关掉
QUIET_HOURS = os.environ.get("WORKFLOW_QUIET_HOURS", "23-8")


def quiet_now(now: float | None = None) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", QUIET_HOURS or "")
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    h = time.localtime(now or time.time()).tm_hour
    return (start <= h < end) if start < end else (h >= start or h < end)


IDLE_NEEDED = float(os.environ.get("WORKFLOW_IDLE_SECONDS", "60"))   # 用户这么久没动键鼠才在后台抢屏幕
ICON_TICK = float(os.environ.get("WORKFLOW_ICON_SECONDS", "3"))     # 多久看一次菜单栏图标
TASKS = "tasks"                                                     # 领中控任务的工作流 id
TASK_MAX_STEPS = 80                                                 # 中控任务和你下的指令一样最多 80 步（巡检 25 步）
CHECKUP = "checkup"                                                 # 自检：动态里的工作流标记
CHECKUP_KINDS = {"ami.check", "checkup", "login-check", "自检", "每日自检"}  # 这些类型的中控任务直接跑自检，不交给 agent
DRAFT_KIND = "post.draft"                                           # 起草帖子：只调一次模型，不碰电脑、不发出去
STATE_KINDS = {"agent.state", "ami.state"}                          # 中控来要 Ami 的状态：专门的领取线程马上回，不排队
STATE_REFRESH = 60.0                                                # 状态文件（agentstate）多久现算刷新一次


class Scheduler:
    """后台线程：到点且用户没在用电脑时，跑一次到期的工作流。前台在跑用户指令时（busy）不跑。"""

    def __init__(self, make_agent, tick: float = ICON_TICK, can_work=lambda: True):
        self.make_agent = make_agent       # () -> Agent，每次运行一个新的（不带历史）
        self.tick = tick                   # 多久看一次菜单栏图标、检查一次有没有到点
        self.can_work = can_work           # () -> bool：能不能操作电脑（有没有辅助功能、屏幕录制权限），不能就不领任务
        self.watcher = None                # menubar.Watcher，第一次用时再建
        self.triggered: list[dict] = []    # 菜单栏图标报的新消息，等着下一次巡检处理
        self.tasks: list[dict] = []        # 中控派来、排队等着跑的任务 {"task", "lease", "done", "result" / "error"}
        self._icon_model = None            # 判断图标变化用的 (client, model)
        self.busy = threading.Event()      # 前台在跑用户的指令
        self.running: dict = {}            # 正在跑的工作流 {"id", "agent", "started"}
        self._working = False              # 调度线程正在跑某个工作流（含巡检前用代码看界面那段）
        self.state_worker = None           # 专门领 agent.state 的线程（不占屏幕，忙的时候也马上回）
        self._state_at = 0.0               # 上次刷新状态文件的时间
        self._stop = threading.Event()
        self._thread = None
        self._dialog_logged = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="workflows", daemon=True)
            self._thread.start()
            import amients
            worker = amients.start_worker(self.submit_task, self._can_take_task,
                                          lambda k, t, d: log_event(k, t, {"workflow": TASKS, **d}),
                                          setup=self.register, kinds=task_kinds())
            if worker:
                self.state_worker = amients.TaskWorker(
                    self.answer_state, lambda: True, self._state_event, kinds=sorted(STATE_KINDS))
                self.state_worker.start()
            log_event("scheduler", "工作流调度已启动" + ("，会去 AMI 中控领任务" if worker else ""))
            amients.set_status("idle", "Ami 刚启动，空闲中")  # 状态文件里一开始就有运行状态
            self._refresh_state()

    def stop(self):
        self._stop.set()
        import amients
        amients.stop_worker()
        if self.state_worker:
            self.state_worker.stop()
        a = self.running.get("agent")
        if a:
            a.stop()

    def preempt(self):
        """用户在前台下了指令：让正在跑的工作流停下（它下一轮会从头再看）。"""
        self.busy.set()
        a = self.running.get("agent")
        if a:
            a.stop()

    def release(self):
        self.busy.clear()

    def due(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        if quiet_now(now):
            return []
        goals, state = load_goals(), load_state()
        out = []
        for wid, wf in goals["workflows"].items():
            if not wf.get("enabled") or wid == TASKS:  # 领任务不按时间跑：领到了才排进 self.tasks
                continue
            nxt = (state.get(wid) or {}).get("next_run")
            if nxt is None:  # 第一次：也从现在起随机等一段，不一启动就去点界面
                nxt = next_run_after(time.time(), wf)
                with STATE_LOCK:
                    st = load_state()
                    st.setdefault(wid, {})["next_run"] = nxt
                    save_state(st)
            if (wid == "inbox" and self.triggered) or now >= nxt:
                out.append(wid)
        return out

    def _watch_icons(self) -> None:
        """菜单栏图标（微信、Lark）变了：让模型看变化前后两张小图，是新消息就记下，交给下一次巡检处理。"""
        if not (load_goals()["workflows"].get("inbox") or {}).get("enabled"):
            return
        import menubar
        try:
            if self.watcher is None:
                self.watcher = menubar.Watcher()
            changes = self.watcher.poll()
        except Exception:  # noqa: BLE001  没有权限、应用正在重启等，下一轮再看
            return
        for c in changes:
            name = menubar.APPS.get(c.app, c.app)
            try:
                verdict = menubar.judge(c, *self.judge_model())
            except Exception as e:  # noqa: BLE001  模型不可用：未读数变成非空就当新消息
                verdict = {"new_message": bool(c.after.title) and c.after.title != c.before.title,
                           "unread": c.after.title or None, "reason": f"模型判断出错（{type(e).__name__}），按未读数判断"}
            log_event("trigger" if verdict["new_message"] else "icon",
                      f"{name}图标「{c.before.title}」→「{c.after.title}」：{verdict['reason']}", verdict)
            if not verdict["new_message"]:
                continue
            if c.app == "WeChat":
                import wechat
                if wechat.paused():  # 微信被锁定 / 风控时不去碰它，等用户恢复
                    log_event("icon", f"{name}有新消息，但微信自动操作已暂停，不处理（/wechat on 恢复）")
                    continue
                kind = "wechat_unread"
            else:
                kind = "lark_unread"
            self.triggered.append({"app": name, "kind": kind, "unread": verdict.get("unread"),
                                   "at": time.time(), "key": f"{kind}|{time.time()}"})

    def judge_model(self) -> tuple:
        """看图判断用的 (client, model)：菜单栏图标变化、通讯录红点。"""
        if self._icon_model is None:
            a = self.make_agent()
            self._icon_model = (a.client, a.model)
        return self._icon_model

    def _urgent(self, wid: str) -> bool:
        """有图标报的新消息：不等用户空闲，马上跑。"""
        return wid == "inbox" and bool(self.triggered)

    def run_checkup(self, sites: list[str] | None = None, browser_too: bool = True, stop=lambda: False) -> dict:
        """自检：看各平台的登录状态（不调模型）。要重新登录的逐条记成 check_alert（报到中控是 warn），再记一句总结。
        你在终端里下了指令（或 stop() 为真）就不再看剩下的网站。返回 {text, results, stopped}。"""
        import agent as agent_mod
        import amients
        used = agent_mod.UsedApps()  # 看完把切到前台过的浏览器收起来
        halt = lambda: self.busy.is_set() or stop()  # noqa: E731
        try:
            results = checkup.run(sites, browser_too=browser_too, on_step=used.seen, stop=halt)
        finally:
            used.minimize()
        for r in results:
            if r["state"] == "login":
                log_event("check_alert", f"{r['name']} 要重新登录：{r['detail'].rstrip('。.')}。{r['hint']}", {"workflow": CHECKUP, **r})
        wanted = [p for p in checkup.PROBES if browser_too and p.site in (checkup.default_sites() if sites is None else sites)]
        stopped = sum(r["kind"] == "site" for r in results) < len(wanted)  # 有网站没来得及看
        text = checkup.summary(results) + ("（被打断，只查了一部分）" if stopped else "")
        checks = checkup.to_checks(results, checkup.check_names(sites))
        import agentstate
        checked = {checkup.check_name(r) for r in results}
        agentstate.record_checks([c for c in checks if c["name"] in checked])  # 这次没查到的不改记下的状态
        log_event("check", f"自检：{text}", {"workflow": CHECKUP, "results": results})
        amients.set_status("warn" if any(r["state"] == "login" for r in results) else "idle", f"自检：{text}")
        return {"text": text, "results": results, "checks": checks, "stopped": stopped}

    # ---------------- 自己的状态 ----------------
    def answer_state(self, task: dict, lease) -> dict:
        """中控来要状态（agent.state）：现算一份交回去；fresh 时先重新查一遍命令行工具的登录（不碰浏览器）。"""
        import agentstate
        p = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        if p.get("fresh"):
            lease.note("在查命令行工具的登录")
            results = checkup.run([], browser_too=False)
            agentstate.record_checks(checkup.to_checks(results, checkup.check_names([])), source="state")
        d = agentstate.snapshot(self)
        self._state_at = time.time()
        return {"text": agentstate.summary(d), "state": d}

    def _state_event(self, kind: str, text: str, data: dict) -> None:
        """中控会时不时来要状态：领到、做完不记动态（免得刷屏），只记出错。"""
        if kind in ("task_failed", "task_lost", "error"):
            log_event(kind, text, {"workflow": TASKS, **data})

    def _refresh_state(self) -> None:
        try:
            import agentstate
            agentstate.snapshot(self)
        except Exception:  # noqa: BLE001  状态文件写不了不影响工作流
            pass
        self._state_at = time.time()

    # ---------------- 领中控任务 ----------------
    def register(self) -> None:
        """开始领任务前：把能力注册给中控，再声明自检项（中控据此每天派 ami.check，第一次马上派）。"""
        import amients
        caps = capabilities()
        amients.register_capabilities(caps)
        names = checkup.check_names()
        if names:
            amients.declare_checks(names)
        log_event("registered", f"已把 {len(caps)} 项能力（{'、'.join(c['title'] for c in caps)}）和 {len(names)} 个自检项报给中控",
                  {"workflow": TASKS, "capabilities": [c["kind"] for c in caps], "checks": names})

    def _can_take_task(self) -> bool:
        """领任务线程问：现在能接活吗（这个工作流开着、调度空闲、没有排着的任务、能操作电脑）。忙的时候不领，留给别的 agent。"""
        return (not self.busy.is_set() and not self._working and not self.tasks
                and bool(load_goals()["workflows"].get(TASKS, {}).get("enabled")) and self.can_work())

    def submit_task(self, task: dict, lease) -> str:
        """领任务线程领到任务后交给调度：排进队，等调度线程轮到它、跑完再返回结果。排队期间照常回报进度，中控不会收回。"""
        import amients
        job = {"task": task, "lease": lease, "done": threading.Event()}
        lease.note("排队中，等手上的事做完")
        self.tasks.append(job)
        while not job["done"].wait(1):
            if lease.lost.is_set() or self._stop.is_set():
                try:
                    self.tasks.remove(job)
                except ValueError:
                    continue  # 调度线程已经拿走在跑了：停下 agent 后它会收尾
                if lease.lost.is_set():
                    return ""
                raise amients.TaskFailed("Ami 退出了，让中控重新派发", retry=True)
        if job.get("error"):
            raise job["error"]
        return job.get("result") or ""

    def run_task(self, job: dict) -> None:
        """在调度线程里跑一个中控派来的任务，结果放回 job，由领任务线程回报。"""
        import agent as agent_mod
        import amients
        task, lease = job["task"], job["lease"]
        try:
            if lease.lost.is_set():
                return
            state = load_state()
            state.setdefault(TASKS, {})["last_run"] = time.time()
            save_state(state)
            if str(task.get("kind") or "").strip().lower() in CHECKUP_KINDS:
                self._run_checkup_task(job)
                return
            if task.get("kind") in STATE_KINDS:
                job["result"] = self.answer_state(task, lease)
                return
            if task.get("kind") == DRAFT_KIND:
                lease.note("在起草")
                job["result"] = draft_posts(task, *self.judge_model())
                log_event("drafted", job["result"]["text"], {"workflow": TASKS, "task": task.get("id")})
                return
            agent = self.make_agent()
            agent.origin = f"task#{task.get('id')}"  # 上报时标明是哪个任务
            agent.max_steps = TASK_MAX_STEPS
            lease.on_lost = agent.stop
            lease.note("开始做")
            self.running = {"id": TASKS, "agent": agent, "started": time.time()}
            try:
                job["result"] = agent.run(task_prompt(load_goals()["workflows"][TASKS], task), agent_mod.AgentCallbacks(
                    log=lambda k, t: lease.note(t) if k == "action" else None))
            finally:
                self.running = {}
            if agent.stopped and not lease.lost.is_set():  # 被前台指令打断（或者 Ami 退出）：让中控重新派发
                job["error"] = amients.TaskFailed("被电脑前的指令打断了，让中控重新派发", retry=True)
        except Exception as e:  # noqa: BLE001
            job["error"] = e
        finally:
            job["done"].set()

    def _run_checkup_task(self, job: dict) -> None:
        """自检任务：直接用代码查。中控派的 ami.check 带着要查的项 {"checks": [名字…]}，就只看里面的网站；
        也可以带 {"sites": [...]}（只看这些网站）、{"browser": false}（只查命令行工具）。"""
        task, lease = job["task"], job["lease"]
        p = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        sites = p.get("sites") if isinstance(p.get("sites"), list) else None
        if sites is None and isinstance(p.get("checks"), list):
            sites = [n.removesuffix(" 网页登录") for n in p["checks"] if isinstance(n, str) and n.endswith(" 网页登录")]
        lease.note("在查各平台的登录状态")
        self.running = {"id": TASKS, "agent": None, "started": time.time()}
        try:
            r = self.run_checkup(sites, browser_too=p.get("browser", True) is not False, stop=lease.lost.is_set)
        finally:
            self.running = {}
        # 按中控自检的格式回报每一项；被打断没查到的也报上（算没通过），不让中控重派（自检有 5 分钟时限）
        job["result"] = {"text": r["text"], "checks": r["checks"]}

    def _loop(self):
        while not self._stop.wait(self.tick):
            if time.time() - self._state_at >= STATE_REFRESH:
                self._refresh_state()
            if self.busy.is_set():
                continue
            self._watch_icons()
            due = self.due()
            if due or self.tasks:
                import computer
                owner = computer.system_dialog()
                if owner:  # 系统弹窗在最前面：按键会落到它上面，这一轮不跑
                    if self._dialog_logged != owner:
                        log_event("error", f"屏幕上有系统弹窗（{owner}），后台巡检先暂停，等你处理完自动恢复")
                        self._dialog_logged = owner
                    continue
                self._dialog_logged = None
            if self.tasks:  # 中控的任务有时限：不等用户空闲、夜里也做；做完下一轮再看巡检
                try:
                    job = self.tasks.pop(0)
                except IndexError:  # 刚好被领任务线程撤回了（领取失效）
                    continue
                self._working = True
                try:
                    self.run_task(job)
                finally:
                    self._working = False
                continue
            for wid in due:
                if user_idle_seconds() < IDLE_NEEDED and not self._urgent(wid):
                    continue  # 定时巡检：用户正在用电脑就等他空闲了再跑
                extra, self.triggered = (self.triggered, []) if wid == "inbox" else ([], self.triggered)
                self._working = True
                try:
                    self.run_once(wid, extra)
                except Exception as e:  # noqa: BLE001
                    log_event("error", f"工作流「{wid}」出错：{type(e).__name__}: {e}")
                finally:
                    self._working = False
                break

    def run_once(self, wid: str, extra: list[dict] | None = None) -> dict:
        """extra：菜单栏图标报的新消息。有它时直接交给 agent 处理，不先看 Lark 会话列表、微信好友申请。"""
        goals = load_goals()
        wf = goals["workflows"][wid]
        state = load_state()
        st = state.setdefault(wid, {})
        st["last_run"] = time.time()
        st["next_run"] = next_run_after(st["last_run"], wf)  # 图标触发的这次也算，刚看过界面就不用再定时去看
        save_state(state)
        if wid != "inbox":
            return {"status": "skipped"}
        if extra:
            items = []
        else:
            import agent as agent_mod
            used = agent_mod.UsedApps()  # 看完把切到前台过的 Lark / 微信收起来
            try:
                judge = self.judge_model()
            except Exception as e:  # noqa: BLE001  模型不可用：这次不看微信好友申请
                log_event("error", f"看图模型不可用，这次不看微信好友申请：{type(e).__name__}: {e}")
                judge = None
            items = inbox_snapshot(on_step=used.seen, judge_model=judge)
            used.minimize()
        fresh = new_items(items, st.get("seen", [])) + list(extra or [])
        if not fresh:
            log_event("run", f"「{wf['name']}」看了一眼，没有新情况", {"workflow": wid, "checked": len(items)})
            _report_status(wf, f"{wf['name']}：{time.strftime('%H:%M')} 看过，没有新情况")
            return {"status": "idle"}
        log_event("run", f"「{wf['name']}」发现 {len(fresh)} 个新情况，开始处理",
                  {"workflow": wid, "items": [{k: v for k, v in i.items() if k != "key"} for i in fresh]})
        agent = self.make_agent()
        agent.origin = wid  # 上报时标明是这个工作流发起的
        self.running = {"id": wid, "agent": agent, "started": time.time()}
        events = []
        try:
            import agent as agent_mod
            final = agent.run(inbox_task(wf, fresh), agent_mod.AgentCallbacks(
                log=lambda k, t: events.append((k, t))))
        finally:
            self.running = {}
            if self.watcher is not None:
                self.watcher.rebase()  # 刚才打开聊天会让未读数变化，不算新消息
        stopped = any(k == "status" and t.startswith("已停止") for k, t in events)
        if stopped:  # 被前台打断的这批不算看过，下次再处理
            self.triggered = list(extra or []) + self.triggered
        else:
            state = load_state()
            seen = state.setdefault(wid, {}).setdefault("seen", [])
            seen += [i["key"] for i in fresh if i["kind"] not in ("wechat_unread", "lark_unread")]
            state[wid]["seen"] = seen[-500:]
            save_state(state)
        actions = [t for k, t in events if k == "action"]
        _report_status(wf, f"{wf['name']}：{final or '处理完了'}")
        log_event("run_done", final or ("被前台指令打断，下次再处理" if stopped else "处理完了"),
                  {"workflow": wid, "actions": actions, "stopped": stopped})
        return {"status": "stopped" if stopped else "done", "final": final, "actions": actions}


def _report_status(wf: dict, summary: str) -> None:
    """巡检完把当前状态报给中控。"""
    try:
        import amients
        amients.set_status("idle", summary, workflow=wf.get("name"),
                           interval_min=wf.get("interval_min"), interval_max=wf.get("interval_max"),
                           enabled=wf.get("enabled"))
    except Exception:  # noqa: BLE001
        pass


def status_snapshot(scheduler: "Scheduler | None" = None) -> dict:
    goals, state = load_goals(), load_state()
    wfs = []
    for wid, wf in goals["workflows"].items():
        st = state.get(wid) or {}
        wfs.append({"id": wid, **wf, "last_run": st.get("last_run", 0),
                    "next_run": st.get("next_run") if wf.get("enabled") else None,
                    "running": bool(scheduler and scheduler.running.get("id") == wid)})
    events = recent_events(300)
    return {"now": time.time(), "workflows": wfs, "events": events[-120:],
            "user_busy": bool(scheduler and scheduler.busy.is_set()),
            "idle_seconds": round(user_idle_seconds())}


if __name__ == "__main__":
    # python workflows.py serve        # 不开界面，只跑后台调度
    # python workflows.py once inbox   # 立刻跑一次消息巡检
    # python workflows.py peek         # 只看一眼有什么新情况（不调模型、不发东西）
    import sys

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    import agent as agent_mod

    def make_agent():
        return agent_mod.Agent(api_key=agent_mod.api_key_from_env(),
                               model=os.environ.get("OPENAI_MODEL") or agent_mod.DEFAULT_MODEL,
                               base_url=os.environ.get("OPENAI_BASE_URL") or None, max_steps=25)
    a = sys.argv[1:]
    sched = Scheduler(make_agent)
    if a[:1] == ["peek"]:
        st = load_state().get("inbox", {})
        items = inbox_snapshot(judge_model=sched.judge_model())
        print(json.dumps({"all": items, "new": new_items(items, st.get("seen", []))}, ensure_ascii=False, indent=1))
    elif a[:1] == ["once"]:
        print(json.dumps(sched.run_once(a[1] if len(a) > 1 else "inbox"), ensure_ascii=False, indent=1, default=str))
    elif a[:1] == ["serve"]:
        sched.start()
        print("后台工作流已启动，Ctrl+C 退出")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sched.stop()
    else:
        print(__doc__)
