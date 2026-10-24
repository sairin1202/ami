#!/usr/bin/env python3
"""AMI 中控：给外部 agent 发 API key，agent 用 key 上报状态和事件，这里集中看。

任务队列：key 可以带分组（ami_<分组>_xxx）。用某个 key 提交的任务进它所在分组的队列，同组的 key 来领；
不分组的 key 提交的任务进公共池，同一个用户的所有 agent 都能领。每条任务同一时间只会被一个 agent 领走。

多用户：管理员在控制台建账号，每个用户只看到自己的 key、动态和任务，管理员看全部。

只用标准库。启动：
    AMI_ADMIN_USER=admin AMI_ADMIN_PASSWORD=换成你自己的 python3 hub/server.py --port 8787
环境变量里的账号是内置管理员，每次启动按环境变量同步它的密码；其他账号在控制台里管理。
"""
import argparse
import collections
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
MAX_BODY = 1 << 20        # 单次请求体上限 1MB
EVENTS_KEPT = 2000        # 每个 agent 保留的事件条数
ONLINE_WINDOW = 120       # 多少秒内有上报算在线
LEVELS = ("info", "warn", "error")
SESSION_DAYS = 30
SESSION_COOKIE = "ami_session"
LOGIN_FAILS = 5           # 同一 IP 每 LOGIN_WINDOW 秒最多失败几次
LOGIN_WINDOW = 300
PBKDF2_ITERS = 200_000
MIN_PASSWORD = 8
GROUP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.@\-一-鿿]{2,32}")
TASK_STATUSES = ("queued", "running", "done", "failed", "canceled", "expired")
TASK_FINAL = ("done", "failed", "canceled", "expired")
TASK_QUEUE_MAX = 10000    # 每个队列最多排队多少条
TASK_KEPT_DAYS = 30       # 结束的任务保留多少天
MAX_WAIT = 30             # 长轮询最多挂多少秒

SCHEMA = """
create table if not exists users (
  id          integer primary key,
  username    text not null unique,
  pw_hash     text not null,
  is_admin    integer not null default 0,
  created_at  real not null,
  disabled_at real
);
create table if not exists agents (
  id          integer primary key,
  name        text not null,
  key_hash    text not null unique,
  key_prefix  text not null,
  api_key     text,
  created_at  real not null,
  revoked_at  real,
  last_seen   real,
  status      text,
  owner_id    integer references users(id),
  grp         text not null default ''          -- 分组，'' 表示不分组
);
create table if not exists events (
  id        integer primary key,
  agent_id  integer not null references agents(id),
  ts        real not null,
  kind      text not null,
  level     text not null default 'info',
  text      text not null default '',
  data      text
);
create index if not exists events_agent on events(agent_id, id);
create table if not exists sessions (
  token_hash  text primary key,
  user_id     integer not null references users(id),
  expires_at  real not null
);
create table if not exists tasks (
  id            integer primary key,
  owner_id      integer not null references users(id),
  grp           text not null default '',        -- 所在队列；'' 是公共池
  from_agent    integer references agents(id),   -- null：控制台发的
  worker        integer references agents(id),   -- 最近一次领走它的 agent
  claim_token   text,                            -- 每次领取发一个，回报时必须带上
  kind          text not null,
  payload       text,
  status        text not null default 'queued',
  priority      integer not null default 0,
  attempts      integer not null default 0,
  max_attempts  integer not null default 3,
  timeout       integer not null default 600,    -- 每次领取后多少秒内要回报，否则收回重派
  lease_until   real,
  expires_at    real,                            -- 排队超过这个时间还没人领就过期
  dedupe_key    text,
  progress      text,
  result        text,
  error         text,
  created_at    real not null,
  started_at    real,
  finished_at   real,
  updated_at    real not null
);
create index if not exists tasks_queue on tasks(owner_id, grp, status, priority desc, id);
create index if not exists tasks_from on tasks(from_agent, id);
create index if not exists tasks_worker on tasks(worker, id);
create index if not exists tasks_status on tasks(status, lease_until);
create unique index if not exists tasks_dedupe on tasks(owner_id, grp, dedupe_key) where dedupe_key is not null;
"""
INDEXES = """
create index if not exists agents_owner on agents(owner_id, grp);
"""


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(grp=""):
    key = f"ami_{grp}_" if grp else "ami_"
    return key + secrets.token_urlsafe(32), len(key) + 6   # key，以及展示时露出的前缀长度


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2${PBKDF2_ITERS}${salt.hex()}${h.hex()}"


def check_password(password, stored):
    try:
        _, iters, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, AttributeError):
        return False


DUMMY_HASH = hash_password(secrets.token_hex(8))   # 用户不存在时也算一次，不让响应时间泄露用户名是否存在


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


AGENT_SELECT = "select a.*, u.username owner_name from agents a left join users u on u.id=a.owner_id"
TASK_SELECT = ("select t.*, fa.name from_name, wa.name worker_name, u.username owner_name from tasks t "
               "left join agents fa on fa.id=t.from_agent left join agents wa on wa.id=t.worker "
               "left join users u on u.id=t.owner_id")
