# AMI 中控

Agent 中台：给外部 agent 发 API key，agent 用 key 把状态和事件推上来，在一个后台里集中看。还带一个任务队列（用 key push 任务、同分组的 key pull 领取）和多用户账号。代码在 `hub/`，只用 Python 标准库 + SQLite，没有依赖。

- `hub/server.py`：服务端（API + 后台页面）
- `hub/dashboard.html`：后台页面
- `hub/ami_client.py`：Python 上报客户端
- `status_page.html`：小暖桌面 agent 的本机状态页（独立的，不依赖中控）

## 启动

```bash
AMI_ADMIN_USER=admin AMI_ADMIN_PASSWORD=换成你自己的 python3 hub/server.py --host 127.0.0.1 --port 8787
```

- 打开 `http://<host>:8787`，用上面的用户名和密码登录控制台（session cookie，30 天有效；同一 IP 5 分钟内最多输错 5 次）。
- 环境变量里的账号是**内置管理员**：第一次启动时创建，之后每次启动按环境变量同步它的密码（所以它的密码只能改环境变量）。
- 其他人的账号由管理员在「用户」页添加。普通用户只看到自己的 key、动态和任务；管理员看全部，也能重置密码、设管理员、停用账号（停用后该用户名下的 key 立即失效）。用户自己可以在左下角改密码，密码用 PBKDF2 存储。
- 升级到多用户版本时，原来的 key 自动归到内置管理员名下；原来的登录会失效，重新登录一次即可。
- 在左侧「API Keys」页创建 key 交给 agent，之后随时可以在控制台查看、复制、换 key 或吊销。key 原文存在 `hub/data/hub.db`，注意保护这个文件。
- 登录 cookie 带 `Secure`，所以对外必须走 HTTPS（本机 127.0.0.1 / localhost 调试不受影响）。

### 线上部署（amients.com）

- 服务：systemd `ami-hub`，监听 `127.0.0.1:8740`，数据在 `hub/data/`
- 账号：`/etc/ami/env`（`AMI_ADMIN_USER` / `AMI_ADMIN_PASSWORD`），改完 `sudo systemctl restart ami-hub`
- HTTPS：Caddy（`/etc/caddy/Caddyfile` 里的 `amients.com` 段）反代过去，证书自动续期
- 改代码后：`sudo systemctl restart ami-hub`

## agent 怎么上报

所有请求带 `Authorization: Bearer <api key>`，body 是 JSON。

| 接口 | 作用 |
|---|---|
| `PUT /api/v1/status` | 整份替换当前状态。约定 `state`（running / idle / warn / error…）和 `summary`（一句话），其余字段随意，后台会原样展示 |
| `PATCH /api/v1/status` | 只合并给出的字段 |
| `POST /api/v1/events` | 发事件：`{kind, text, level, data, ts}`，也可以发数组或 `{"events": [...]}`，一次最多 200 条。`level` 为 info / warn / error |
| `POST /api/v1/heartbeat` | 只刷新在线时间 |
| `GET /api/v1/whoami` | 检查 key 是否有效 |
| `PUT /api/v1/checks` | 声明自检项 `{"checks": ["Lark 登录", ...]}`，见下面「每日自检」 |
| `PUT /api/v1/capabilities` | 注册能力（整份替换），见下面「能力注册」 |
| `GET /api/v1/capabilities` | 查同一个用户名下的 agent 都注册了哪些能力 |

任何上报都会刷新「最近上报」时间，120 秒内没有上报就显示为离线。每个 agent 保留最近 2000 条事件。

Python agent 可以直接拷 `hub/ami_client.py`：

```python
from ami_client import Ami
ami = Ami("http://127.0.0.1:8787", "ami_xxx")
ami.status(state="running", summary="正在巡检 Lark 消息", workflows=[...])
ami.event("held", "扣下：给李四的回复", level="warn", why="涉及报价")
```

中控连不上时客户端只返回 `False`，不会让 agent 自己挂掉。

## 任务队列

**队列 = 用户 + 分组。** 创建 key 时可以填一个分组（小写字母、数字、`-`），key 会带上前缀，如 `ami_translate_xxxx`；分组创建后不能改。

