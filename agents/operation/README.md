# Ami：macOS 上的 Computer Use（终端版）

在终端里输入一句指令，程序用 OpenAI Responses API 的 `computer` 工具（默认模型 `gpt-6-luna`）看屏幕截图、操作鼠标键盘，在这台 Mac 上把任务做完。
后台同时跑常驻工作流（消息巡检 + 盯菜单栏图标），见下面「常驻工作流」。

## 运行

```bash
cp .env.example .env   # 填入 OPENAI_API_KEY
./run.sh               # 交互模式：输入指令回车执行，后台同时跑消息巡检
./run.sh "打开备忘录"   # 只执行这一条指令，做完就退出（不跑后台巡检）
./run.sh serve         # 不接收指令，只跑后台工作流（消息巡检、领中控任务）
```

- `run.sh` 第一次运行时用 Python 3.10 以上（按 python3.14 … python3.10 找）建 `.venv` 并装依赖；系统自带的 `python3` 可能是 3.9，太旧，
  找不到时先 `brew install python`。
- **配置**：先读项目里的 `.env`，再用 `~/.computer-use/.env` 补上项目里没有的项（已设置的环境变量不覆盖）。
  Key / Base URL / 模型都写在 `.env` 里；填了 `TYPESAFE_API_KEY` 就自动开 Jev 加速。
- `WORKFLOWS=off` 不跑后台工作流（消息巡检、领中控任务）。同一时间只允许一个实例跑后台巡检（锁文件 `~/.computer-use/app.lock`），免得两个都巡检、抢屏幕。
- 日志打印在终端，同时追加写到 `~/.computer-use/app.log`。