loads = lambda v: json.loads(v) if v is not None else None
dumps = lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        # 旧版 sessions 按用户名记，换成 user_id；旧 session 作废，重新登录一次即可
        if "username" in self._cols("sessions"):
            self.db.execute("drop table sessions")
        self.db.executescript(SCHEMA)
        cols = self._cols("agents")
        if "api_key" not in cols:   # 旧库只存了哈希，这些 key 换一次才能在控制台里看到
            self.db.execute("alter table agents add column api_key text")
        if "owner_id" not in cols:  # 多用户之前的 key，启动时归给内置管理员（见 ensure_admin）
            self.db.execute("alter table agents add column owner_id integer references users(id)")
        if "grp" not in cols:
            self.db.execute("alter table agents add column grp text not null default ''")
        self.db.executescript(INDEXES)
        self.db.commit()
        self.lock = threading.Lock()

    def _cols(self, table):
        return {r["name"] for r in self.db.execute(f"pragma table_info({table})")}

    # ---- 用户 ----
    def _user(self, r):
        return {"id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"]),
                "disabled": r["disabled_at"] is not None, "created_at": r["created_at"]}

    def ensure_admin(self, username, password):
        """内置管理员：没有就建，有就把密码同步成环境变量里的；没有主人的 key 都归它。"""
        now = time.time()
        with self.lock, self.db:
            r = self.db.execute("select * from users where username=?", (username,)).fetchone()
            if r is None:
                uid = self.db.execute("insert into users(username, pw_hash, is_admin, created_at) values(?,?,1,?)",
                                      (username, hash_password(password), now)).lastrowid
            else:
                uid = r["id"]
                pw = r["pw_hash"] if check_password(password, r["pw_hash"]) else hash_password(password)
                self.db.execute("update users set pw_hash=?, is_admin=1, disabled_at=null where id=?", (pw, uid))
            self.db.execute("update agents set owner_id=? where owner_id is null", (uid,))
        return uid

    def user_count(self):
        with self.lock:
            return self.db.execute("select count(*) n from users").fetchone()["n"]

    def check_login(self, username, password):
        with self.lock:
            r = self.db.execute("select * from users where username=?", (username,)).fetchone()
        ok = check_password(password, r["pw_hash"] if r else DUMMY_HASH)
        return self._user(r) if r and ok and r["disabled_at"] is None else None

    def get_user(self, user_id):
        with self.lock:
            r = self.db.execute("select * from users where id=?", (user_id,)).fetchone()
        return self._user(r) if r else None

    def users(self):
        with self.lock:
            rows = self.db.execute(
                "select u.*, (select count(*) from agents a where a.owner_id=u.id and a.revoked_at is null) agents "
                "from users u order by u.disabled_at is not null, u.id").fetchall()
        return [{**self._user(r), "agents": r["agents"]} for r in rows]

    def create_user(self, username, password, is_admin):
        with self.lock, self.db:
            if self.db.execute("select 1 from users where username=?", (username,)).fetchone():
                raise ApiError(409, "这个用户名已经有了")
            uid = self.db.execute("insert into users(username, pw_hash, is_admin, created_at) values(?,?,?,?)",
                                  (username, hash_password(password), int(is_admin), time.time())).lastrowid
        return self.get_user(uid)

    def update_user(self, user_id, password=None, is_admin=None, disabled=None):
        with self.lock, self.db:
            if password is not None:
                self.db.execute("update users set pw_hash=? where id=?", (hash_password(password), user_id))
            if is_admin is not None:
                self.db.execute("update users set is_admin=? where id=?", (int(is_admin), user_id))
            if disabled is not None:
                self.db.execute("update users set disabled_at=? where id=?", (time.time() if disabled else None, user_id))
            if password is not None or disabled:   # 改密码 / 停用后，已登录的地方都要重新登录
                self.db.execute("delete from sessions where user_id=?", (user_id,))
        return self.get_user(user_id)

    def check_password_of(self, user_id, password):
        with self.lock:
            r = self.db.execute("select pw_hash from users where id=?", (user_id,)).fetchone()
        return bool(r) and check_password(password, r["pw_hash"])

    def new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock, self.db:
            self.db.execute("delete from sessions where expires_at < ?", (now,))
            self.db.execute("insert into sessions values(?,?,?)", (hash_key(token), user_id, now + SESSION_DAYS * 86400))
        return token

    def session_user(self, token):
        with self.lock:
            r = self.db.execute("select u.* from sessions s join users u on u.id=s.user_id "
                                "where s.token_hash=? and s.expires_at > ? and u.disabled_at is null",
                                (hash_key(token), time.time())).fetchone()
        return self._user(r) if r else None

    def end_session(self, token):
        with self.lock, self.db:
            self.db.execute("delete from sessions where token_hash=?", (hash_key(token),))

    # ---- agent ----
    def _agent(self, row):
        if row is None:
            return None
        return {
            "id": row["id"], "name": row["name"], "key_prefix": row["key_prefix"], "api_key": row["api_key"],
            "group": row["grp"], "owner_id": row["owner_id"], "owner_name": row["owner_name"],
            "created_at": row["created_at"], "revoked": row["revoked_at"] is not None,
            "last_seen": row["last_seen"],
            "status": json.loads(row["status"]) if row["status"] else None,
        }

    def get(self, agent_id):
        with self.lock:
            return self._agent(self.db.execute(AGENT_SELECT + " where a.id=?", (agent_id,)).fetchone())

    def list(self, owner=None):
        sql, args = AGENT_SELECT, []
        if owner is not None:
            sql += " where a.owner_id=?"
            args.append(owner)
        with self.lock:
            rows = self.db.execute(sql + " order by a.revoked_at is not null, a.id", args).fetchall()
        return [self._agent(r) for r in rows]

    def create(self, name, owner_id, grp):
        key, n = new_key(grp)
        with self.lock, self.db:
            cur = self.db.execute(
                "insert into agents(name, key_hash, key_prefix, api_key, created_at, owner_id, grp) values(?,?,?,?,?,?,?)",
                (name, hash_key(key), key[:n], key, time.time(), owner_id, grp))
        return cur.lastrowid, key

    def rotate(self, agent_id):
        with self.lock, self.db:
            r = self.db.execute("select grp from agents where id=? and revoked_at is null", (agent_id,)).fetchone()
            if not r:
                return None
            key, n = new_key(r["grp"])
            self.db.execute("update agents set key_hash=?, key_prefix=?, api_key=? where id=?",
                            (hash_key(key), key[:n], key, agent_id))
        return key

    def rename(self, agent_id, name):
        with self.lock, self.db:
            return self.db.execute("update agents set name=? where id=?", (name, agent_id)).rowcount > 0

    def revoke(self, agent_id):
        with self.lock, self.db:
            return self.db.execute(
                "update agents set revoked_at=? where id=? and revoked_at is null",
                (time.time(), agent_id)).rowcount > 0

    def by_key(self, key):
        """返回 {id, owner_id, grp}；key 已吊销或主人被停用时返回 None。"""
        with self.lock:
            row = self.db.execute(
                "select a.id, a.owner_id, a.grp from agents a join users u on u.id=a.owner_id "
                "where a.key_hash=? and a.revoked_at is null and u.disabled_at is null", (hash_key(key),)).fetchone()
        return dict(row) if row else None

    def touch(self, agent_id, status=None, merge=False):
        with self.lock, self.db:
            if status is None:
                self.db.execute("update agents set last_seen=? where id=?", (time.time(), agent_id))
                return
            if merge:
                old = self.db.execute("select status from agents where id=?", (agent_id,)).fetchone()["status"]
                status = {**(json.loads(old) if old else {}), **status}
            self.db.execute("update agents set last_seen=?, status=? where id=?",
                            (time.time(), json.dumps(status, ensure_ascii=False), agent_id))

    def add_events(self, agent_id, events):
        out = []
        with self.lock, self.db:
            now = time.time()
            for e in events:
                cur = self.db.execute(
                    "insert into events(agent_id, ts, kind, level, text, data) values(?,?,?,?,?,?)",
                    (agent_id, e["ts"], e["kind"], e["level"], e["text"],
                     json.dumps(e["data"], ensure_ascii=False) if e["data"] is not None else None))
                out.append({"id": cur.lastrowid, "agent_id": agent_id, **e})
            self.db.execute("update agents set last_seen=? where id=?", (now, agent_id))
            self.db.execute(
                "delete from events where agent_id=? and id <= ("
                " select id from events where agent_id=? order by id desc limit 1 offset ?)",
                (agent_id, agent_id, EVENTS_KEPT))
        return out

    def events(self, agent_id=None, before=None, limit=100, levels=None, q=None, owner=None):
        where, args = ["1=1"], []
        if agent_id is not None:
            where.append("agent_id=?")
            args.append(agent_id)
        if owner is not None:
            where.append("agent_id in (select id from agents where owner_id=?)")
            args.append(owner)
        if before:
            where.append("id < ?")
            args.append(before)
        if levels:
            where.append(f"level in ({','.join('?' * len(levels))})")
            args += levels
        if q:
            where.append("(instr(lower(text), lower(?)) > 0 or instr(lower(kind), lower(?)) > 0)")
            args += [q, q]
        sql = f"select * from events where {' and '.join(where)} order by id desc limit ?"
        with self.lock:
            rows = self.db.execute(sql, args + [limit]).fetchall()
        return [{"id": r["id"], "agent_id": r["agent_id"], "ts": r["ts"], "kind": r["kind"], "level": r["level"], "text": r["text"],
                 "data": json.loads(r["data"]) if r["data"] else None} for r in rows]

    def event_counts(self, agent_id=None, since=0, owner=None):
        """最近一段时间各级别的动态条数，给动态页顶部的统计用。"""
        sql, args = "select level, count(*) n from events where ts >= ?", [since]
        if agent_id is not None:
            sql += " and agent_id=?"
            args.append(agent_id)
        if owner is not None:
            sql += " and agent_id in (select id from agents where owner_id=?)"
            args.append(owner)
        with self.lock:
            rows = self.db.execute(sql + " group by level", args).fetchall()
        counts = {r["level"]: r["n"] for r in rows}
        return {"total": sum(counts.values()), "warn": counts.get("warn", 0), "error": counts.get("error", 0)}

    # ---- 任务队列 ----
    def _task(self, r, full=True):
        t = {k: r[k] for k in ("id", "kind", "status", "priority", "owner_id", "owner_name", "from_agent", "from_name",
                               "worker", "worker_name", "attempts", "max_attempts", "timeout", "lease_until",
                               "expires_at", "dedupe_key", "error", "created_at", "started_at", "finished_at", "updated_at")}
        t["group"] = r["grp"]
        t["progress"] = loads(r["progress"])
        if full:
            t["payload"], t["result"] = loads(r["payload"]), loads(r["result"])
        return t

    def _task_row(self, task_id):
        return self.db.execute(TASK_SELECT + " where t.id=?", (task_id,)).fetchone()

    def get_task(self, task_id, full=True):
        with self.lock:
            r = self._task_row(task_id)
        return self._task(r, full) if r else None

    def push_task(self, owner_id, grp, from_agent, t):
        """返回 (任务, 是否新建)。同一个队列里 dedupe_key 已经有了就直接返回原来那条。"""
        now = time.time()
        with self.lock, self.db:
            if t["dedupe_key"]:
                r = self.db.execute("select id from tasks where owner_id=? and grp=? and dedupe_key=?",
                                    (owner_id, grp, t["dedupe_key"])).fetchone()
                if r:
                    return self._task(self._task_row(r["id"])), False
            n = self.db.execute("select count(*) n from tasks where owner_id=? and grp=? and status='queued'",
                                (owner_id, grp)).fetchone()["n"]
            if n >= TASK_QUEUE_MAX:
                raise ApiError(429, f"这个队列已经有 {n} 条任务在排队，先等它消化一些")
            cur = self.db.execute(
                "insert into tasks(owner_id, grp, from_agent, kind, payload, priority, max_attempts, timeout, expires_at,"
                " dedupe_key, created_at, updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, grp, from_agent, t["kind"], dumps(t["payload"]), t["priority"], t["max_attempts"], t["timeout"],
                 now + t["expires_in"] if t["expires_in"] else None, t["dedupe_key"], now, now))
            return self._task(self._task_row(cur.lastrowid)), True

    def claim_tasks(self, agent, limit, kinds=None):
        """agent 领自己分组的队列和公共池里的任务。查和改在同一把锁、同一个事务里，一条任务只会给一个 agent。"""
        now = time.time()
        sql = ("select id from tasks where owner_id=? and grp in (?, '') and status='queued'"
               " and (expires_at is null or expires_at > ?)")
        args = [agent["owner_id"], agent["grp"], now]
        if kinds:
            sql += f" and kind in ({','.join('?' * len(kinds))})"
            args += kinds
        out = []
        with self.lock, self.db:
            ids = [r["id"] for r in self.db.execute(sql + " order by priority desc, id limit ?", args + [limit])]
            for i in ids:
                token = secrets.token_urlsafe(16)
                self.db.execute("update tasks set status='running', worker=?, claim_token=?, attempts=attempts+1, "
                                "started_at=?, lease_until=?+timeout, updated_at=? where id=?",
                                (agent["id"], token, now, now, now, i))
                out.append({**self._task(self._task_row(i)), "token": token})
        return out

    def _claimed(self, task_id, agent_id, token):
        """确认这次回报来自当前持有任务的那次领取。"""
        if not isinstance(token, str) or not token:
            raise ApiError(400, "缺少 token：pull 返回的每个任务里都有，回报时原样带上")
        r = self._task_row(task_id)
        if not r or r["worker"] != agent_id:
            raise ApiError(404 if not r else 409, "没有这个任务" if not r else "这个任务现在不在你手上")
        if r["status"] == "running" and hmac.compare_digest(r["claim_token"] or "", token):
            return r
        if r["status"] == "queued":
            raise ApiError(409, "这次领取已超时，任务已收回等待重新派发")
        if r["status"] == "running":
            raise ApiError(409, "这次领取已失效，任务已经被重新领走")
        raise ApiError(409, f"任务已经是 {r['status']}，不能再回报")

    def finish_task(self, task_id, agent_id, token, ok, result=None, error=None, retry=False):
        now = time.time()
        with self.lock, self.db:
            r = self._claimed(task_id, agent_id, token)
            if ok:
                self.db.execute("update tasks set status='done', result=?, error=null, claim_token=null, lease_until=null, "
                                "finished_at=?, updated_at=? where id=?", (dumps(result), now, now, task_id))
            elif retry and r["attempts"] < r["max_attempts"]:
                self.db.execute("update tasks set status='queued', error=?, claim_token=null, lease_until=null, updated_at=? "
                                "where id=?", (error, now, task_id))
            else:
                self.db.execute("update tasks set status='failed', error=?, result=?, claim_token=null, lease_until=null, "
                                "finished_at=?, updated_at=? where id=?", (error, dumps(result), now, now, task_id))
            return self._task(self._task_row(task_id))

    def task_progress(self, task_id, agent_id, token, progress, extend):
        now = time.time()
        with self.lock, self.db:
            r = self._claimed(task_id, agent_id, token)
            if progress is not None:
                old = loads(r["progress"])
                progress = {**old, **progress} if isinstance(old, dict) and isinstance(progress, dict) else progress
            else:
                progress = loads(r["progress"])
            self.db.execute("update tasks set progress=?, lease_until=?, updated_at=? where id=?",
                            (dumps(progress), now + (extend or r["timeout"]), now, task_id))
            return self._task(self._task_row(task_id))

    def cancel_task(self, task_id, by_agent=None, owner=None):
        """by_agent：agent 只能取消自己发的；控制台用 owner 限定范围（管理员为 None）。执行方再回报会拿到 409。"""
        now = time.time()
        with self.lock, self.db:
            r = self._task_row(task_id)
            if not r or by_agent is not None and r["from_agent"] != by_agent or owner is not None and r["owner_id"] != owner:
                raise ApiError(404, "没有这个任务，或不是你发的")
            if r["status"] in TASK_FINAL:
                raise ApiError(409, f"任务已经是 {r['status']}，不用取消")
            self.db.execute("update tasks set status='canceled', claim_token=null, lease_until=null, finished_at=?, "
                            "updated_at=? where id=?", (now, now, task_id))
            return self._task(self._task_row(task_id))

    def retry_task(self, task_id, owner=None):
        now = time.time()
        with self.lock, self.db:
            r = self._task_row(task_id)
            if not r or owner is not None and r["owner_id"] != owner:
                raise ApiError(404, "没有这个任务")
            if r["status"] not in ("failed", "canceled", "expired"):
                raise ApiError(409, "只有失败、已取消、已过期的任务能重新派发")
            self.db.execute("update tasks set status='queued', attempts=0, worker=null, claim_token=null, lease_until=null, "
                            "result=null, error=null, progress=null, started_at=null, finished_at=null, expires_at=null, "
                            "updated_at=? where id=?", (now, task_id))
            return self._task(self._task_row(task_id))

    def reap_tasks(self):
        """收回超时的任务（还有次数就重新排队，否则失败），排队太久的标成过期。返回变化了的任务。"""
        now = time.time()
        with self.lock, self.db:
            rows = self.db.execute("select id, attempts, max_attempts, timeout from tasks where status='running' and lease_until < ?",
                                   (now,)).fetchall()
            for r in rows:
                err = f"第 {r['attempts']} 次执行超时（{r['timeout']} 秒内没有回报）"
                if r["attempts"] < r["max_attempts"]:
                    self.db.execute("update tasks set status='queued', error=?, claim_token=null, lease_until=null, "
                                    "updated_at=? where id=?", (err, now, r["id"]))
                else:
                    self.db.execute("update tasks set status='failed', error=?, claim_token=null, lease_until=null, "
                                    "finished_at=?, updated_at=? where id=?", (err, now, now, r["id"]))
            expired = self.db.execute("select id from tasks where status='queued' and expires_at < ?", (now,)).fetchall()
            self.db.execute("update tasks set status='expired', finished_at=?, updated_at=? where status='queued' and expires_at < ?",
                            (now, now, now))
            return [self._task(self._task_row(i), full=False) for i in [r["id"] for r in rows] + [r["id"] for r in expired]]

    def purge_tasks(self):
        with self.lock, self.db:
            self.db.execute(f"delete from tasks where status in ({','.join('?' * len(TASK_FINAL))}) and finished_at < ?",
                            (*TASK_FINAL, time.time() - TASK_KEPT_DAYS * 86400))

    def _task_where(self, owner=None, grp=None, agent_id=None, box=None):
        """grp 为 None 不限队列，'' 是公共池。agent_id + box：sent 它发的，worked 它领过的，None 两者都算。"""
        where, args = ["1=1"], []
        if owner is not None:
            where.append("t.owner_id=?")
            args.append(owner)
        if grp is not None:
            where.append("t.grp=?")
            args.append(grp)
        if agent_id is not None:
            where.append({"sent": "t.from_agent=?", "worked": "t.worker=?"}.get(box, "(t.from_agent=? or t.worker=?)"))
            args += [agent_id] * (1 if box in ("sent", "worked") else 2)
        return where, args

    def tasks(self, owner=None, grp=None, agent_id=None, box=None, statuses=None, q=None, before=None, limit=50):
        where, args = self._task_where(owner, grp, agent_id, box)
        if statuses:
            where.append(f"t.status in ({','.join('?' * len(statuses))})")
            args += statuses
        if q:
            q = q.lstrip("#")
            where.append("(instr(lower(t.kind), lower(?)) > 0 or t.id = ? or t.dedupe_key = ?)")
            args += [q, int(q) if q.isdigit() else -1, q]
        if before:
            where.append("t.id < ?")
            args.append(before)
        with self.lock:
            rows = self.db.execute(f"{TASK_SELECT} where {' and '.join(where)} order by t.id desc limit ?",
                                   args + [limit]).fetchall()
        return [self._task(r, full=False) for r in rows]

    def task_stats(self, owner=None, grp=None, agent_id=None):
        """排队中 / 执行中是当前数，完成 / 失败是最近 24 小时。"""
        where, args = self._task_where(owner, grp, agent_id)
        since = time.time() - 86400
        sql = ("select sum(t.status='queued') queued, sum(t.status='running') running, "
               "sum(t.status='done' and t.finished_at >= ?) done, sum(t.status='failed' and t.finished_at >= ?) failed "
               f"from tasks t where {' and '.join(where)}")
        with self.lock:
            r = self.db.execute(sql, [since, since] + args).fetchone()
        return {k: r[k] or 0 for k in ("queued", "running", "done", "failed")}

    def queues(self, owner=None):
        """每个用户的队列：各分组 + 公共池，带能领它的 agent 数和排队 / 执行中的任务数。"""
        cond, args = ("where owner_id=?", [owner]) if owner is not None else ("", [])
        online_since = time.time() - ONLINE_WINDOW
        with self.lock:
            ag = self.db.execute(
                "select owner_id, grp, count(*) n, sum(last_seen > ?) online from agents "
                f"{cond + (' and' if cond else 'where')} revoked_at is null group by owner_id, grp",
                [online_since] + args).fetchall()
            tk = self.db.execute(
                "select owner_id, grp, sum(status='queued') queued, sum(status='running') running from tasks "
                f"{cond + (' and' if cond else 'where')} status in ('queued', 'running') group by owner_id, grp", args).fetchall()
            names = {r["id"]: r["username"] for r in self.db.execute("select id, username from users")}
        out = {}
        def q(o, g):
            return out.setdefault((o, g), {"owner_id": o, "owner_name": names.get(o), "group": g,
                                           "agents": 0, "online": 0, "queued": 0, "running": 0})
        for r in ag:
            # 有分组的 agent 也能领公共池，所以公共池的人手是这个用户的全部 agent
            for g in {r["grp"], ""}:
                x = q(r["owner_id"], g)
                x["agents"] += r["n"]
                x["online"] += r["online"] or 0
        for r in tk:
            x = q(r["owner_id"], r["grp"])
            x["queued"], x["running"] = r["queued"] or 0, r["running"] or 0
        return sorted(out.values(), key=lambda x: (x["owner_id"] or 0, x["group"] != "", x["group"]))