- 用某个 key 提交（push）的任务，进这个 key 所在分组的队列；**不分组**的 key 提交的任务进这个用户的**公共池**。
- agent 领任务（pull）时，领自己分组的队列和公共池里的任务；不分组的 agent 只领公共池。
- 不同用户的队列完全隔开，同名分组也不会串。
- 控制台「任务」页可以看各队列、全部任务，手动派任务到任意队列（管理员可以派到别人的队列），取消和重新派发。

**不会被抢：** 领任务时「查出排队中的任务」和「标成执行中」在同一把锁、同一个事务里完成，一条任务同一时间只给一个 agent。每次领取会发一个 `token`，回报（progress / complete / fail）必须带上；任务超时被收回、重新派发后，之前那次领取的 token 作废，晚到的回报会拿到 409。

| 接口 | 作用 |
|---|---|
| `POST /api/v1/tasks` | 发任务到自己分组的队列：`{kind, payload}`，可选 `priority`（-100~100，大的先领）、`max_attempts`（默认 3）、`timeout`（每次执行的时限秒数，默认 600）、`expires_in`（排队多少秒没人领就过期）、`dedupe_key`（同一个队列里同一个值只建一次，重复提交返回原任务） |
| `GET /api/v1/tasks/{id}?wait=30` | 查任务；带 `wait` 时挂着直到任务结束（最多 30 秒）。能查自己发的、领过的、以及自己能领的队列里的任务 |
| `POST /api/v1/tasks/{id}/cancel` | 取消自己发的任务（排队中或执行中） |
| `GET /api/v1/tasks?box=sent\|worked&status=&q=&before=&limit=` | 列出自己发的 / 领过的任务 |
| `POST /api/v1/tasks/pull` | 领任务：`{wait, limit, kinds}`。有任务立刻返回（每个带 `token`），没有就最多挂 `wait` 秒（上限 30）后返回空列表 |
| `POST /api/v1/tasks/{id}/progress` | 报进度 `{token, progress, extend}`，同时把时限往后延 |
| `POST /api/v1/tasks/{id}/complete` | 回报结果 `{token, result}` |
| `POST /api/v1/tasks/{id}/fail` | 回报失败 `{token, error, retry}`；`retry: true` 且还有次数时重新排队 |

状态：`queued`（排队中）→ `running`（被领走）→ `done` / `failed`；另有 `canceled`、`expired`。领走后超过 `timeout` 没回报的任务会被自动收回重新排队，次数用完算失败。投递语义是“至少一次”（超时重派时同一任务可能被执行不止一次），处理最好能重复执行。每个队列最多 10000 条排队，结束超过 30 天的任务会被清理。

```python
ami.push("translate", {"text": "hello"})                       # 提交到这个 key 的分组队列
ami.work(lambda t: {"text": translate(t["payload"]["text"])})  # 同组的 key：一直领、做、回报（token 自动带上）
```

## 每日自检

中控每天定时给 agent 派一条 `kind` 为 `ami.check` 的任务（`target` 是这个 agent，同组的其它 agent 领不到）。agent 把各项功能真的试一遍——登录态还在不在、API key 还有没有余额、网络通不通——用 `complete` 回报：

```json
{"token": "…", "result": {"checks": [
  {"name": "Lark 登录", "ok": true},
  {"name": "OpenAI 余额", "ok": false, "kind": "quota", "message": "insufficient_quota", "hint": "去 OpenAI 后台充值"}
]}}
```

`kind` 是没通过的原因：`auth`（登录 / 授权失效）、`quota`（余额 / 额度不足）、`network`、`config`、`other`；`hint` 是给人看的处理办法。

- **开启**：agent 调 `PUT /api/v1/checks` 声明检查项后自动开启（默认每天 09:00），并马上做第一次。时区用环境变量 `AMI_TZ`，默认 `Asia/Shanghai`。控制台的 agent 详情里可以改时间、关闭、「立即自检」；没声明检查项的 agent 也能手动打开，只确认它还能响应。
- **算没通过**：有任何一项 `ok` 不是 `true`；30 分钟内没领走（离线或没在领任务）；领走后 5 分钟内没回报；回报的不是上面的格式。
- **提醒**：概览顶部和 agent 卡片变红并写明哪一项、什么原因、怎么处理；动态里记一条错误；同时推送到用户设置的通知地址（控制台左下角铃铛）。没通过每次都推，恢复时推一次，一直正常不打扰。
- **通知地址**：飞书 / Lark、钉钉、企业微信群机器人按各自格式发，其它地址（Slack、Discord、自建服务）收到 `{"text", "content", "event", "agent", "health"}`。只允许公网 http(s) 地址。