用第三方中转（如 nevatoken）：在 `.env` 里填 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`，然后先跑一下检查脚本，确认它支持 Responses API 的 `computer` 工具（填了 `TYPESAFE_API_KEY` 时也会检查 Jev）：

```bash
.venv/bin/python check_endpoint.py
```

**权限**：macOS 把权限记在启动它的终端 App 上（Terminal / iTerm / Claude）。到「系统设置 → 隐私与安全性」给那个 App 打开：
- **辅助功能**：用于控制鼠标键盘
- **屏幕录制**：用于截图

打开后要完全退出并重新打开那个终端 App 才生效。启动时缺哪项会在终端里提示，并打印打开对应设置页的 `open 'x-apple.systempreferences:…'` 命令。

## 使用

交互模式里，不以 `/` 开头的输入都当成指令，回车执行；任务完成后可以继续输入，会接着同一个对话（上下键翻历史）。

| 命令 | 作用 |
|---|---|
| `/stop` | 停止正在跑的指令（运行中按 Ctrl+C 也一样） |
| `/pause` | 当前这一步结束后暂停，你自己操作电脑 |
| `/resume [你做了什么]` | 继续；把你说的操作告诉模型，从当前状态接着做 |
| `/new` | 新对话（清空上下文） |
| `/status` | 各个工作流（消息巡检、领中控任务）的目标、规则、上次运行时间、最近的动态，以及 amients 上报状态 |
| `/inbox` | 立刻跑一次消息巡检 |
| `/patrol on\|off` | 打开 / 暂停消息巡检 |
| `/tasks on\|off` | 打开 / 暂停领中控任务 |
| `/check` | 立刻自检一次（看各平台要不要重新登录，结果报给中控；平时中控每天派 `ami.check` 任务） |
| `/quit` | 退出（空闲时按 Ctrl+C / Ctrl+D 也一样） |

- 后台巡检的动态（新消息、处理完、出错）会直接打印在终端里；没有新情况的巡检不打印。
- **紧急停止**：除了 `/stop` / Ctrl+C，也可以把鼠标甩到屏幕任一角落（pyautogui 失效保护）。
- 所有操作都直接执行，不会停下来问你（发消息、加好友、发帖、`⌘Q` / `⌘⌫` 等快捷键都一样）；OpenAI 返回的安全检查自动通过。
  macOS 系统对话框在最前面时不按键。
- 每条指令结束后终端里会打印 `[报告]` 和本次运行报告（HTML）的路径：每一步的动作、耗时和截图，方便排查。
- 终端窗口会出现在截图里，模型看得到它；运行时别让终端挡住要操作的内容。
- 卡住检测：连续重复同样的操作，或操作后屏幕几乎没变化时，会提醒模型换方法，实在做不到就停下来告诉你遇到了什么问题。
- 对话很长时，较早的部分会自动压缩成摘要（中转每轮都要重发完整历史）。
- 停止或出错后会自动松开仍按着的 ⌘ / Shift 等按键和鼠标。
- 每次运行（一条指令或一轮巡检）结束后，把这次被切到最前面过的应用都最小化，浏览器也一样（网页窗口收进 Dock）。
  开始时你自己在用的那个应用（交互模式下一般就是终端）和 Finder 不动；`MINIMIZE_AFTER_RUN=off` 关闭。
- 等模型回复时可以 `/stop` 或 Ctrl+C 立刻放弃等待。单次请求最多等 60 秒、失败只重试 1 次
  （`MODEL_TIMEOUT` / `MODEL_RETRIES`），中转很慢时不会干等十几分钟。

## 人设（SOUL.md）

助理的人设写在 `SOUL.md`（默认是温柔细心、说话带 00 后聊天风的女孩子「Ami」），会追加在系统提示最后，只影响和你说话的语气：
操作规则都比人设优先，替你发出去的消息也不会带上她的语气。改这个文件就能换人设；
`COMPUTER_USE_SOUL=<文件路径>` 换成别的文件，`COMPUTER_USE_SOUL=off` 关闭。

## 技能（skills/）

技能是写给 Ami 的操作手册，教它把某类事从头做到尾。每个技能是一个目录，里面一份 `SKILL.md`：

```markdown
---
name: 发帖
description: 一句话说明什么时候用
---
正文：步骤、用哪些工具、注意什么
```

- 项目里的放在 `skills/`，你自己加的放在 `~/.computer-use/skills/<名字>/SKILL.md`（同名时你的优先）。
- 系统提示里只列名字和一句话说明，Ami 碰到对应的事会先用 `skill_load` 读全文再照着做。新加技能后重启 Ami 才会出现在列表里。
- 自带的技能：**发帖**（`skills/post/SKILL.md`）—— 问清楚 → 起草 → 给你过目 → 发 → 核对，
  覆盖 rednote / 小红书、X、B 站动态、GitHub、Telegram 频道，以及 Reddit、LinkedIn、知乎、即刻、V2EX、少数派、掘金等浏览器发帖；
  你给了完整原文并说直接发时不再确认；后台巡检里不发帖，只把草稿写进总结。

## 常驻工作流

agent 不只是「一条指令跑一次」：后台有按自己节奏循环的工作流（`workflows.py`），现在有两个：「消息巡检」（Lark、微信）和「领中控任务」（见下面）。消息巡检：右上角聊天软件图标一变就看，另外你空闲时每隔 30～60 分钟（随机，不用固定间隔，免得被微信风控）整体看一遍；微信只在通讯录图标有红点时才打开「新的朋友」。夜里（`WORKFLOW_QUIET_HOURS`，默认 23-8）不跑，新消息攒到早上处理。每次碰微信前都会检查「已被锁定」和安全风控提示，看到就暂停所有微信自动操作，本人处理完输入 `/wechat on` 恢复。

- **目标**：每个工作流有一段目标和几条规则，存在 `~/.computer-use/goals.json`。你在终端里输入的每句话先交给模型判断：
  是在改常驻目标（「以后陈宏找我就回收到」「好友申请都别通过了」「巡检改成 10 分钟」「先别巡检了」），还是一次性任务
  （「给 hong 发个消息」「看看谁找我」），改目标直接生效（改动记在动态里，`/status` 能看到），一次性任务照旧马上执行。
- **消息巡检**：先用代码看一眼 Lark 会话列表（最后一条不是你发的）和微信「新的朋友」（等待验证），和上次看过的比；
  没有新情况就不调模型、不花钱。有新情况才跑一次 agent loop：读完整上下文，按目标和规则决定回复、通过好友申请，或者只提醒你。
- **盯菜单栏图标**（`menubar.py`）：每 3 秒（`WORKFLOW_ICON_SECONDS`）用辅助功能找到右上角微信和 Lark 的图标（Lark 的图标属于它的「Lark Helper」子进程）、截下这几十个像素和上次比，
  不调模型、不动键鼠。图标变了（未读数变化、出现红点）才把前后两张小图交给模型判断「是不是来了新消息」，是就马上启动 agent（不等你空闲、不先看 Lark 会话列表），
  由 agent 去读未读的聊天（微信用 wechat_open / wechat_read，Lark 用 lark_chats / lark_read）、按目标和规则决定回不回。
  未读数变少 / 消失（你自己看过了）不算。
  巡检处理完会重新记下图标的样子，不会把自己打开聊天造成的变化当成新消息。
- **领中控任务**：和消息巡检并列的另一个工作流。有 `AMIENTS_API_KEY` 时，调度空闲（没在巡检、你的指令没在跑、
  没有排着的任务）就去 AMI 中控领派给这个 agent 的任务（长轮询，没任务时挂 25 秒再领），领到排进调度，轮到了用一个新的 agent
  （最多 80 步）去做，做完把结果报回中控。它的目标和规则也在 `goals.json` 里（默认规则：付款、删东西、替你答应事情这类要你点头的
  先别做，在结果里写清楚要你确认什么），可以像改巡检一样直接说话改。细节见下面「领任务」。
- **注册能力**：开始领任务前，先把能接的任务类型注册给中控（`PUT /api/v1/capabilities`），中控的「能力」页和派任务时能看到，
  派任务时会预填示例。Ami 只领这些类型和自检（`AMIENTS_TASK_KINDS=all` 什么类型都领）：
  | kind | 做什么 | 内容 |
  |---|---|---|
  | `computer.do` | 在这台 Mac 上做一件事（通用，最多 80 步） | `{"text": "要做的事"}` |
  | `message.send` | 用你本人的账号发消息（描述里列出现在能用的：Lark、微信、Telegram…） | `{"app", "to", "text"}` |
  | `message.inbox` | 看看 Lark / 微信谁找你，汇报要点和建议怎么回（只看不回） | `{"text": "可选"}` |
  | `post.draft` | 起草帖子：按各平台的写法起草（V2EX、少数派、掘金、即刻、小红书、X、知乎…），**只起草不发** | `{"platforms": [...], "text": "要说什么", "material": "背景资料"}` |
  | `agent.state` | 交回 Ami 自己维护的状态（见下面「自己的状态」），忙的时候也马上回 | `{"fresh": true}` 先重新查命令行工具的登录 |
  | `web.research` | 上网查资料，先给结论再给要点，每条附链接 | `{"text": "要查什么"}` |

  `post.draft` 不交给 agent：只调一次模型（带上「发帖」技能和这些平台的规矩），不操作电脑、不给工具，所以不可能发出去；
  结果是 `{"text", "drafts": [{"platform", "where", "title", "body", "tags", "notes"}]}`，在中控里看过，要发就在终端里让 Ami 发。
  能力的说明会带进给 agent 的指令里。改了能用的工具（装了命令行工具、填了 key）重启 Ami 后重新注册。
- **自己的状态**（`agentstate.py`）：Ami 自己维护一份 `~/.computer-use/agent_state.json`，中控时不时派 `agent.state` 任务来要，
  Ami 整份交回去（`{"text": 一句话, "state": {...}}`）。有一个专门领 `agent.state` 的线程，不占屏幕、不排队，在巡检或做任务时也马上回；
  这类任务领到、做完都不记动态（免得刷屏），只记出错。文件里有：
  - `accounts`：各平台账号能不能用（名字和自检项一样，如「rednote 命令行登录」），每项有 `ok`、`kind`、`detail`、`hint`、
    `checked_at`、`changed_at`（从好变坏 / 从坏变好的时间）、`source`（自检 / 工具 / 状态请求）。自检每查一项就更新；
    平时用 `xhs`、`bili`、`tg`、`twitter` 时报没登录也记下来（只记坏消息，调用成功不代表登录着）；
  - `running`：当前运行状态（running / idle / warn / error、一句话、更新时间），每次上报状态都跟着更新，`AMIENTS=off` 也记；
  - 现算的：进程（主机、pid、启动时间）、工作流（开没开、在不在跑、上次 / 下次）、你是否在用电脑、领任务和上报队列、
    能用 / 没开的工具、权限（辅助功能、屏幕录制）、微信是否暂停。每分钟刷新一次，来要状态时也现算。
  - `/status` 里也会列出各账号的状态。
- **自检**（`checkup.py`，中控每天派的 `ami.check` 任务）：启动时把自检项报给中控（`PUT /api/v1/checks`），中控就自动开启
  每日自检（默认每天 09:00，在中控里改时间、点「立即自检」），第一次马上派。Ami 自己不定时跑。
  领到 `ami.check`（也认 `checkup`、`login-check`、`自检`、`每日自检`）不交给 agent，直接用代码查，不调模型、不替你登录：
  - 命令行工具：rednote / 小红书（`xhs`）、B 站（`bili`）、Telegram（`tg`）、X（`twitter`）各跑一次只读的 `status`，
    自检项叫「rednote 命令行登录」这样；没装的不算自检项；
  - 浏览器里的网站：在你的浏览器里打开一个要登录才能看的页面（rednote 通知、X 首页、Reddit 设置、LinkedIn 动态、知乎、
    B 站个人中心、linux.do 设置、微博、即刻、V2EX 设置、少数派写文章、掘金个人设置），被跳到登录页、页面上有密码框、
    或者出现没登录才有的「登录」按钮就算掉线；自检项叫「zhihu.com 网页登录」这样。自己新开的标签页看完就关，最后把浏览器最小化。
    碰到 Cloudflare「请稍候…」这类人机验证页，最多再等 20 秒让它自己过去（不点、不做验证）；过不去就报「没查到」，
    不说登录着也不说掉线，提示你在浏览器里打开过一下验证。
    看哪些网站：`CHECKUP_SITES`（逗号分隔，`off` 不碰浏览器），默认全部。
  - 按中控的格式回报每一项：`{"checks": [{"name", "ok", "kind", "message", "hint"}]}`，掉线是 `auth`、网络问题是 `network`；
    `hint` 写怎么重新登录，会出现在中控和推送的通知里。要重新登录的另外记一条动态（warn）。
  - 你在终端里下指令时没看完的网站就不看了，这些项照样报上去（算没通过、写明被打断），中控有 5 分钟时限，不等重派。
  - `/check` 立刻在本机查一次（同样报给中控）；只查不报：`.venv/bin/python checkup.py`（`checkup.py cli` 只查命令行工具）。
- **直接做，不问你**：agent 按目标和规则决定的回复、通过好友申请都直接执行，不经过额外判断、也不扣下。
  本地消息记录照样生效。
- **不抢你的电脑**：一次只跑一个工作流，消息巡检和中控任务排队轮流跑，谁也不打断谁（任务排着时先做任务，做完再巡检）。
  你最近 60 秒有键鼠操作就延后（`WORKFLOW_IDLE_SECONDS`）。定时巡检只在空闲时跑；图标报的新消息和中控任务不等空闲，
  中控任务夜里也做（有时限）。你在终端里下指令时，正在跑的工作流会停下让出屏幕：巡检的这批新情况下次再处理，任务让中控重新派发。
- **看状态**：交互模式里 `/status`：目标、规则、上次巡检、是否正在处理，以及最近的动态（发出了什么、目标改了什么）。
  新消息、处理完、领到任务、出错这些动态也会实时打印在终端里。`/inbox` 立刻巡检一次，`/patrol on|off` 打开 / 暂停巡检，
  `/tasks on|off` 打开 / 暂停领中控任务。
- `WORKFLOWS=off` 不启动后台工作流；`./run.sh serve` 只跑后台工作流、不接收指令。

```bash
.venv/bin/python workflows.py peek          # 只看一眼有什么新情况（不调模型、不发东西）
.venv/bin/python workflows.py once inbox    # 立刻跑一次消息巡检
.venv/bin/python workflows.py serve         # 只跑后台工作流（平时用 ./run.sh serve）
.venv/bin/python menubar.py watch --judge   # 只盯菜单栏图标，变了就打印模型的判断（不跑巡检）
```

限制：微信的会话列表还读不到未读消息，所以微信这边目前只处理新的好友申请。

## 上报每一步的状态（AMI 中控 amients.com）

agent 每一步的状态都会上报到 AMI 中控（`amients.py`），在 amients.com 登录后能看到这个 agent 在不在线、正在做什么、每一步的动态。

- **事件**（`POST /api/v1/events`）：前台指令和后台工作流的每次运行开始、每个动作、状态、出错、结束（耗时、模型请求次数），
  以及工作流的巡检、发出、改目标。出错是 error，其余 info；
  运行 id、发起方（你 / 工作流）、主机名放在 data 里。不含截图，文字超过 1000 字截断，等待中的进度刷新不上报。
  注意动作里会带消息内容（例如「Lark 发送给「A」: '…'」）。
- **当前状态**（`PUT /api/v1/status`）：运行开始报 running（在做什么），结束报 idle（结果）。
- **心跳**（`POST /api/v1/heartbeat`）：空闲时每 60 秒一次，保持在线。
- 事件先进本地待发队列 `~/.computer-use/amients_outbox.jsonl`（最多留最近 5000 条），后台每 5 秒分批发（一次最多 200 条），
  失败退避重试，成功才往后走，不会丢。交互模式里 `/status` 能看到待发多少条、上次成功和最近的失败。
- **领任务**（常驻工作流「领中控任务」，见上面）：`POST /api/v1/tasks/pull` 领，一次一个。
  - 任务内容是纯文本、或 `{"text": …}`（也认 `task` / `instruction` / `prompt`）时就是指令本身，其余字段附在后面；
    别的 JSON 原样交给 agent，类型（kind）一起告诉它。
  - 排队和做的过程中都会回报进度（`/progress`，每个动作都带上，至少每 1/3 个时限一次，时限就会往后延），中控不会收回。
  - 做完回报 `/complete`（结果是 agent 最后那句话）；出错、被你在终端里下的指令打断，回报 `/fail` 并让中控重新派发；
    中途领取失效（超时被收回、被取消）就停下 agent、丢掉结果。
  - 领到、做完、失败、被收回都记在工作流动态里（`/status`、终端里实时打印「〔中控任务·…〕」）。
  - 只有跑后台工作流的那个 Ami 领任务（`WORKFLOWS=off` 不领）；没权限（辅助功能 / 屏幕录制）时不领，留给别的 agent。
- 配置：`AMIENTS_API_KEY`（在 amients.com 的 API Keys 里创建，一个 agent 一个）有值才发，`AMIENTS_URL` 默认
  `https://amients.com`，都写在 `.env` 里。`AMIENTS=off` 不记录、不发送；`AMIENTS_TASKS=off` 只上报、不领任务。