class Bus:
    """把变化推给打开着的后台页面（SSE）。普通用户只收自己名下的，管理员收全部。"""

    def __init__(self):
        self.subs = {}
        self.lock = threading.Lock()

    def subscribe(self, user):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs[q] = user
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.pop(q, None)

    def publish(self, name, payload, owner_id):
        msg = f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
        with self.lock:
            for q, user in self.subs.items():
                if not user["is_admin"] and user["id"] != owner_id:
                    continue
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


class Signal:
    """任务一有变化就 +1 并唤醒所有在长轮询的请求，它们各自再查一遍。"""

    def __init__(self):
        self.cv = threading.Condition()
        self.version = 0

    def changed(self):
        with self.cv:
            self.version += 1
            self.cv.notify_all()

    def wait(self, version, timeout):
        with self.cv:
            self.cv.wait_for(lambda: self.version != version, timeout)
            return self.version


def int_field(body, key, default, lo, hi):
    v = body.get(key, default)
    if v is None:
        return default
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ApiError(400, f"{key} 必须是 {lo} 到 {hi} 之间的整数")
    return v


def clean_group(v):
    v = "" if v is None else v
    if not isinstance(v, str) or v and not GROUP_RE.fullmatch(v):
        raise ApiError(400, "分组只能用小写字母、数字和 -，1-32 位，以字母或数字开头；留空表示不分组")
    return v