Python 客户端：

```python
from ami_client import Ami, CheckFailed

@ami.check("Lark 登录")
def lark_login():
    if not lark.get_chats(limit=1):
        raise CheckFailed("Lark 登录已过期", kind="auth", hint="在家里的 Mac 上重新扫码登录")

@ami.check("OpenAI 余额")
def openai_quota():
    client.models.list()    # 普通异常按状态码和报错内容自动归类：401/403 → auth，402/quota/余额 → quota，超时/连不上 → network

ami.work(handle)            # 用 work() 领任务的：自检任务自动处理，不会交给 handle
ami.serve_checks()          # 不领其它任务的：开个后台线程只领自检任务
```

## 能力注册

agent 把自己能处理的任务类型报给中控：控制台的「能力」页列出每种能力、谁能做、在不在线，点「派任务」就选好队列、填好类型和示例 payload；agent 详情和卡片上也会列出来。

```json
PUT /api/v1/capabilities
{"capabilities": [
  {"kind": "translate", "title": "中译英", "description": "把中文翻译成英文，保留原有格式",
   "input": {"text": "要翻译的文字"}, "output": {"text": "译文"}, "example": {"text": "你好"}}
]}
```

- `kind` 必填，就是任务的 `kind`（1-64 字，不能以 `ami.` 开头，不能重复）；也可以直接写字符串 `"translate"`。`title` ≤ 80 字、`description` ≤ 2000 字；`input` / `output`（字段说明或 JSON Schema）和 `example`（示例 payload）是任意 JSON，每项 ≤ 10000 字。每个 agent 最多 50 项。
- 整份替换，传 `[]` 就是全部撤掉。内容有变化时动态里记一条（新增 / 去掉了哪些），没变化的重复注册不记。吊销的 key 的能力不再列出。
- 能力只是说明，不改变派单规则：任务还是按队列（分组）领。派任务弹窗里会提示选中的队列有没有 agent 注册了这个 kind。
- `GET /api/v1/capabilities` 返回同一个用户名下的能力，按 `kind` 合并：`providers` 是注册了它的 agent（`id`、`name`、`group`、`online`）；`reachable` 表示用当前 key 提交这个 kind 的任务，有注册了它的 agent 能领到（当前 key 不分组，或者和它同组）。

Python 客户端：

```python
@ami.capability("translate", "把中文翻译成英文", title="中译英",
                input={"text": "要翻译的文字"}, output={"text": "译文"}, example={"text": "你好"})
def translate(task):
    return {"text": do_translate(task["payload"]["text"])}

ami.work()                    # 启动时注册能力，只领注册过的 kind，按 kind 分给对应的函数；也可以 work(handle) 兜底处理其它 kind
ami.find("translate")         # 查谁能做 translate、用自己的 key 发过去能不能被领到；不给 kind 返回全部
ami.declare_capabilities()    # 不用 work() 的，启动时自己调一次
```

## 管理接口

控制台用的接口，需要先 `POST /api/login {"username","password"}` 拿到 cookie：`GET /api/me`、`POST /api/logout`、`GET/POST /api/agents`、`PATCH /api/agents/{id}`（改名 `name`、每日自检时间 `check_at`：`HH:MM`，空表示关闭）、`POST /api/agents/{id}/check`（立即自检）、`PUT /api/me/notify {url}`（通知地址）、`POST /api/me/notify/test`、`POST /api/agents/{id}/rotate`、`DELETE /api/agents/{id}`（吊销）、`GET /api/agents/{id}/events?before=&limit=`、`GET /api/events`（全部 agent 的动态，可加 `agent=`）、`GET /api/stream`（SSE，只推自己能看的）、`GET /api/queues`、`GET/POST /api/tasks`（可筛 `owner=&group=&agent=&status=&q=`）、`GET /api/tasks/stats`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/cancel`、`POST /api/tasks/{id}/retry`、`POST /api/me/password`；管理员另有 `GET/POST /api/users`、`PATCH /api/users/{id}`（`password` / `is_admin` / `disabled`）。普通用户调这些接口只会拿到自己名下的数据。两个动态接口都支持 `level=warn,error`（按级别筛）、`q=`（搜内容和类型）、`counts=1`（附带最近 24 小时各级别条数）。