## 模型

默认用便宜的 `gpt-6-luna`（nevatoken 上是 `MaaS_GP_6_luna_20260922`），模型的工具调用直接执行。
换模型直接改 `.env` 里的 `OPENAI_MODEL`。

`bench_models.py` 对比不同模型的第一步决策（16 个场景，只看第一步做什么、不执行，按 OpenAI 官方价格算每步成本）：

```bash
.venv/bin/python bench_models.py decide --models MaaS_GP_6_luna_20260922,MaaS_GP_6_sol_20260922 --reps 2
.venv/bin/python bench_models.py regrade <decide 的结果 JSON>   # 改了打分规则后重新打分，不再请求模型
```

## Jev 加速（可选）

在 `.env` 里填了 `TYPESAFE_API_KEY` 就自动开启 Jev 加速（不填就不用）。开启后每条指令先走快速模式：

1. 用 macOS 辅助功能（AX）读出最上层应用的控件表（不截图）：展开着的菜单、菜单栏、窗口里的按钮 / 输入框 / 列表等
2. 一次请求问 [TypeSafe Jev](https://docs.typesafe.ai/introduction)「做什么操作 + 对哪个元素」，通常 0.6–1 秒返回
3. 直接执行。可用操作：点击（优先 `AXPress`）、输入文字（文字由上面的模型单独生成）、回车、Esc、滚动、
   打开已安装的应用（按系统显示名，如「备忘录」）

Jev 返回 `BLOCKED`、置信度低于 `JEV_MIN_CONFIDENCE`（默认 0.6）、连续两步界面没变化、点了没反应时，
交给 GPT 截图循环接着做，并告诉它已经做过哪些步骤；Jev 认为 `DONE` 时也会让 GPT 截图核实一次。
每条指令结束时日志里会显示「用时 · Jev 几步 · GPT 请求几次」。

- **动作缓存**：一条指令由 Jev 走完、GPT 核实时没再做任何操作，就记住这些步骤（操作 + 控件特征，不是坐标）。
  下次完全相同的指令直接回放，不调用任何模型；每一步都先确认控件还在，对不上就作废缓存、回到正常流程。
- **点了没反应**：勾选框按下前后读值、输入框输入后读回内容核对，不一致就交给 GPT。
- **读不到控件的应用**（控件少于 8 个，如游戏、Canvas）：用 OCR 识别出的文字补充元素表，让 Jev 也能点。
- 元素表会去掉被父控件包住的重复子控件，并标出上一步之后新出现的控件（弹出的菜单、对话框）。
- **跨任务经验**：步数较多的任务结束后，让模型总结一两句经验；以后遇到相似指令时附给模型参考。

Jev 的每一步也都直接执行：安全输入框（密码框）也能输入，点「删除 / 发送 / 付款 / 退出 / 关机」这类控件不会先问你，
Apple 菜单也照常读。

**Electron 应用**（Slack、飞书、Cursor 等）：程序会自动打开它们的辅助功能开关，并等内容出现（第一次读时多等不到 1 秒）。
**浏览器**：Jev 不主动打开浏览器的网页辅助功能（会让浏览器变慢），网页上的操作交给 GPT；读网页内容用 `browser_read_page`。

## 微信

微信 4.x 是自绘界面，辅助功能读不到它的控件，所以 Jev 帮不上忙；改用「快捷键 + 本地 OCR 校验」，给模型两个函数工具：

- `wechat_send(联系人, 内容)`：点搜索框输入名字 → 只点「联系人 / 群聊 / 功能」分区里名字**完全一致**的结果
  （「聊天记录」等分区里的同名文字不点；找不到就报错并列出相近的名字，不会猜）→ OCR 核对聊天标题 →
  清空草稿、粘贴 → OCR 核对输入框里就是这条内容、再核对一次聊天对象 → 回车 →
  OCR 确认消息出现在聊天里。整个过程不会在别处按回车。
- `wechat_read(联系人, 条数)`：打开聊天，OCR 识别当前屏幕可见的消息，按气泡左右区分「我 / 对方」

**加好友 / 好友申请**（`wechat_friend.py`）：

- `wechat_add_friend(微信号或手机号, 验证消息, 备注)`：「+」→ 添加朋友 → 搜索 → 添加到通讯录 → 填验证消息和备注 →
  确定。搜不到、已经是好友时直接告诉你，不做操作。如果对方已经申请过加你，出来的是「通过朋友验证」，
  这一步就是同意他的申请。
- `wechat_friend_requests()`：通讯录 →「新的朋友」里的申请（名字、附言、等待验证 / 已添加 / 已过期）。
- `wechat_accept_friend(名字, 备注)`：打开申请详情 → 前往验证 → 确定，
  之后回到「新的朋友」核对已经变成「已添加」。说「有新的好友申请就加」时，会把等待验证的申请逐个通过。
- 按钮都按 OCR 认出的文字去点，窗口按标题区分（「添加朋友」「申请添加朋友」「通过朋友验证」）。
- 防风控：看到「操作过于频繁」立刻停；微信弹出自己的安全验证提示时也停下，不点、不绕过，交给你本人验证。
  和发消息共用一把跨进程的微信界面锁。
- 「+」按钮、左侧栏图标、各小窗口的输入框和关闭按钮都不写死坐标，由模型看截图找；发出新申请（「申请添加朋友」窗口）
  那条路径还没在真实微信上走过。

```bash
.venv/bin/python wechat_friend.py search 微信号      # 只搜索，看搜到谁，不添加
.venv/bin/python wechat_friend.py requests          # 列出「新的朋友」
.venv/bin/python wechat_friend.py accept 名字        # 同意某人的申请
```

防误发：发送 / 读取全程持有一把跨进程的锁，同一时间只有一个流程能操作微信。

**版本注意**：微信 4.1.60 起主窗口禁止截图，本方案会失效。建议关闭微信自动更新；程序检测到截图里识别不到微信界面时
会直接报错停下，不会在看不见的情况下操作。

文字识别用 macOS 自带的 Vision，在本地完成。界面元素在哪（搜索框、会话列表、聊天标题、输入框、导航栏图标等）
不写死坐标，而是把微信窗口截图发给模型让它框出来（`wechat.layout`），同一窗口大小、同一页缓存 10 分钟；
点完照旧用 OCR 核对，核对不过就停下并在下次重新看。注意这些截图会发给模型接口，里面可能有聊天内容。
指令里提到「微信」时跳过 Jev，直接交给 GPT + 微信工具。
朋友圈、文件等其他操作仍走截图模式。微信对自动化有风控，不要让它群发或高频发送。

调试（需要在有「屏幕录制」权限的终端里运行，`inspect` 的输出会把聊天内容打码）：

```bash
.venv/bin/python wechat.py inspect [contacts]        # 识别到的文字位置 + 模型找到的界面元素
.venv/bin/python wechat.py inspect-search 文件传输助手   # 只搜索、识别，不点击
.venv/bin/python wechat.py inspect-input                # 往输入框粘一串探针文字再清空，看输入区位置
.venv/bin/python wechat.py send 文件传输助手 测试消息
```


## Lark / 飞书客户端（你本人的账号）

操作 Mac 上已登录的 Lark / 飞书客户端，能读私聊、以你本人的身份发消息（和下面「飞书 / Lark（开放平台接口）」的机器人身份不同）。
Lark 客户端是 Chromium 内核，打开辅助功能后能读到网页的 DOM 和 class 名，所以按 class 名找会话、消息和输入框，
不截图、不 OCR，也不依赖窗口大小和界面语言：

- `lark_chats(关键词)`：左侧会话列表里已加载的会话：名字、最后一条消息预览、时间、未读数、标签（机器人 / 全员…），
  以及最后一条是不是你自己发的（`last_from_me`：自己发的消息预览前有个已读 / 未读的小圆圈），免得把你发的说成对方发来的。
- `lark_read(联系人, 条数)`：打开聊天，读客户端已加载的最近消息（从旧到新），标出「我 / 对方名字 / 系统」和日期分隔。
- `lark_send(联系人, 内容)`：会话列表里有名字**完全一致**（不区分大小写和空格）的会话就直接点开，否则 ⌘K 搜索，
  只点名字完全一致的联系人 / 群结果（没有或有多个就报错并列出候选，不猜）→ 核对聊天标题 →
  清空草稿、粘贴 → 读回输入框核对就是这条内容 → 再核对聊天对象和输入框焦点 → 回车 → 在聊天里找到这条消息。整个过程只按这一次回车。

**梗图**：`lark_send` 的 `meme` 参数可以在文字后面再发一张梗图（单独一条）。填 `auto` 时，程序把最近的聊天和要发的话交给
Jev，在同一个请求里问「要不要配」（是 / 否的概率）和「配哪张」（梗图之间选最贴切的），0.4–0.9 秒出结果：
工作消息、正式通知、严肃话题不配，闲聊才配；Jev 不可用或出错就不配。
梗图超过 40 张时分组同时问，再从各组第一名里挑。没配 `TYPESAFE_API_KEY` 时，如果 GPT 走的是 nevatoken，
就用同一个 key 调 nevatoken 转发的 Jev（`{OPENAI_BASE_URL}/systemone`）。

- 梗图库：放在 `~/.computer-use/memes/`（或 `MEMES_DIR`），仓库里不带梗图。可以分子目录，文件名就是梗图名，用途从文件名里猜
  （「负鼠00002-收到办不到」→「收到办不到」），也可以在同目录的 `memes.json` 里写。
- 网上的梗图：`python memes.py fetch <目录名>` 从 [ChineseBQB](https://github.com/zhaoolee/ChineseBQB) 下载一个目录到
  自己的梗图目录。那个仓库没有许可证、版权归原作者，所以只在本机自用，不放进本仓库。
- 图片以 PNG 放进剪贴板粘贴到输入框，用辅助功能核对输入框里确实是一张图、没有文字后才回车。GIF 会同时放原图，
  Lark 能不能发成动图还没实测。

```bash
.venv/bin/python memes.py                              # 列出所有梗图和用途
.venv/bin/python memes.py pick 辛苦啦 早点休息           # 问 Jev 这条回复配哪张
.venv/bin/python memes.py fetch 109_Opossum_负鼠_BQB     # 下载 ChineseBQB 的一个目录
.venv/bin/python lark.py send "Hong Chen" 收到 --meme 负鼠00002-收到办不到
```

防误发：全程持有跨进程的锁。关闭搜索框前会先清空搜索词
（直接按 Esc 的话 Lark 会把搜索词留在侧边栏，继续过滤会话列表）。

```bash
.venv/bin/python lark.py chats              # 会话列表
.venv/bin/python lark.py search hong        # 只搜索、列出联系人 / 群结果，不点击
.venv/bin/python lark.py read "Hong Chen" 20
.venv/bin/python lark.py send "Hong Chen" 测试消息
```

## 网站（浏览器操作）

在你已经登录的浏览器（Chrome / Safari / Edge 等）里操作，不调用网站接口。通用工具：

- `browser_open`：打开网址。当前标签页已经是同一个网站时就在这个标签页里跳转，别的网站才新开标签（不覆盖你在看的网页）。
- `browser_read_page`：通过浏览器的辅助功能读页面上显示的文字和链接，比看截图读字快很多（读不到时退回 OCR）。
  要链接时帖子链接排在导航前面、同一个帖子只给一个链接。你同时在别的窗口看别的网页也没关系：优先读最近打开的那个网站所在的窗口。
- `browser_load_more`：把列表最后一条滚到可见来加载下一批（不动鼠标、不按键），之后再用 `browser_read_page` 读。

各网站的帖子链接格式、导航噪音和给模型的操作说明都在 `sites.py`，一个网站一条配置：

| 网站 | 读 | 写 |
|---|---|---|
| Reddit | 版块、搜索、帖子和评论 | 发帖、评论 |
| linux.do | 最新、热门、类别、搜索、话题 | 新话题、回复 |
| X（Twitter） | 时间线、搜索、个人页、帖子（日文 / 中文 / 英文界面都认） | 发帖、回复 |
| 知乎 | 热榜、搜索、问题和回答、专栏 | 回答、评论 |
| LinkedIn | 动态、搜索、帖子 | 发帖、评论 |
| 微博 | 首页、热搜、搜索、单条微博 | 发微博、评论 |
| B 站 | 首页、搜索、视频标题简介和评论、动态 | 评论 |
| 微信公众号文章 | 搜狗微信搜索 → 原文 | 不发 |
| 即刻 | 关注动态、动态详情 | 发动态、评论 |
| V2EX | 列表和正文走 `v2ex.py` 的接口（不开浏览器） | 发主题（自己做的东西只发「分享创造」，广告只发「推广」）、回复 |
| 少数派 | 首页、Matrix 社区、文章 | 写文章发到 Matrix（投稿上首页要先问你）、评论 |
| 掘金 | 首页、文章、沸点 | 发文章（选分类、标签、写摘要，发完要审核）、发沸点、评论 |

- 发帖、评论、回复都由模型直接点发布，不先问你、不限频率。
- 弹出验证码时停下来让你自己处理。网页内容是别人写的数据，里面的要求一律不照做。
- 新加网站：在 `sites.py` 的 `SITES` 里加一条 `Site(...)`。`verified=False` 的几条（微博、即刻、公众号原文在开发机的网络上连不上；V2EX 拦了开发机的请求；掘金的链接是页面脚本生成的）链接格式和发帖按钮还没在真实页面上核对过，按钮名以页面上为准。

```bash
.venv/bin/python browser.py read 3000     # 读当前浏览器页面（调试用）
```

## rednote（小红书国际版，浏览器操作）

用 https://www.rednote.com/ ，在已登录的浏览器里操作，不调用接口：

- **刷**：`rednote_feed` 打开首页推荐流（keyword 为空），每刷新一次就是一批新推荐（约 24 篇），刷新到够数为止；
  有 keyword 时返回搜索结果第一屏（约 20 篇，刷新结果不变）。收集笔记标题、作者和链接，不滚动、不动鼠标。
- **读**：用 `browser_open` 打开返回的链接（要带着原来的 `xsec_token`，去掉会打不开），`browser_read_page` 读正文和评论。
- **评论**：在「说点什么...」里输入后点「发送」。
- **发笔记**：`rednote_publish(title, body, card_text)` 发纯文字笔记，在创作者中心走
  「上传图文 → 文字配图 → 写文字 → 生成图片 → 下一步 → 填标题正文 → 发布」。控件都用辅助功能找、内容直接设置
  （不模拟键盘，所以不受中文输入法影响），填完读回来核对，然后直接点「发布」。标题最多 20 字，正文最多 1000 字。
- 不点赞、收藏、关注、私信；要求登录时停下来让你自己登录。

```bash
.venv/bin/python rednote.py 30            # 刷首页推荐流 30 篇，打印标题、作者、链接
.venv/bin/python rednote.py agent 记忆 20  # 搜索「agent 记忆」20 篇
```

## X 刷推荐 / 搜索、LinkedIn 搜索（浏览器操作）

- **X**：`x_feed(keyword, sort, count)`。keyword 为空时刷主页推荐流：把最后一条滚到可见来加载下一批（不动鼠标），
  滚不出新帖子时刷新一次换一批；有 keyword 时看搜索结果（sort 选 top 热门 / latest 最新）。每条帖子整理成作者、账号、时间、
  正文、引用的帖子、回复 / 转帖 / 喜欢 / 浏览数和链接，广告跳过；弹出 Premium 订阅页时自动重新打开。实测 25 条约 18 秒。
- **LinkedIn**：`linkedin_search(kind, keywords, count)`，kind 为 people / companies / posts（按最新）/ jobs。
  人和公司按页翻（每页 10 个），帖子和职位滚动加载；每个结果整理成一段文字（名字、标题、地点、摘要等）和链接
  （帖子卡片里没有帖子本身的链接，给的是作者主页）。只读公开展示的信息，不加好友、不发消息。

```bash
.venv/bin/python twitter.py 30                       # 刷 X 推荐流 30 条
.venv/bin/python twitter.py jev 20                   # 搜索 X「jev」
.venv/bin/python linkedin.py people "AI agent memory" 10
```

## 小红书 / X / B 站 / Telegram 命令行工具（你本人的账号）

接入了 jackwener 的四个命令行工具，走各平台网页用的接口（多是逆向出来的），不开浏览器、不动鼠标，结果是结构化的 JSON。
装了哪个就提供哪个工具（没装时自动退回上面的浏览器 / 机器人方式）：

| 平台 | 命令行工具 | 安装 | 登录（你本人在终端里做） | 工具 |
|---|---|---|---|---|
| rednote（小红书国际版） | [xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli) + `rednote_cli.py` | `uv tool install xiaohongshu-cli` | 浏览器里登录 www.rednote.com，自动读 cookie；或 `python3 rednote_cli.py login` | `xhs` / `xhs_write` |
| X（默认不启用，见下） | [twitter-cli](https://github.com/jackwener/twitter-cli) | `uv tool install twitter-cli` | 浏览器里登录 x.com 即可，每次自动读 cookie | `twitter` / `twitter_write` |
| B 站 | [bilibili-cli](https://github.com/jackwener/bilibili-cli) | `uv tool install bilibili-cli` | `bili login` 扫码（或读浏览器 cookie） | `bili` / `bili_write` |
| Telegram | [tg-cli](https://github.com/jackwener/tg-cli) | `uv tool install kabi-tg-cli` | `tg chats`（第一次会问手机号和验证码） | `tg` / `tg_write` |

- rednote：xiaohongshu-cli 只支持 www.xiaohongshu.com，`rednote_cli.py` 在它加载前把域名换成 rednote.com
  （接口 webapi.rednote.com，同一套网页、同样的接口路径），登录信息存在 `~/.rednote-cli`，不改它装好的文件。
  默认就连 rednote；设 `XHS_SITE=xiaohongshu` 改回小红书国内版（那时用 `xhs login` 登录）。
  rednote 模式下不开放发笔记（图片上传没适配），发笔记还是用 `rednote_publish`；扫码登录也没适配。
- X 默认走浏览器（`x_feed` 和浏览器工具）：twitter-cli 模仿网页内部接口，新账号发帖会被 X 拒绝。设 `X_VIA=cli` 才启用 twitter-cli 的工具。
- 每个平台两个工具：只读的 `<名字>` 和会发出内容的 `<名字>_write`（点赞、评论、发帖、发消息等，直接生效）。
  参数是子命令 + 参数列表，子命令只允许白名单里的；删除、登录登出、写 / 读本地文件（`--output` 等）、跳过确认（`--yes`）都不开放。
  公共的调用、解析和截短在 `cli_runner.py`。
- 读浏览器 cookie 时 macOS 可能弹「钥匙串」授权（读 Chrome 的 cookie 密钥），允许后才能用。
- 防封：都是你本人的账号，请求多了会被风控。X 和 B 站的 `--max` 最多 50；Telegram 全量同步（`tg refresh`）
  两次之间至少隔 12 小时，默认 `--max-chats 30 --delay 3`，要最新消息用查询的 `--sync-first` 或 `tg sync <聊天>`；
  tg-cli 建议在 [my.telegram.org](https://my.telegram.org) 申请自己的 `TG_API_ID` / `TG_API_HASH`。
- GitHub 本来就走 `gh` 命令行（见下面的 GitHub 一节）。

## 有官方接口的服务（不开浏览器）

### 网页搜索（Exa）、RSS 订阅源

- **网页搜索（Exa）**（`exa.py`）：`exa_search` 搜网页（可限最近几天 / 指定网站）、`exa_contents` 读网页正文、
  `exa_answer` 直接问答并附来源。需要在 `.env` 里配 `EXA_API_KEY`（https://dashboard.exa.ai/api-keys），没配时不提供这些工具。
  没有专门工具的问题（「查一下最近…」）先搜再读，回答里附上链接。
- **RSS / Atom**（`rss.py`）：`rss_read` 读任意订阅源或网站（自动找订阅源），支持简写 `hn`、`hn:<关键词>`、
  `github:<owner/repo>`（Release）、`youtube:<频道 id 或 @handle>`、`substack:<名字>`、`medium:@<用户>`、`arxiv:<分类>`、
  `sspai`、`36kr`；`rss_subscribe` / `rss_unsubscribe` 管理订阅列表（存在 `~/.computer-use/feeds.json`），
  `rss_digest` 汇总所有订阅最近的更新。不需要 key。（少数派、36氪在开发机的网络上连不上。）

### Hacker News、V2EX、GitHub

- **Hacker News**（`hn.py`）：`hn_stories` / `hn_search` / `hn_item`，走公开接口（Firebase + Algolia）只读，不需要 key。
- **V2EX**（`v2ex.py`）：`v2ex_topics` / `v2ex_topic`，走公开 v1 接口只读；配了 `V2EX_TOKEN` 时节点主题可以翻页，
  并多一个 `v2ex_notifications` 读自己的提醒。
- **GitHub**（`github.py`）：`github_search` / `github_repo` / `github_issue` / `github_list` 只读，
  `github_comment` / `github_create_issue` 评论和新建 issue（直接发出）。
  走本机已登录的 `gh` 命令行（先 `gh auth login`），不单独存 token。

### AI 论文 / 模型、GitHub Trending

- **AI 论文 / 模型**（`papers.py`，公开接口，不需要配置）：`papers_daily` 看 Hugging Face 每日论文（按点赞排序，可指定日期），
  `papers_search` 按主题搜论文，`paper_detail` 读一篇 arXiv 论文的详情（HF + arxiv.org 摘要页：作者、摘要、版本、备注、PDF / HTML 链接），
  `arxiv_new` 看 arXiv 分类最新论文（RSS，如 cs.AI、cs.CL、cs.LG），`hf_trending` 看 HF 热门模型 / 数据集 / Space。
  arXiv 的 export 接口在开发机的网络上连不上，所以不用。
- **GitHub Trending**：`github_trending` 看今天 / 本周 / 本月的热门仓库（可按语言筛），直接读 github.com/trending 网页，不经过 gh。

### Discord / Slack / Telegram（机器人接口）

用机器人 token 调官方接口读消息、发消息。机器人只能看到它加入了的服务器 / 频道 / 群，看不到你本人的私信。
没配 token 的服务不会出现在工具列表里。发送直接执行，不先问你、不限频率。发出的 Discord 消息不会 @ 任何人；Telegram 发纯文本。

**Discord**（`discord_channels` / `discord_read` / `discord_send`）
1. 打开 https://discord.com/developers/applications → New Application → Bot → Reset Token，复制到 `DISCORD_BOT_TOKEN`。
2. 同一页打开 **Message Content Intent**（不开的话读到的消息正文是空的）。
3. OAuth2 → URL Generator：scopes 勾 `bot`，权限勾 View Channels、Read Message History、Send Messages，
   用生成的链接把机器人加到服务器。
4. 频道写成 `服务器名/#频道名` 或频道 id（开发者模式下右键频道 → 复制 ID）。

**Slack**（`slack_channels` / `slack_read` / `slack_send`）
1. https://api.slack.com/apps → Create New App → From scratch。
2. OAuth & Permissions → Bot Token Scopes 加：`channels:read`、`groups:read`、`channels:history`、`groups:history`、
   `users:read`、`chat:write`。
3. Install to Workspace，复制 Bot User OAuth Token（`xoxb-…`）到 `SLACK_BOT_TOKEN`。
4. 在要用的频道里 `/invite @你的机器人`（公开、私有频道都要）。

**Telegram**（`telegram_updates` / `telegram_send`）
1. 在 Telegram 里找 @BotFather → `/newbot`，复制 token 到 `TELEGRAM_BOT_TOKEN`。
2. 把机器人拉进群 / 设为频道管理员；私聊需要对方先给机器人发过消息。
   群里机器人默认只收到 @它 和命令，要看所有群消息需在 @BotFather 里 `/setprivacy` → Disable。
3. `telegram_updates` 只能看到最近 24 小时内机器人收到的消息（不会把消息标记为已处理）；
   机器人设置了 webhook 时用不了。发消息的目标写数字 chat id、`@频道用户名`，或最近消息里出现过的聊天名。

### 飞书 / Lark（开放平台接口）

不操作飞书客户端（那是上面的 `lark_*` 工具），而是用开放平台的自建应用（机器人身份）调接口。配置了 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 才启用：

- `feishu_doc_read(链接或 token, 字数)`：读新版文档（docx），也支持知识库（wiki）链接（解析成实际的 docx），返回标题和纯文本。
- `feishu_chats(关键词)`：列出机器人所在的群（群名 + chat_id）。
- `feishu_read(群, 条数)`：读群里最近的消息，文本 / 富文本转成纯文字，图片、文件等显示成占位。
- `feishu_send(群, 内容)`：以机器人身份发一条文字消息。群用 chat_id（`oc_` 开头）或完全一致的群名，找不到 / 重名直接报错不猜。

设置步骤：

1. 打开开放平台：Lark 国际版 https://open.larksuite.com/app ，飞书国内版 https://open.feishu.cn/app 。
   「创建企业自建应用」（Create Custom App），填名字和图标。
2. 「凭证与基础信息」里复制 App ID、App Secret，填进 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`；
   国际版 `FEISHU_DOMAIN=lark`（默认），国内版 `FEISHU_DOMAIN=feishu`。文档链接是 `larksuite.com` 就用 lark。
3. 「添加应用能力」→ 启用「机器人」。
4. 「权限管理」开通：
   - 读文档 `feishu_doc_read`：`docx:document:readonly`；知识库链接另加 `wiki:wiki:readonly`
   - 列群 `feishu_chats`：`im:chat:readonly`
   - 读消息 `feishu_read`：`im:message:readonly`、`im:message.group_msg`（读群里所有消息；敏感权限，可能要管理员审批），
     可选 `contact:user.base:readonly`（显示发送者名字）
   - 发消息 `feishu_send`：`im:message:send_as_bot`
5. 「版本管理与发布」→ 创建版本并发布，等管理员审核通过。之后每次加权限都要重新发布才生效。
6. 把机器人拉进要读 / 发的群；要读的文档在右上角「…」→「添加文档应用」把应用加进来。

```bash
.venv/bin/python feishu.py chats            # 列出机器人所在的群
.venv/bin/python feishu.py read 产品组 10    # 读最近 10 条
.venv/bin/python feishu.py doc https://xxx.sg.larksuite.com/docx/<token>
```

## 文件

- `cli.py`：终端入口：交互 / 单条指令 / 只跑后台巡检（指令在后台线程里跑，后台巡检由 `workflows.Scheduler` 调度）
- `run.sh`：建 `.venv`（Python 3.10+）、装依赖，然后运行 `cli.py`
- `agentstate.py`：Ami 自己的状态（账号、运行状态、工作流、工具、权限），存在 `~/.computer-use/agent_state.json`
- `checkup.py`：自检（中控每天派 `ami.check` 任务时跑）：各平台的登录状态（命令行工具的 status、浏览器里打开要登录的页面）
- `amients.py`：把每一步的状态上报给 amients.com（本地待发队列 + 后台发送），以及去它的任务队列领任务、回报结果
- `agent.py`：computer use 循环（发送任务 → 执行 `computer_call.actions` → 回传 `computer_call_output` 截图）
- `axui.py`：读取前台应用的 AX 控件表，按控件点击 / 输入 / 滚动
- `jev.py`：Jev 客户端和快速模式循环
- `workflows.py`：常驻工作流（目标、调度、消息巡检）
- `menubar.py`：盯右上角菜单栏聊天软件图标，变了让模型判断是不是新消息
- `bench_models.py`：模型对比（第一步决策 `decide`、重新打分 `regrade`）
- `tools.py`：函数工具注册表（微信、浏览器、各网站、各接口按同一套接口接入）
- `wechat.py`：微信工具（搜索打开聊天、OCR 核对后发送、OCR 读消息），也可以命令行调试
- `wechat_friend.py`：微信加好友、列出 / 同意好友申请
- `lark.py`：Lark / 飞书客户端工具（按 DOM class 名读会话列表和消息、核对后发送），也可以命令行调试
- `memes.py`：梗图库、让 Jev 挑梗图、从 ChineseBQB 下载
- `msgstore.py`：本地消息记录（读到的和发出去的消息存进 SQLite，`message_history` 读回）
- `SOUL.md`：助理人设
- `browser.py`：浏览器工具（打开网页、读页面文字和链接、加载更多）
- `sites.py`：在浏览器里读写的网站配置（链接格式、导航噪音、操作说明）
- `twitter.py` / `linkedin.py`：X 刷推荐 / 搜索、LinkedIn 搜索（浏览器里按帖子 / 结果卡片整理）
- `rednote.py`：rednote 的操作规则，`rednote_feed` 刷推荐流 / 搜索，`rednote_publish` 发纯文字笔记
- `exa.py` / `rss.py`：网页搜索（Exa）、RSS / Atom 订阅源
- `papers.py`：Hugging Face 每日论文 / 论文搜索 / 热门模型，arXiv 论文详情和最新论文
- `hn.py` / `v2ex.py` / `github.py`：Hacker News、V2EX、GitHub（gh）接口
- `discord.py` / `slack.py` / `telegram.py` / `feishu.py`：机器人接口读写消息、飞书文档
- `ocr.py`：屏幕区域文字识别（macOS Vision，本地）
- `action_cache.py` / `memory.py` / `report.py`：动作缓存、跨任务经验、运行报告
- `paths.py`：本地状态目录、项目里的资源文件路径、读 `.env`
- `tests/`：离线测试，不联网、不操作屏幕（`.venv/bin/python -m unittest discover -v tests`）
- `computer.py`：macOS 截图（mss）和动作执行（pyautogui；中文等非 ASCII 文本通过剪贴板粘贴）

## 本地消息记录

读到的和发出去的每条消息（Lark、微信、飞书机器人、Slack、Discord、Telegram）都会存进 `~/.computer-use/messages.db`
（SQLite），以后不打开应用也能查，屏幕上已经滚走的消息也在：

- 在 `tools.call` 里统一记录：读消息的工具返回后存下读到的，发消息的工具发送成功后存下发出的。存储出错不影响工具本身。
- 屏幕上读到的（Lark、微信）没有消息 id，每次读到的是最近一屏：和库里这个聊天的最后几条对齐，只追加新出现的部分；
  同样内容连发几条也能分清。时间分隔（「昨天」「00:32」）记在紧跟着的那条上，后面的记成「00:32 之后」，并记下读到的日期。
- 接口读到的按消息 id / 时间去重；发出去的消息之后再读到时会对上，不重复。发出去的消息有准确时间，Lark 的梗图记成「[图片]」并带上梗图名。
- 读回用 `message_history(app, chat, query, limit)`：都空时列出有记录的聊天，给聊天名看这个聊天的记录，给 query 按内容搜。
  app 写「飞书」指 Lark 客户端，「飞书机器人」指开放平台接口。

消息原文以明文存在本机。不想存就设 `COMPUTER_USE_MESSAGES=off`，删掉 `messages.db` 就清空。

```bash
.venv/bin/python msgstore.py                    # 有记录的聊天
.venv/bin/python msgstore.py "Hong Chen" 50     # 某个聊天最近 50 条
.venv/bin/python msgstore.py --search 开会       # 按内容搜
```

## 本地数据

缓存、经验、消息记录（`messages.db`，见上）、运行报告都在 `~/.computer-use/`，只在本机，可以随时删除；
用 `COMPUTER_USE_HOME` 可以换位置，`COMPUTER_USE_MEMORY=off` 关闭经验记忆，`COMPUTER_USE_MESSAGES=off` 不存消息。

## 注意

它操作的是你真实的电脑。模型可能误点，而且所有操作（付款、发消息、删除）都不会先问你、直接执行，建议先用不重要的任务试。
截图会发送给 OpenAI。截图默认缩到 0.7（上传量约减半、回复更快），可用 `IMAGE_SCALE` 调整：调小更快更省，但点击精度下降。
浏览器打开网页辅助功能后会变慢一些，所以只在读网页时才打开；如果觉得 Chrome 不跟手，重启 Chrome 即可恢复。