def clean_task(body):
    if not isinstance(body, dict):
        raise ApiError(400, "任务必须是 JSON 对象")
    kind = body.get("kind", "task")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise ApiError(400, "kind 必须是 1-64 字的字符串")
    dedupe = body.get("dedupe_key")
    if dedupe is not None and (not isinstance(dedupe, str) or not dedupe or len(dedupe) > 128):
        raise ApiError(400, "dedupe_key 必须是 1-128 字的字符串")
    return {"kind": kind, "payload": body.get("payload"), "dedupe_key": dedupe,
            "priority": int_field(body, "priority", 0, -100, 100),
            "max_attempts": int_field(body, "max_attempts", 3, 1, 20),
            "timeout": int_field(body, "timeout", 600, 10, 86400),
            "expires_in": int_field(body, "expires_in", 0, 0, 86400 * 30)}


def clean_event(e):
    if not isinstance(e, dict):
        raise ApiError(400, "每个事件必须是 JSON 对象")
    kind = e.get("kind", "log")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise ApiError(400, "kind 必须是 1-64 字的字符串")
    level = e.get("level", "info")
    if level not in LEVELS:
        raise ApiError(400, f"level 只能是 {', '.join(LEVELS)}")
    text = e.get("text", "")
    if not isinstance(text, str) or len(text) > 20000:
        raise ApiError(400, "text 必须是不超过 20000 字的字符串")
    ts = e.get("ts")
    now = time.time()
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or abs(ts - now) > 86400 * 7:
        ts = now
    return {"ts": float(ts), "kind": kind, "level": level, "text": text, "data": e.get("data")}


def clean_password(v):
    if not isinstance(v, str) or len(v) < MIN_PASSWORD or len(v) > 200:
        raise ApiError(400, f"密码至少 {MIN_PASSWORD} 位")
    return v


ROUTES = [
    ("GET", r"/", "page"),
    ("POST", r"/api/login", "login"),
    ("POST", r"/api/logout", "logout"),
    ("GET", r"/api/me", "me"),
    ("POST", r"/api/me/password", "me_password"),
    ("GET", r"/api/users", "admin_users"),
    ("POST", r"/api/users", "admin_create_user"),
    ("PATCH", r"/api/users/(\d+)", "admin_update_user"),
    ("GET", r"/api/agents", "admin_list"),
    ("POST", r"/api/agents", "admin_create"),
    ("POST", r"/api/agents/(\d+)/rotate", "admin_rotate"),
    ("DELETE", r"/api/agents/(\d+)", "admin_revoke"),
    ("PATCH", r"/api/agents/(\d+)", "admin_rename"),
    ("GET", r"/api/agents/(\d+)/events", "admin_events"),
    ("GET", r"/api/events", "admin_all_events"),
    ("GET", r"/api/stream", "admin_stream"),
    ("GET", r"/api/queues", "admin_queues"),
    ("GET", r"/api/tasks", "admin_tasks"),
    ("POST", r"/api/tasks", "admin_push"),
    ("GET", r"/api/tasks/stats", "admin_task_stats"),
    ("GET", r"/api/tasks/(\d+)", "admin_task"),
    ("POST", r"/api/tasks/(\d+)/cancel", "admin_cancel"),
    ("POST", r"/api/tasks/(\d+)/retry", "admin_retry"),
    ("PUT", r"/api/v1/status", "agent_status"),
    ("POST", r"/api/v1/status", "agent_status"),
    ("PATCH", r"/api/v1/status", "agent_status_merge"),
    ("POST", r"/api/v1/events", "agent_events"),
    ("POST", r"/api/v1/heartbeat", "agent_heartbeat"),
    ("GET", r"/api/v1/whoami", "agent_whoami"),
    ("POST", r"/api/v1/tasks", "agent_push"),
    ("GET", r"/api/v1/tasks", "agent_tasks"),
    ("POST", r"/api/v1/tasks/pull", "agent_pull"),
    ("GET", r"/api/v1/tasks/(\d+)", "agent_task"),
    ("POST", r"/api/v1/tasks/(\d+)/progress", "agent_progress"),
    ("POST", r"/api/v1/tasks/(\d+)/complete", "agent_complete"),
    ("POST", r"/api/v1/tasks/(\d+)/fail", "agent_fail"),
    ("POST", r"/api/v1/tasks/(\d+)/cancel", "agent_cancel"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ami-hub"
    store: Store = None
    bus: Bus = None
    signal: Signal = None
    admin_user: str = ""          # 内置管理员（环境变量），密码只能改环境变量
    login_fails = collections.defaultdict(list)   # ip -> 失败时间
    login_lock = threading.Lock()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 通用 ----
    def _dispatch(self):
        url = urlparse(self.path)
        self.query = parse_qs(url.query, keep_blank_values=True)
        allowed = False
        for method, pattern, name in ROUTES:
            m = re.fullmatch(pattern, url.path)
            if not m:
                continue
            allowed = True
            if method != self.command:
                continue
            try:
                getattr(self, name)(*m.groups())
            except ApiError as e:
                self._json({"error": e.msg}, e.code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._json({"error": "method not allowed" if allowed else "not found"}, 405 if allowed else 404)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "请求体太大（上限 1MB）")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(400, "请求体不是合法 JSON")

    def _obj(self):
        body = self._body()
        if not isinstance(body, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return body

    def _arg(self, k):
        return (self.query.get(k) or [""])[0].strip()

    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def _session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _login(self):
        """控制台接口：确认已登录，返回当前用户。"""
        token = self._session_token()
        user = self.store.session_user(token) if token else None
        if not user:
            raise ApiError(401, "请先登录")
        self.user = user
        return user

    def _scope(self):
        """数据范围：管理员看全部（None），普通用户只看自己的。"""
        return None if self.user["is_admin"] else self.user["id"]

    def _require_admin(self):
        if not self._login()["is_admin"]:
            raise ApiError(403, "只有管理员能做这个操作")

    def _client_ip(self):
        # 只监听 127.0.0.1，前面是 Caddy；它把真实 IP 追加在 X-Forwarded-For 最后
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def _me(self, user):
        return {**user, "managed": user["username"] == self.admin_user}

    # ---- 登录 / 账号 ----
    def login(self):
        ip, now = self._client_ip(), time.time()
        with self.login_lock:
            fails = [t for t in self.login_fails[ip] if now - t < LOGIN_WINDOW]
            self.login_fails[ip] = fails
            if len(fails) >= LOGIN_FAILS:
                raise ApiError(429, f"失败次数太多，{int(LOGIN_WINDOW - (now - fails[0])) // 60 + 1} 分钟后再试")
        body = self._obj()
        user = self.store.check_login(str(body.get("username", "")).strip(), str(body.get("password", "")))
        if not user:
            with self.login_lock:
                self.login_fails[ip].append(now)
            raise ApiError(401, "用户名或密码不对")
        with self.login_lock:
            self.login_fails.pop(ip, None)
        token = self.store.new_session(user["id"])
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; "
                  f"Max-Age={SESSION_DAYS * 86400}")
        self._json(self._me(user), cookie=cookie)

    def logout(self):
        token = self._session_token()
        if token:
            self.store.end_session(token)
        self._json({"ok": True}, cookie=f"{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0")

    def me(self):
        self._json(self._me(self._login()))

    def me_password(self):
        user = self._login()
        if user["username"] == self.admin_user:
            raise ApiError(400, "这个账号的密码由服务器环境变量 AMI_ADMIN_PASSWORD 管理，改那里再重启服务")
        body = self._obj()
        if not self.store.check_password_of(user["id"], str(body.get("old", ""))):
            raise ApiError(400, "当前密码不对")
        self.store.update_user(user["id"], password=clean_password(body.get("new")))
        token = self.store.new_session(user["id"])   # 其它地方的登录都失效了，这里换一个新的 session 继续用
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; "
                  f"Max-Age={SESSION_DAYS * 86400}")
        self._json({"ok": True}, cookie=cookie)

    def admin_users(self):
        self._require_admin()
        self._json({"users": [{**u, "managed": u["username"] == self.admin_user} for u in self.store.users()]})

    def admin_create_user(self):
        self._require_admin()
        body = self._obj()
        username = str(body.get("username", "")).strip()
        if not USERNAME_RE.fullmatch(username):
            raise ApiError(400, "用户名 2-32 位，可以用字母、数字、中文和 _ . @ -")
        user = self.store.create_user(username, clean_password(body.get("password")), bool(body.get("is_admin")))
        self._json({"user": {**user, "agents": 0, "managed": False}}, 201)

    def admin_update_user(self, user_id):
        self._require_admin()
        target = self.store.get_user(int(user_id))
        if not target:
            raise ApiError(404, "没有这个用户")
        body = self._obj()
        managed = target["username"] == self.admin_user
        if managed and body.keys() & {"password", "is_admin", "disabled"}:
            raise ApiError(400, "内置管理员由服务器环境变量管理，不能在这里修改")
        if target["id"] == self.user["id"] and (body.get("disabled") or body.get("is_admin") is False):
            raise ApiError(400, "不能停用自己，也不能取消自己的管理员")
        password = clean_password(body["password"]) if "password" in body else None
        is_admin = bool(body["is_admin"]) if "is_admin" in body else None
        disabled = bool(body["disabled"]) if "disabled" in body else None
        user = self.store.update_user(target["id"], password, is_admin, disabled)
        self._json({"user": {**user, "managed": managed}})

    def _agent_id(self):
        """agent 接口：用 API key 认证，返回 {id, owner_id, grp}。"""
        key = self._bearer()
        agent = self.store.by_key(key) if key else None
        if not agent:
            raise ApiError(401, "API key 无效或已吊销")
        return agent

    def _agent_or_404(self, agent_id):
        agent = self.store.get(int(agent_id))
        if not agent or self._scope() is not None and agent["owner_id"] != self.user["id"]:
            raise ApiError(404, "没有这个 agent")
        return agent

    # ---- 后台页面 ----
    def page(self):
        body = (HERE / "dashboard.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _publish_agent(self, agent_id):
        agent = self.store.get(agent_id)
        self.bus.publish("agent", agent, agent["owner_id"])
        return agent

    def admin_list(self):
        self._login()
        self._json({"agents": self.store.list(self._scope()), "server_time": time.time(), "online_window": ONLINE_WINDOW})

    def _name(self, body):
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ApiError(400, "名字必填，不超过 80 字")
        return name

    def admin_create(self):
        self._login()
        body = self._obj()
        agent_id, key = self.store.create(self._name(body), self.user["id"], clean_group(body.get("group")))
        self._json({"agent": self._publish_agent(agent_id), "api_key": key}, 201)

    def admin_rotate(self, agent_id):
        self._login()
        self._agent_or_404(agent_id)
        key = self.store.rotate(int(agent_id))
        if not key:
            raise ApiError(404, "没有这个 agent，或已吊销")
        self._json({"agent": self._publish_agent(int(agent_id)), "api_key": key})

    def admin_rename(self, agent_id):
        self._login()
        self._agent_or_404(agent_id)
        self.store.rename(int(agent_id), self._name(self._obj()))
        self._json({"agent": self._publish_agent(int(agent_id))})

    def admin_revoke(self, agent_id):
        self._login()
        self._agent_or_404(agent_id)
        self.store.revoke(int(agent_id))
        self._json({"agent": self._publish_agent(int(agent_id))})

    def _event_query(self, agent_id):
        try:
            before = int(self._arg("before")) if self._arg("before") else None
            limit = max(1, min(int(self._arg("limit") or 100), 500))
        except ValueError:
            raise ApiError(400, "before / limit 必须是数字")
        levels = [l for l in self._arg("level").split(",") if l in LEVELS]
        owner = self._scope()
        events = self.store.events(agent_id, before, limit, levels, self._arg("q")[:200] or None, owner)
        out = {"events": events}
        if self._arg("counts"):
            out["counts"] = self.store.event_counts(agent_id, time.time() - 86400, owner)
        self._json(out)

    def admin_events(self, agent_id):
        self._login()
        self._agent_or_404(agent_id)
        self._event_query(int(agent_id))

    def admin_all_events(self):
        self._login()
        agent = self._arg("agent")
        self._event_query(int(agent) if agent.isdigit() else None)

    def admin_stream(self):
        user = self._login()
        q = self.bus.subscribe(user)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.bus.unsubscribe(q)

    # ---- agent 上报 ----
    def agent_status(self, merge=False):
        agent = self._agent_id()
        status = self._body()
        if not isinstance(status, dict):
            raise ApiError(400, "状态必须是 JSON 对象")
        self.store.touch(agent["id"], status, merge=merge)
        self._publish_agent(agent["id"])
        self._json({"ok": True})

    def agent_status_merge(self):
        self.agent_status(merge=True)

    def agent_events(self):
        agent = self._agent_id()
        body = self._body()
        items = body if isinstance(body, list) else body.get("events", [body]) if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raise ApiError(400, "需要一个事件对象、事件数组，或 {\"events\": [...]}")
        if len(items) > 200:
            raise ApiError(400, "一次最多 200 条事件")
        saved = self.store.add_events(agent["id"], [clean_event(e) for e in items])
        for e in saved:
            self.bus.publish("event", {"agent_id": agent["id"], "event": e}, agent["owner_id"])
        self._publish_agent(agent["id"])
        self._json({"ok": True, "ids": [e["id"] for e in saved]})

    def agent_heartbeat(self):
        agent = self._agent_id()
        self.store.touch(agent["id"])
        self._publish_agent(agent["id"])
        self._json({"ok": True})

    def agent_whoami(self):
        agent = self.store.get(self._agent_id()["id"])
        self._json({"id": agent["id"], "name": agent["name"], "group": agent["group"]})

    # ---- 任务队列 ----
    def _task_changed(self, task):
        light = {k: v for k, v in task.items() if k not in ("payload", "result", "token")}
        self.bus.publish("task", light, task["owner_id"])
        self.signal.changed()

    def _list_args(self):
        try:
            before = int(self._arg("before")) if self._arg("before") else None
            limit = max(1, min(int(self._arg("limit") or 50), 200))
        except ValueError:
            raise ApiError(400, "before / limit 必须是数字")
        statuses = [s for s in self._arg("status").split(",") if s in TASK_STATUSES]
        return {"statuses": statuses, "q": self._arg("q")[:200] or None, "before": before, "limit": limit}

    def _wait_secs(self, v):
        if v in (None, ""):
            return 0
        try:
            return max(0.0, min(float(v), MAX_WAIT))
        except (TypeError, ValueError):
            raise ApiError(400, f"wait 必须是 0 到 {MAX_WAIT} 之间的秒数")

    def _console_filter(self):
        """控制台的任务筛选：owner（管理员可选）、group（'' 为公共池，不传不限）、agent。"""
        owner = self._scope()
        if owner is None and self._arg("owner").isdigit():
            owner = int(self._arg("owner"))
        grp = self.query["group"][0].strip() if "group" in self.query else None
        agent = self._arg("agent")
        return {"owner": owner, "grp": grp, "agent_id": int(agent) if agent.isdigit() else None}

    def admin_queues(self):
        self._login()
        self._json({"queues": self.store.queues(self._scope())})

    def admin_tasks(self):
        self._login()
        self._json({"tasks": self.store.tasks(**self._console_filter(), **self._list_args())})

    def admin_task_stats(self):
        self._login()
        self._json(self.store.task_stats(**self._console_filter()))

    def admin_task(self, task_id):
        self._login()
        task = self.store.get_task(int(task_id))
        if not task or self._scope() is not None and task["owner_id"] != self.user["id"]:
            raise ApiError(404, "没有这个任务")
        self._json({"task": task})

    def admin_push(self):
        self._login()
        body = self._obj()
        owner = self.user["id"]
        if self.user["is_admin"] and isinstance(body.get("owner_id"), int):   # 管理员可以往别人的队列里派
            if not self.store.get_user(body["owner_id"]):
                raise ApiError(404, "没有这个用户")
            owner = body["owner_id"]
        task, created = self.store.push_task(owner, clean_group(body.get("group")), None, clean_task(body))
        if created:
            self._task_changed(task)
        self._json({"task": task}, 201 if created else 200)

    def admin_cancel(self, task_id):
        self._login()
        task = self.store.cancel_task(int(task_id), owner=self._scope())
        self._task_changed(task)
        self._json({"task": task})

    def admin_retry(self, task_id):
        self._login()
        task = self.store.retry_task(int(task_id), owner=self._scope())
        self._task_changed(task)
        self._json({"task": task})

    def agent_push(self):
        agent = self._agent_id()
        task, created = self.store.push_task(agent["owner_id"], agent["grp"], agent["id"], clean_task(self._obj()))
        self.store.touch(agent["id"])
        if created:
            self._task_changed(task)
        self._json({"task": task, "created": created}, 201 if created else 200)

    def agent_tasks(self):
        agent = self._agent_id()
        box = self._arg("box")
        if box not in ("", "sent", "worked"):
            raise ApiError(400, "box 只能是 sent（我发的）或 worked（我领过的）")
        self._json({"tasks": self.store.tasks(agent["owner_id"], None, agent["id"], box or None, **self._list_args())})

    def agent_pull(self):
        agent = self._agent_id()
        body = self._obj()
        limit = int_field(body, "limit", 1, 1, 50)
        wait = self._wait_secs(body.get("wait"))
        kinds = body.get("kinds")
        if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
            raise ApiError(400, "kinds 必须是字符串数组")
        self.store.touch(agent["id"])
        self._publish_agent(agent["id"])
        deadline = time.monotonic() + wait
        while True:
            version = self.signal.version
            tasks = self.store.claim_tasks(agent, limit, kinds or None)
            left = deadline - time.monotonic()
            if tasks or left <= 0:
                break
            self.signal.wait(version, left)
        for t in tasks:
            self._task_changed(t)
        self._json({"tasks": tasks})

    def _visible_task(self, task_id, agent):
        """agent 能看的任务：自己发的、自己领过的、以及自己能领的队列里的。"""
        task = self.store.get_task(int(task_id))
        if not task or not (agent["id"] in (task["from_agent"], task["worker"]) or
                            task["owner_id"] == agent["owner_id"] and task["group"] in (agent["grp"], "")):
            raise ApiError(404, "没有这个任务")
        return task

    def agent_task(self, task_id):
        agent = self._agent_id()
        wait = self._wait_secs(self._arg("wait"))
        deadline = time.monotonic() + wait
        while True:
            version = self.signal.version
            task = self._visible_task(task_id, agent)
            left = deadline - time.monotonic()
            if task["status"] in TASK_FINAL or left <= 0:
                break
            self.signal.wait(version, left)
        self._json({"task": task})

    def agent_progress(self, task_id):
        agent = self._agent_id()
        body = self._obj()
        extend = int_field(body, "extend", 0, 0, 86400)
        task = self.store.task_progress(int(task_id), agent["id"], body.get("token"), body.get("progress"), extend)
        self.store.touch(agent["id"])
        self._task_changed(task)
        self._json({"task": task})

    def _finish(self, task_id, ok):
        agent = self._agent_id()
        body = self._obj()
        error = None if ok else str(body.get("error") if body.get("error") is not None else "执行失败")[:20000]
        task = self.store.finish_task(int(task_id), agent["id"], body.get("token"), ok, body.get("result"), error,
                                      bool(body.get("retry")))
        self.store.touch(agent["id"])
        self._task_changed(task)
        self._json({"task": task})

    def agent_complete(self, task_id):
        self._finish(task_id, True)

    def agent_fail(self, task_id):
        self._finish(task_id, False)

    def agent_cancel(self, task_id):
        task = self.store.cancel_task(int(task_id), by_agent=self._agent_id()["id"])
        self._task_changed(task)
        self._json({"task": task})


def reaper(store, bus, signal):
    """后台每 2 秒收回超时的任务，每小时清一次过老的已结束任务。"""
    last_purge = 0
    while True:
        time.sleep(2)
        try:
            changed = store.reap_tasks()
            for t in changed:
                bus.publish("task", t, t["owner_id"])
            if changed:
                signal.changed()
            if time.time() - last_purge > 3600:
                store.purge_tasks()
                last_purge = time.time()
        except Exception as e:   # 别让后台线程因为一次数据库错误就退出
            sys.stderr.write(f"reaper: {e!r}\n")


def main():
    ap = argparse.ArgumentParser(description="AMI 中控")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--data", default=str(HERE / "data"), help="数据库存放目录")
    args = ap.parse_args()

    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(str(data_dir / "hub.db"))
    admin_user = os.environ.get("AMI_ADMIN_USER", "admin").strip()
    admin_password = os.environ.get("AMI_ADMIN_PASSWORD", "")
    if admin_password:
        store.ensure_admin(admin_user, admin_password)
    elif not store.user_count():
        sys.exit("请设置 AMI_ADMIN_PASSWORD（内置管理员的登录密码）")
    Handler.store, Handler.bus, Handler.signal = store, Bus(), Signal()
    Handler.admin_user = admin_user if admin_password else ""

    threading.Thread(target=reaper, args=(store, Handler.bus, Handler.signal), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"AMI 中控已启动：http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
