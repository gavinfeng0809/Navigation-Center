#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重要通知中心 · Notice Hub
------------------------------------------------------------
把企业微信 / Outlook 邮件里的【重要通知】统一归档到一个可搜索的页面。

- 后端：Python 3.12 标准库，零第三方依赖
- 存储：SQLite 单文件
- 采集：v1 手动导入（粘贴 / CSV / JSON / XLSX / .eml）
        v2 预留 POST /api/ingest 幂等接口 + sync_sources 配置表，
        接 IMAP / Graph 采集器时不需要改库结构和前端。
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, parseaddr, parsedate_to_datetime
from html import unescape as html_unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DATA_DIR = os.environ.get("NOTICE_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("NOTICE_DB_PATH", os.path.join(DATA_DIR, "notices.db"))
PORT = int(os.environ.get("PORT", "8091"))
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
SESSION_COOKIE = "nh_session"
SESSION_TTL = int(os.environ.get("SESSION_TTL_HOURS", "720")) * 3600  # 默认 30 天
MAX_UPLOAD = 64 * 1024 * 1024  # 单次上传上限 64MB

# 响应压缩：低于这个体积压了不划算（还可能因为 gzip 头反而变大）
GZIP_MIN_BYTES = 1024
# 只压文本类。图片/zip/xlsx 本身已压缩，再压纯属浪费 CPU
GZIP_TYPES = ("text/", "application/json", "application/javascript",
              "application/xml", "image/svg+xml")

# 首次启动（users 表为空）时自动种入的超级管理员
DEFAULT_ADMIN_USER = os.environ.get("DEFAULT_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.environ.get("DEFAULT_ADMIN_PASS", "admin")

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_LABELS = {ROLE_ADMIN: "管理员", ROLE_VIEWER: "查看"}

# 这些密码会被标记为弱密码，前端会持续提醒修改
WEAK_PASSWORDS = {"admin", "123456", "12345678", "password", "notice2026", "cse2026", "000000"}

PBKDF2_ROUNDS = 180000

VALID_SOURCES = ("wecom", "outlook", "manual", "other")
VALID_LEVELS = ("urgent", "important", "normal")

SOURCE_LABELS = {
    "wecom": "企业微信",
    "outlook": "Outlook",
    "manual": "手工录入",
    "other": "其它来源",
}
LEVEL_LABELS = {"urgent": "紧急", "important": "重要", "normal": "普通"}

DEFAULT_CATEGORIES = [
    ("网络变更", "#3b82f6"),
    ("故障公告", "#ef4444"),
    ("安全通告", "#f59e0b"),
    ("系统维护", "#8b5cf6"),
    ("业务通知", "#10b981"),
    ("人事行政", "#ec4899"),
    ("未分类", "#6b7280"),
]

_LOG_LOCK = threading.Lock()


def log(*args) -> None:
    with _LOG_LOCK:
        print("[notice-hub]", *args, flush=True)


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------

DT_FMT = "%Y-%m-%d %H:%M:%S"
DT_FMT_MIN = "%Y-%m-%d %H:%M"

# 容器基础镜像通常是 UTC，这里在应用层显式指定展示时区，
# 避免挂载 /etc/localtime 或安装 tzdata 带来的环境依赖。
try:
    APP_TZ_OFFSET = float(os.environ.get("APP_TZ_OFFSET_HOURS", "8"))
except ValueError:
    APP_TZ_OFFSET = 8.0
APP_TZ = timezone(timedelta(hours=APP_TZ_OFFSET))


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone(APP_TZ)


def now_str() -> str:
    return now().strftime(DT_FMT)


def normalize_dt(value) -> str:
    """把各种写法的时间归一化为 'YYYY-MM-DD HH:MM:SS'，失败返回空串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(DT_FMT)
    s = str(value).strip()
    if not s:
        return ""
    s = s.replace("/", "-").replace("T", " ").replace("年", "-").replace("月", "-").replace("日", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", "", s).strip()
    for fmt in (
        DT_FMT, DT_FMT_MIN, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S %p", "%Y-%m-%d %H:%M:%S.%f %p",
    ):
        try:
            return datetime.strptime(s, fmt).strftime(DT_FMT)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mi, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        try:
            return datetime(y, mo, d, hh, mi, ss).strftime(DT_FMT)
        except ValueError:
            return ""
    return ""


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS notices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id        TEXT UNIQUE,
    source        TEXT NOT NULL DEFAULT 'manual',
    source_name   TEXT NOT NULL DEFAULT '',
    sender        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '未分类',
    level         TEXT NOT NULL DEFAULT 'normal',
    tags          TEXT NOT NULL DEFAULT '[]',
    publisher     TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    attachments   TEXT NOT NULL DEFAULT '[]',
    origin        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_n_pub      ON notices(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_n_recv     ON notices(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_n_source   ON notices(source);
CREATE INDEX IF NOT EXISTS idx_n_category ON notices(category);

CREATE TABLE IF NOT EXISTS categories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT UNIQUE NOT NULL,
    color TEXT NOT NULL DEFAULT '#6b7280',
    sort  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    pwd_salt    TEXT NOT NULL,
    pwd_hash    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'viewer',
    display_name TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    weak_pwd    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    last_login  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nav_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nav_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    subgroup    TEXT NOT NULL DEFAULT '',   -- 组内二级分组（如「客户站点防火墙」下按客户分）
    tags        TEXT NOT NULL DEFAULT '[]',
    icon        TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '#3b82f6',
    sort        INTEGER NOT NULL DEFAULT 0,
    visible_to  TEXT NOT NULL DEFAULT 'guest',   -- guest 免登录可见 | member 需登录 | admin 仅管理员
    probe       INTEGER NOT NULL DEFAULT 1,      -- 是否参与在线探测
    favorite    INTEGER NOT NULL DEFAULT 0,
    launch      TEXT NOT NULL DEFAULT 'web',     -- web 浏览器打开 | winbox 本地 Winbox | custom 自定义协议
    launch_host TEXT NOT NULL DEFAULT '',        -- winbox 用：设备地址（可含端口）
    launch_arg  TEXT NOT NULL DEFAULT '',        -- custom 用：完整自定义协议 URL
    visit_count INTEGER NOT NULL DEFAULT 0,
    last_visit  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nav_items_group ON nav_items(group_id, sort);

CREATE TABLE IF NOT EXISTS sync_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    config      TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 0,
    last_sync   TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""

_conn_local = threading.local()


def get_db() -> sqlite3.Connection:
    conn = getattr(_conn_local, "conn", None)
    if conn is None:
        # isolation_level=None → 自动提交模式。
        # 关键：默认模式下某条 INSERT 失败（如唯一键冲突）会留下未结束的事务，
        # 该连接会一直占着写锁，后续所有写请求都被 "database is locked" 堵死。
        # 自动提交模式下每条语句独立生效，不存在悬挂事务。
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _conn_local.conn = conn
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    cur = conn.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO categories(name, color, sort) VALUES(?,?,?)",
            [(n, c, i) for i, (n, c) in enumerate(DEFAULT_CATEGORIES)],
        )
    conn.commit()
    seed_admin()
    seed_nav()
    log("database ready at", DB_PATH)


def migrate_schema(conn) -> None:
    """轻量迁移：给已存在的库补列。

    `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，所以新增字段必须显式 ALTER，
    否则老库跑新代码会在读写新列时报错。
    """
    wanted = [
        ("nav_items", "subgroup", "subgroup TEXT NOT NULL DEFAULT ''"),
        ("nav_items", "launch", "launch TEXT NOT NULL DEFAULT 'web'"),
        ("nav_items", "launch_host", "launch_host TEXT NOT NULL DEFAULT ''"),
        ("nav_items", "launch_arg", "launch_arg TEXT NOT NULL DEFAULT ''"),
    ]
    for table, col, ddl in wanted:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            log(f"迁移：{table} 新增列 {col}")


def seed_admin() -> None:
    """users 表为空时种入初始管理员，不覆盖已存在的账号。"""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] > 0:
        return
    create_user(DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS, ROLE_ADMIN,
                display_name="系统管理员", created_by="system",
                enforce_strength=False)
    log(f"已创建初始管理员账号 {DEFAULT_ADMIN_USER} / {DEFAULT_ADMIN_PASS}")
    log("⚠️  请登录后立即在「更多 → 修改我的密码」里改掉它")


# audit_log 只增不删会无限膨胀：读接口只取 200 条，但表本身没人清理。
# 每写 AUDIT_PRUNE_EVERY 次顺手裁一次，只保留最新 AUDIT_KEEP_ROWS 条。
AUDIT_KEEP_ROWS = 5000
AUDIT_PRUNE_EVERY = 200
_audit_writes = 0
_audit_writes_lock = threading.Lock()


def audit(action: str, target: str = "", detail: str = "") -> None:
    global _audit_writes
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO audit_log(ts, action, target, detail) VALUES(?,?,?,?)",
            (now_str(), action, target, detail[:500]),
        )
        conn.commit()
        with _audit_writes_lock:
            _audit_writes += 1
            due = _audit_writes % AUDIT_PRUNE_EVERY == 0
        if due:
            # id 是 INTEGER PRIMARY KEY，走索引，代价极小
            conn.execute(
                "DELETE FROM audit_log WHERE id <= "
                "(SELECT MAX(id) FROM audit_log) - ?", (AUDIT_KEEP_ROWS,))
            conn.commit()
    except Exception:  # 审计失败不影响主流程
        pass


def get_secret() -> str:
    path = os.path.join(DATA_DIR, ".session_secret")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
        except OSError:
            pass
    s = secrets.token_hex(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return s


SECRET = None


def effective_sync_token() -> str:
    return SYNC_TOKEN or hashlib.sha256((SECRET + "sync").encode()).hexdigest()


# --------------------------------------------------------------------------
# 账号与权限
# --------------------------------------------------------------------------

def hash_password(password: str, salt: str = None) -> tuple:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), PBKDF2_ROUNDS)
    return salt, base64.b64encode(digest).decode("ascii")


def verify_password(password: str, salt: str, expected: str) -> bool:
    if not salt or not expected:
        return False
    try:
        _, calc = hash_password(password or "", salt)
    except Exception:
        return False
    return hmac.compare_digest(calc, expected)


def is_weak_password(password: str) -> bool:
    p = (password or "").strip().lower()
    return len(p) < 8 or p in WEAK_PASSWORDS


def create_user(username: str, password: str, role: str = ROLE_VIEWER,
                display_name: str = "", created_by: str = "",
                enforce_strength: bool = True) -> int:
    username = (username or "").strip()
    if not re.match(r"^[A-Za-z0-9._@-]{2,32}$", username):
        raise ValueError("账号只能包含字母、数字、点、下划线、@、连字符，长度 2-32 位")
    if not password:
        raise ValueError("密码不能为空")
    # 初始管理员允许用短密码种入（之后前端会持续提醒修改），
    # 但通过接口建号 / 改密一律要求至少 6 位。
    if enforce_strength and len(str(password)) < 6:
        raise ValueError("密码至少 6 位")
    role = role if role in (ROLE_ADMIN, ROLE_VIEWER) else ROLE_VIEWER
    salt, pwd_hash = hash_password(password)
    ts = now_str()
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO users(username, pwd_salt, pwd_hash, role, display_name, enabled,
             weak_pwd, created_by, created_at, updated_at)
           VALUES(?,?,?,?,?,1,?,?,?,?)""",
        (username, salt, pwd_hash, role, display_name,
         1 if is_weak_password(password) else 0, created_by, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_name(username: str):
    return get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(uid: int):
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def public_user(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    d.pop("pwd_hash", None)
    d.pop("pwd_salt", None)
    d["role_label"] = ROLE_LABELS.get(d.get("role"), d.get("role"))
    return d


def list_users() -> list:
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return [public_user(r) for r in rows]


def set_password(uid: int, password: str) -> None:
    if not password:
        raise ValueError("密码不能为空")
    salt, pwd_hash = hash_password(password)
    conn = get_db()
    conn.execute(
        "UPDATE users SET pwd_salt=?, pwd_hash=?, weak_pwd=?, updated_at=? WHERE id=?",
        (salt, pwd_hash, 1 if is_weak_password(password) else 0, now_str(), uid),
    )
    conn.commit()


def count_enabled_admins(exclude_id: int = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM users WHERE role=? AND enabled=1"
    args = [ROLE_ADMIN]
    if exclude_id:
        sql += " AND id<>?"
        args.append(exclude_id)
    return get_db().execute(sql, args).fetchone()["c"]


# ---- 会话：签名 Cookie，改密码 / 停用 / 删号都会立即失效 ----

def _b64e(raw: str) -> str:
    """URL-safe base64，去掉 '=' 填充（避免部分环境对 cookie 值里的等号处理不一致）。"""
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(text: str) -> str:
    """容忍带或不带 '=' 填充的 base64。"""
    s = (text or "").strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii")).decode("utf-8")


def sign_session(username: str, expires: int, pwd_hash: str) -> str:
    msg = f"{username}|{expires}|{pwd_hash}".encode("utf-8")
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def issue_session(username: str, pwd_hash: str) -> tuple:
    expires = int(time.time()) + SESSION_TTL
    sig = sign_session(username, expires, pwd_hash)
    return _b64e(f"{username}|{expires}|{sig}"), expires


def decode_session(token: str):
    """解析会话令牌，任一步不匹配都返回 None（对应 users 行）。"""
    if not token:
        return None
    try:
        raw = _b64d(token)
        username, expires, sig = raw.rsplit("|", 2)
        expires = int(expires)
    except Exception:
        return None
    if expires < int(time.time()):
        return None
    row = get_user_by_name(username)
    if row is None or not row["enabled"]:
        return None
    if not hmac.compare_digest(sig, sign_session(username, expires, row["pwd_hash"])):
        return None
    return row


# --------------------------------------------------------------------------
# 文本处理
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END_RE = re.compile(r"(?i)</(p|div|tr|li|h[1-6]|table|section)>")
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    s = _SCRIPT_RE.sub(" ", raw)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html_unescape(s)
    s = _WS_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def clean_text(raw) -> str:
    if raw is None:
        return ""
    return str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_tags(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        s = str(raw).strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    items = [str(x).strip() for x in v]
                else:
                    items = [s]
            except (ValueError, TypeError):
                items = re.split(r"[,，;；、|/]+", s)
        else:
            items = re.split(r"[,，;；、|/]+", s)
    out, seen = [], set()
    for it in items:
        it = it.strip().strip("#")
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def guess_source(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "manual"
    if s in VALID_SOURCES:
        return s
    if any(k in s for k in ("企微", "企业微信", "wecom", "weixin", "wxwork")):
        return "wecom"
    if any(k in s for k in ("outlook", "邮件", "邮箱", "mail", "exchange", "m365")):
        return "outlook"
    if any(k in s for k in ("手工", "手动", "录入", "manual")):
        return "manual"
    return "other"


def guess_level(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "normal"
    if s in VALID_LEVELS:
        return s
    if any(k in s for k in ("紧急", "特急", "最高", "urgent", "critical", "p0", "p1")):
        return "urgent"
    if any(k in s for k in ("重要", "高", "important", "high", "p2")):
        return "important"
    return "normal"


def make_ext_id(source: str, title: str, published_at: str, sender: str = "") -> str:
    raw = "|".join([source or "", title or "", published_at or "", sender or ""])
    return "auto-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# 通知读写
# --------------------------------------------------------------------------

def ensure_category(conn: sqlite3.Connection, name: str, color: str = None) -> None:
    name = (name or "").strip() or "未分类"
    row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if row:
        return
    nxt = conn.execute("SELECT COALESCE(MAX(sort),0)+1 AS s FROM categories").fetchone()["s"]
    conn.execute(
        "INSERT OR IGNORE INTO categories(name, color, sort) VALUES(?,?,?)",
        (name, color or "#6b7280", nxt),
    )


def apply_paste_attachments(items: list, attach) -> None:
    """把「粘贴文本」时一起上传的附件塞进解析出来的通知里。

    规则：
      · 只解析出一条 → 全挂给它（最常见：贴一条通知 + 几张截图）
      · 多条 → 只挂给正文里引用了该 URL 的那条（前端把图片插在正文对应位置）
    附件统一存成 {"name": 显示名, "url": "/uploads/xx.png"}；历史数据里的纯字符串
    附件照原样带上，前端的渲染两种都认。
    """
    if not items or not attach:
        return
    norm = []
    for a in (attach if isinstance(attach, list) else []):
        if isinstance(a, str) and a.strip():
            norm.append({"name": a.strip()[:120], "url": ""})
        elif isinstance(a, dict) and (a.get("url") or a.get("name")):
            norm.append({"name": str(a.get("name") or a.get("url"))[:120],
                         "url": str(a.get("url") or ""),
                         "size": int(a.get("size") or 0)})
    if not norm:
        return
    if len(items) == 1:
        items[0]["attachments"] = norm
        return
    for it in items:
        body = it.get("content") or ""
        picked = [a for a in norm if a["url"] and a["url"] in body]
        if picked:
            it["attachments"] = picked


# 取标题时要跳过的行：纯标签、问候语、邮件头、落款/签名、保密声明
TITLE_LABEL_RE = re.compile(
    r"^\s*(?:【(?:重要|紧急)?通知】|(?:重要|紧急)?(?:通知|公告|提醒|说明|注意事项|温馨提示))\s*[:：]?\s*$")
TITLE_GREET_RE = re.compile(
    r"^\s*(?:Dear\b[^,，]{0,30}[,，]?|Hi\b[^,，]{0,20}[,，]?|Hello\b"
    r"|各位(?:同事|领导|老师)?[好，,:：]*|大家好[，。!！:：]*|您好[，。!！:：]*|早上好|下午好"
    r"|Good\s+(?:morning|afternoon))\s*$", re.I)
TITLE_MAILHDR_RE = re.compile(
    r"^\s*(?:From|Sent|To|Cc|Bcc|Subject|发件人|发送时间|收件人|抄送|主题)\s*[:：]", re.I)
# 「自己公司」的特征串：识别邮件签名/落款行（签名行不能当标题）。
# 真实域名、电话、版权字样放 .env 的 OWN_BRANDS（逗号分隔，不入版本库）；
# 默认只给通用占位，换公司不用改代码。
OWN_BRANDS = [x.strip() for x in os.environ.get(
    "OWN_BRANDS", "example-corp.com,保密声明,版权,This communication may contain"
).split(",") if x.strip()]
TITLE_SIGNOFF_RE = re.compile("(" + "|".join(re.escape(x) for x in OWN_BRANDS) + ")", re.I)
# 落款/结束语（"Best Regards,"、"谢谢" 之类，自己不能当标题）
TITLE_CLOSING_RE = re.compile(
    r"^\s*(?:Best\s+Regards|Regards|Thanks|Thank\s+you|BR|Sincerely|此致|敬礼|谢谢|多谢)"
    r"\s*[,，。.!！]?\s*$", re.I)


def is_title_junk(line: str) -> bool:
    """这一行能不能当标题（纯标签/问候/邮件头/签名等都没信息量）。"""
    t = (line or "").strip()
    if not t:
        return True
    if (TITLE_LABEL_RE.match(t) or TITLE_GREET_RE.match(t) or TITLE_CLOSING_RE.match(t)
            or TITLE_MAILHDR_RE.match(t)):
        return True
    if TITLE_SIGNOFF_RE.search(t):
        return True
    # 邮箱/电话只有出现在**短行**里才当签名——正文里提到告警组邮箱的行不能因此被跳过
    if len(t) <= 40 and TITLE_SIGNOFF_RE.search(t):
        return True
    if re.fullmatch(r"[\s\-–—_=*#·•.，。；;、!！?？:：()（）\[\]【】]+", t):
        return True                     # 纯符号/分隔线
    if re.fullmatch(r"(?:https?://\S+|!\[[^\]]*\]\(\S+\))", t):
        return True                     # 只有链接/只有一张图
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]*(?:\s+[A-Za-z][A-Za-z.\-]*){1,3}", t):
        return True                     # 只有英文署名，如 Lisa Liu
    m = re.match(r"^\s*([^:：]{1,12})\s*[:：]\s*\S", t)
    if m and (m.group(1).strip() in FIELD_ALIASES or m.group(1).strip().lower() in FIELD_ALIASES):
        return True                     # 字段行（来源: 企微 / 发布时间: …）不是标题
    return False


def derive_title(content: str, limit: int = 60) -> str:
    """从正文里挑一行有信息量的当标题（纯规则，不调模型）。

    粘贴进来的文本常常没有标题行：第一行可能是「重要通知：」、问候语或邮件头，
    直接取第一行就会出现「重要通知：」这种没信息量的标题。
    这里跳过这些行；若前面出现过「重要通知：」这类标签，就把它和正文拼起来当标题。
    """
    label = ""
    for raw in (content or "").splitlines():
        t = raw.strip()
        if not t:
            continue
        if TITLE_LABEL_RE.match(t):
            label = t.rstrip(":：").strip()
            continue
        if is_title_junk(t):
            continue
        if label:
            # 「【重要通知】」这种带括号的标签直接拼，不加冒号
            sep = "" if label.endswith(("】", "]", "）", ")")) else "："
            title = f"{label}{sep}{t}"
        else:
            title = t
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) > limit:
            head = title[:limit]
            m = re.search(r"[。；;！!?？]", head)          # 优先在句子结束处断
            m2 = re.search(r"[，,、]", head)              # 退一步在逗号处断
            if m and m.start() >= 12:
                title = head[:m.start() + 1]
            elif m2 and m2.start() >= 12:
                title = head[:m2.start()] + "…"
            else:
                title = head + "…"
        return title
    return "（无标题通知）"


def build_notice_row(data: dict) -> dict:
    """把任意来源的 dict 归一化成 notices 表字段。"""
    title = clean_text(data.get("title") or data.get("标题") or data.get("subject") or "")
    content = clean_text(data.get("content") or data.get("正文") or data.get("body") or "")
    if not title:
        title = derive_title(content)
    source = guess_source(
        data.get("source") or data.get("来源") or ("manual" if not data.get("ext_id") else "")
    )
    category = clean_text(data.get("category") or data.get("分类") or "未分类") or "未分类"
    level = guess_level(data.get("level") or data.get("级别") or data.get("重要级别"))
    published_at = normalize_dt(
        data.get("published_at") or data.get("发布时间") or data.get("date") or data.get("时间")
    )
    received_at = normalize_dt(data.get("received_at") or data.get("接收时间")) or now_str()
    if not published_at:
        published_at = received_at
    tags = parse_tags(data.get("tags") or data.get("标签") or data.get("tag"))
    attachments = data.get("attachments") or data.get("附件") or []
    if isinstance(attachments, str):
        attachments = parse_tags(attachments)
    if not isinstance(attachments, list):
        attachments = []
    ext_id = clean_text(data.get("ext_id") or data.get("外部ID") or "")
    sender = clean_text(data.get("sender") or data.get("发件人") or data.get("sender_addr") or "")
    if not ext_id:
        ext_id = make_ext_id(source, title, published_at, sender)
    pinned = 1 if str(data.get("pinned", "")).strip() in ("1", "true", "True", "是") else 0
    return {
        "ext_id": ext_id,
        "source": source,
        "source_name": clean_text(
            data.get("source_name") or data.get("群名称") or data.get("群") or data.get("邮件文件夹") or ""
        ),
        "sender": sender,
        "title": title,
        "content": content,
        "category": category,
        "level": level,
        "tags": json.dumps(tags, ensure_ascii=False),
        "publisher": clean_text(data.get("publisher") or data.get("发布人") or data.get("from_name") or ""),
        "published_at": published_at,
        "received_at": received_at,
        "pinned": pinned,
        "attachments": json.dumps(attachments, ensure_ascii=False),
        "origin": clean_text(data.get("origin") or "")[:200000],
    }


def upsert_notice(conn: sqlite3.Connection, row: dict, update_existing: bool = False) -> tuple:
    """按 ext_id 幂等写入。返回 (notice_id, created_bool)。"""
    ensure_category(conn, row["category"])
    exist = conn.execute("SELECT id FROM notices WHERE ext_id=?", (row["ext_id"],)).fetchone()
    if exist:
        if update_existing:
            conn.execute(
                """UPDATE notices SET source=?, source_name=?, sender=?, title=?, content=?,
                   category=?, level=?, tags=?, publisher=?, published_at=?, received_at=?,
                   pinned=?, attachments=?, origin=?, updated_at=? WHERE id=?""",
                (row["source"], row["source_name"], row["sender"], row["title"], row["content"],
                 row["category"], row["level"], row["tags"], row["publisher"], row["published_at"],
                 row["received_at"], row["pinned"], row["attachments"], row["origin"],
                 now_str(), exist["id"]),
            )
        return exist["id"], False
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO notices(ext_id, source, source_name, sender, title, content, category, level,
             tags, publisher, published_at, received_at, pinned, attachments, origin, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["ext_id"], row["source"], row["source_name"], row["sender"], row["title"],
         row["content"], row["category"], row["level"], row["tags"], row["publisher"],
         row["published_at"], row["received_at"], row["pinned"], row["attachments"],
         row["origin"], ts, ts),
    )
    return cur.lastrowid, True


# 列表接口不下发的大字段：正文和邮件原始头。
# 单条 2KB 量级，几百条就是 MB 级首屏，而列表渲染只用 summary。
# 详情走 /api/notices/<id>，导出 /api/export 走 slim=False，两条路都不受影响。
LIST_OMIT_FIELDS = ("content", "origin")


def row_to_dict(r: sqlite3.Row, slim: bool = False) -> dict:
    d = dict(r)
    for k in ("tags", "attachments"):
        try:
            v = json.loads(d.get(k) or "[]")
            d[k] = v if isinstance(v, list) else []
        except (ValueError, TypeError):
            d[k] = []
    d["source_label"] = SOURCE_LABELS.get(d.get("source", ""), d.get("source", ""))
    d["level_label"] = LEVEL_LABELS.get(d.get("level", ""), d.get("level", ""))
    # summary 依赖 content，所以必须先把摘要算出来再考虑丢弃原文字段
    body = d.get("content") or ""
    d["summary"] = re.sub(r"\s+", " ", body)[:160]
    if slim:
        for k in LIST_OMIT_FIELDS:
            d.pop(k, None)
    return d


def query_notices(params: dict, slim: bool = True) -> dict:
    conn = get_db()
    where, args = [], []
    eff_time = "COALESCE(NULLIF(published_at,''), NULLIF(received_at,''), created_at)"

    keywords = [k for k in re.split(r"\s+", (params.get("q") or "").strip()) if k]
    for kw in keywords:
        like = f"%{kw}%"
        where.append(
            "(title LIKE ? OR content LIKE ? OR tags LIKE ? OR publisher LIKE ? "
            "OR sender LIKE ? OR source_name LIKE ? OR category LIKE ?)"
        )
        args.extend([like] * 7)

    for field, col in (("source", "source"), ("category", "category"), ("level", "level")):
        raw = (params.get(field) or "").strip()
        if raw:
            vals = [v for v in re.split(r"[,\s]+", raw) if v]
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)

    if (params.get("tag") or "").strip():
        where.append("tags LIKE ?")
        args.append(f"%{params['tag'].strip()}%")

    frm = normalize_dt(params.get("from"))[:10]
    to = normalize_dt(params.get("to"))[:10]
    if frm:
        where.append(f"{eff_time} >= ?")
        args.append(frm + " 00:00:00")
    if to:
        where.append(f"{eff_time} <= ?")
        args.append(to + " 23:59:59")

    if str(params.get("pinned", "")).strip() in ("1", "true"):
        where.append("pinned = 1")

    if (params.get("id") or "").strip():
        where.append("id = ?")
        args.append(params["id"].strip())

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) AS c FROM notices{sql_where}", args).fetchone()["c"]

    size = max(1, min(int(params.get("size") or 20), 200))
    page = max(1, int(params.get("page") or 1))
    order = "ASC" if (params.get("sort") or "desc").lower() == "asc" else "DESC"
    if (params.get("orderby") or "") == "received":
        order_by = f"pinned DESC, received_at {order}, id {order}"
    else:
        order_by = f"pinned DESC, {eff_time} {order}, id {order}"

    rows = conn.execute(
        f"SELECT * FROM notices{sql_where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        args + [size, (page - 1) * size],
    ).fetchall()
    return {
        "items": [row_to_dict(r, slim=slim) for r in rows],
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
    }


def list_categories() -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT c.name, c.color, COUNT(n.id) AS cnt
           FROM categories c LEFT JOIN notices n ON n.category = c.name
           GROUP BY c.name, c.color ORDER BY c.sort, c.name"""
    ).fetchall()
    known = {r["name"] for r in rows}
    extra = conn.execute(
        "SELECT category AS name, COUNT(*) AS cnt FROM notices "
        "WHERE category NOT IN (SELECT name FROM categories) GROUP BY category"
    ).fetchall()
    out = [dict(r) for r in rows]
    for r in extra:
        if r["name"] not in known:
            out.append({"name": r["name"], "color": "#6b7280", "cnt": r["cnt"]})
    return out


def stats() -> dict:
    conn = get_db()
    g = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]
    total = conn.execute("SELECT COUNT(*) AS c FROM notices").fetchone()["c"]
    return {
        "total": total,
        "by_source": g(
            "SELECT source, COUNT(*) AS cnt FROM notices GROUP BY source ORDER BY cnt DESC"
        ),
        "by_level": g(
            "SELECT level, COUNT(*) AS cnt FROM notices GROUP BY level ORDER BY cnt DESC"
        ),
        "by_category": g(
            "SELECT category, COUNT(*) AS cnt FROM notices GROUP BY category ORDER BY cnt DESC LIMIT 12"
        ),
        "by_month": g(
            """SELECT substr(COALESCE(NULLIF(published_at,''), received_at),1,7) AS ym,
                      COUNT(*) AS cnt FROM notices
               WHERE COALESCE(NULLIF(published_at,''), received_at) <> ''
               GROUP BY ym ORDER BY ym DESC LIMIT 12"""
        )[::-1],
        "labels": {"source": SOURCE_LABELS, "level": LEVEL_LABELS},
    }


def delete_expired(scope: str = "demo") -> int:
    conn = get_db()
    if scope == "demo":
        cur = conn.execute("DELETE FROM notices WHERE ext_id LIKE 'demo-%'")
    else:
        cur = conn.execute("DELETE FROM notices")
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# 导入解析
# --------------------------------------------------------------------------

FIELD_ALIASES = {
    "标题": "title", "title": "title", "主题": "title", "subject": "title", "通知标题": "title",
    "正文": "content", "content": "content", "内容": "content", "body": "content", "通知内容": "content",
    "来源": "source", "source": "source", "渠道": "source",
    "群名称": "source_name", "群": "source_name", "source_name": "source_name",
    "群/发件人": "source_name", "会话": "source_name", "邮件文件夹": "source_name",
    "发布人": "publisher", "publisher": "publisher", "通知人": "publisher",
    "发件人": "sender", "sender": "sender", "发件人地址": "sender",
    "时间": "published_at", "发布时间": "published_at", "published_at": "published_at",
    "日期": "published_at", "date": "published_at",
    "分类": "category", "category": "category", "类型": "category",
    "级别": "level", "重要级别": "level", "level": "level", "优先级": "level",
    "标签": "tags", "tags": "tags", "tag": "tags",
    "附件": "attachments", "attachments": "attachments",
    "置顶": "pinned", "pinned": "pinned",
    "外部ID": "ext_id", "ext_id": "ext_id", "消息ID": "ext_id",
}


def parse_pasted(text: str) -> list:
    """
    解析粘贴文本。每条记录之间用单独一行 '---' 分隔。
    每条记录：开头可选的 `键: 值` 头部，遇到第一个空行后全部视为正文。
    """
    text = clean_text(text)
    if not text:
        return []
    blocks = re.split(r"^\s*-{3,}\s*$", text, flags=re.MULTILINE)
    out = []
    for block in blocks:
        block = block.strip("\n")
        if not block.strip():
            continue
        data, body_lines = {}, []
        in_body = False
        for line in block.splitlines():
            if not in_body:
                if not line.strip():
                    in_body = True
                    continue
                m = re.match(r"^\s*([^:：]{1,12})\s*[:：]\s*(.*)$", line)
                if m and m.group(1).strip() in FIELD_ALIASES:
                    key = FIELD_ALIASES[m.group(1).strip()]
                    if key in ("content", "title"):
                        in_body = True
                        body_lines.append(m.group(2))
                        continue
                    data[key] = m.group(2).strip()
                    continue
                in_body = True
                body_lines.append(line)
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if body and not data.get("content"):
            data["content"] = body
        # 这里**故意不**把正文第一行设成标题：交给 build_notice_row() 的 derive_title()
        # 统一判断（否则「重要通知：」这种没信息量的第一行会被硬当标题，且覆盖后面的提炼逻辑）
        if data.get("title") or data.get("content"):
            out.append(data)
    return out


def parse_csv_bytes(raw: bytes) -> list:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    out = []
    for row in reader:
        data = {}
        for k, v in row.items():
            if k is None:
                continue
            key = FIELD_ALIASES.get(str(k).strip().lower(), FIELD_ALIASES.get(str(k).strip()))
            if key:
                data[key] = v
        if any((v or "").strip() for v in data.values() if isinstance(v, str)):
            out.append(data)
    return out


def parse_json_bytes(raw: bytes) -> list:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            obj = json.loads(raw.decode(enc))
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        return []
    items = obj if isinstance(obj, list) else obj.get("items") or obj.get("notices") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        data = {}
        for k, v in it.items():
            key = FIELD_ALIASES.get(str(k).strip().lower(), FIELD_ALIASES.get(str(k).strip()))
            if key:
                data[key] = v
        if data:
            out.append(data)
    return out


XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_col_index(ref: str) -> int:
    letters = re.match(r"^([A-Z]+)", (ref or "").upper())
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def parse_xlsx_bytes(raw: bytes) -> list:
    """最小 XLSX 读取器（标准库 zipfile + ElementTree），取第一个工作表。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))
        sheet = next((n for n in names if re.match(r"^xl/worksheets/sheet\d+\.xml$", n)), None)
        if not sheet:
            return []
        root = ET.fromstring(z.read(sheet))
        rows = []
        for row in root.iter(f"{XLSX_NS}row"):
            cells = {}
            for c in row.findall(f"{XLSX_NS}c"):
                ctype = c.get("t")
                if ctype == "inlineStr":
                    node = c.find(f"{XLSX_NS}is")
                    val = "".join(t.text or "" for t in node.iter(f"{XLSX_NS}t")) if node is not None else ""
                else:
                    v = c.find(f"{XLSX_NS}v")
                    val = v.text if v is not None and v.text is not None else ""
                    if ctype == "s" and val != "":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            val = ""
                cells[_xlsx_col_index(c.get("r"))] = val
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
        if not rows:
            return []
        header = [str(h).strip() for h in rows[0]]
        out = []
        for r in rows[1:]:
            if not any(str(x).strip() for x in r):
                continue
            data = {}
            for i, h in enumerate(header):
                key = FIELD_ALIASES.get(h.lower(), FIELD_ALIASES.get(h))
                if key and i < len(r):
                    data[key] = r[i]
            if data:
                out.append(data)
        return out


def decode_mime(value) -> str:
    if value is None:
        return ""
    try:
        decoded = value.decode() if isinstance(value, bytes) else str(value)
    except Exception:
        decoded = str(value)
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(decoded)))
    except Exception:
        return decoded


def parse_eml_bytes(raw: bytes) -> dict:
    """解析 .eml 邮件原文 → 通知 dict。"""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = decode_mime(msg.get("Subject")).strip()
    from_raw = decode_mime(msg.get("From"))
    from_name, from_addr = parseaddr(from_raw)
    published = ""
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(str(msg.get("Date")))
            if dt.tzinfo is not None:
                dt = dt.astimezone(APP_TZ)
            published = dt.strftime(DT_FMT)
        except Exception:
            published = ""
    ext_id = clean_text(msg.get("Message-ID") or "")
    if ext_id:
        ext_id = "eml" + hashlib.sha1(ext_id.encode("utf-8")).hexdigest()[:28]

    text_body, html_body, attachments = "", "", []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disp = part.get_content_disposition()
        if disp == "attachment" or (filename and part.get_content_maintype() != "text"):
            if filename:
                attachments.append(decode_mime(filename))
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if ctype == "text/plain" and not text_body:
            text_body = payload
        elif ctype == "text/html" and not html_body:
            html_body = payload
    content = text_body or html_to_text(html_body)
    other_headers = []
    for h in ("To", "Cc", "Reply-To"):
        v = msg.get(h)
        if v:
            other_headers.append(f"{h}: {decode_mime(v)}")
    return {
        "ext_id": ext_id,
        "title": subject or "（无主题邮件）",
        "content": content,
        "source": "outlook",
        "sender": from_addr or "",
        "publisher": decode_mime(from_name) or from_addr or "",
        "published_at": published,
        "tags": [],
        "attachments": attachments,
        "origin": "\n".join(other_headers),
    }


# --------------------------------------------------------------------------
# 示例数据
# --------------------------------------------------------------------------

DEMO_DATA = [
    {
        "ext_id": "demo-1", "source": "wecom", "source_name": "运维值班群",
        "title": "【重要】9月18日 广州同城 A-3 机房核心交换机版本升级",
        "content": "各位同事：\n\n计划于 9 月 18 日 00:00-04:00 对广州同城 A-3 机房两台核心交换机做版本升级，"
                   "期间该机房下所有业务会产生 3 次以内、每次不超过 90 秒的中断。\n\n"
                   "影响范围：A-3 机房接入的全部 CSE 客户。\n"
                   "应急联系人：Alex Chen / 1380xxxxxxx\n"
                   "如客户侧有异议，请于 9 月 17 日 18:00 前反馈。",
        "category": "网络变更", "level": "urgent", "tags": ["割接", "机房", "核心交换机"],
        "publisher": "Alex Chen", "published_at": "2026-09-15 10:30:00", "pinned": 1,
    },
    {
        "ext_id": "demo-2", "source": "outlook", "sender": "it-security@example-corp.com",
        "source_name": "IT Security Announcements",
        "title": "FortiClient EMS 7.4 客户端强制升级通知",
        "content": "全体同事务必在 9 月 30 日前完成 FortiClient 升级到 7.4.x。\n\n"
                   "旧版本存在 CVE-2026-xxxx 证书校验绕过风险，未升级的终端将被 EMS 拒绝接入内网。\n"
                   "升级方式：打开 FortiClient → 帮助 → 检查更新，或从 IT 服务台下载安装包。",
        "category": "安全通告", "level": "important", "tags": ["FortiClient", "漏洞", "终端安全"],
        "publisher": "IT Security", "published_at": "2026-09-12 09:05:00",
    },
    {
        "ext_id": "demo-3", "source": "wecom", "source_name": "CDJS 客户服务群",
        "title": "CDJS 站点 009 带宽拥塞告警已恢复",
        "content": "站点 009（20M Cisco 9K）自 09-14 14:20 起的上行拥塞告警，"
                   "经与客户确认是新上线视频会议业务占满带宽所致，已于 09-14 17:40 恢复正常。\n\n"
                   "后续建议：为该站点配置 QoS 策略，将视频流量限速到 6Mbps 并保证优先级。",
        "category": "故障公告", "level": "important", "tags": ["CDJS", "QoS", "带宽"],
        "publisher": "Alex Chen", "published_at": "2026-09-14 18:00:00",
    },
    {
        "ext_id": "demo-4", "source": "outlook", "sender": "hr@example-corp.com",
        "source_name": "HR Notification",
        "title": "2026 年中秋、国庆假期值班安排",
        "content": "中秋 9 月 25 日 - 27 日，国庆 10 月 1 日 - 7 日。\n\n"
                   "两节期间 CSE 值班按节假日排班执行，白班 3 人（C 班）、夜班 2 人（Y 班）。\n"
                   "请各值班同事在 9 月 20 日前确认自己的班次，如需调班请提前申请。",
        "category": "人事行政", "level": "normal", "tags": ["值班", "假期", "排班"],
        "publisher": "HR", "published_at": "2026-09-10 11:20:00",
    },
    {
        "ext_id": "demo-5", "source": "wecom", "source_name": "安全通告群",
        "title": "VeloCloud VCO 控制台维护窗口通知",
        "content": "VeloCloud 官方将于 9 月 20 日 02:00-05:00（UTC+8）对 VCO 控制台做版本维护，"
                   "期间无法通过 VCO 下发策略，但不影响既有隧道转发。\n\n"
                   "如需在窗口内调整 SD-WAN 策略，请提前或顺延。",
        "category": "系统维护", "level": "normal", "tags": ["VeloCloud", "SD-WAN", "VCO"],
        "publisher": "NOC", "published_at": "2026-09-09 16:45:00",
    },
    {
        "ext_id": "demo-6", "source": "manual",
        "title": "内部流程：重要通知归档规范（试行）",
        "content": "从即日起，企微群与邮件中的重要通知统一归档到本系统。\n\n"
                   "归档要求：\n1. 标题保留原始前缀（如【重要】【紧急】）；\n"
                   "2. 必须选择分类与级别；\n3. 涉及客户的通知需填写发布人。",
        "category": "业务通知", "level": "normal", "tags": ["规范", "流程"],
        "publisher": "Alex Chen", "published_at": "2026-09-08 09:00:00",
    },
]


def seed_demo() -> int:
    conn = get_db()
    n = 0
    for item in DEMO_DATA:
        row = build_notice_row(item)
        _, created = upsert_notice(conn, row)
        n += 1 if created else 0
    conn.commit()
    return n


# --------------------------------------------------------------------------
# 内网导航
# --------------------------------------------------------------------------

NAV_VISIBILITIES = ("guest", "member", "admin")
# 打开方式：web 浏览器新标签 | winbox 本地 Winbox 4.x | custom 自定义协议
NAV_LAUNCHES = ("web", "winbox", "custom")
NAV_VIS_LABELS = {"guest": "免登录可见", "member": "需登录可见", "admin": "仅管理员可见"}

# 种子数据里的主机名。真实值放 .env 的 NAV_HOST（不入版本库）
NAV_HOST = os.environ.get("NAV_HOST", "127.0.0.1")

# 在线探测：默认只允许探测内网地址，避免把本服务当成任意主机端口扫描器
NAV_PROBE_ALLOW_PUBLIC = os.environ.get("NAV_PROBE_ALLOW_PUBLIC", "0").strip() in ("1", "true", "on")
NAV_PROBE_INTERVAL = int(os.environ.get("NAV_PROBE_INTERVAL", "60"))
# 探测并发数与单目标超时。客户站点有 200+ 个，串行探一轮要几分钟，
# 并发之后一轮只要几十秒，而且 /api/nav/probe 这个同步接口也不会把请求线程占死。
NAV_PROBE_WORKERS = max(1, int(os.environ.get("NAV_PROBE_WORKERS", "24")))
NAV_PROBE_TIMEOUT = float(os.environ.get("NAV_PROBE_TIMEOUT", "2.5"))


def _nav_seed() -> list:
    H = NAV_HOST
    return [
        ("网络运维", [
            ("配置生成器", f"http://{H}:8088", "多厂商设备配置生成与自检", ["网络", "配置"], "#378ADD"),
            ("Pro 工作台", f"http://{H}:9898", "真机 baseline 驱动的配置生成", ["网络", "配置"], "#7F77DD"),
            ("重要通知中心", f"http://{H}:8091/#/notices", "企微与邮件重要通知归档检索", ["通知", "归档"], "#1D9E75"),
        ]),
        ("监控告警", [
            ("Zabbix", f"http://{H}/zabbix/", "主机与链路告警", ["告警", "监控"], "#D85A30"),
            ("Grafana", f"http://{H}:3000", "监控大屏与看板", ["监控", "看板"], "#EF9F27"),
            ("Prometheus", f"http://{H}:9090", "指标采集与查询", ["监控", "指标"], "#BA7517"),
            ("NetFlow 流量分析", f"http://{H}:8124", "流量采样与查询", ["流量", "分析"], "#185FA5"),
        ]),
        ("排班管理", [
            ("CSE 排班系统", f"http://{H}:3005", "工程师轮班排布 v3.7", ["排班", "CSE"], "#639922"),
            ("7×24 自动排班", f"http://{H}:3006", "CP-SAT 自动求解", ["排班", "自动"], "#3B6D11"),
            ("旧排班系统", f"http://{H}:9999", "历史排班数据查询", ["排班", "历史"], "#5F5E5A"),
            ("排班管理", f"http://{H}:9000", "排班应用入口", ["排班"], "#888780"),
        ]),
        ("效率与安全", [
            ("n8n", f"http://{H}:5678", "流程自动化编排", ["自动化", "流程"], "#993556"),
            ("Vaultwarden", f"http://{H}:8787", "团队密码保险箱", ["密码", "安全"], "#534AB7"),
            ("1Panel", f"http://{H}:16666", "服务器运维面板", ["运维", "面板"], "#0F6E56"),
            ("短视频生成", f"http://{H}:10588", "Toonflow 文本转视频", ["AI", "视频"], "#D4537E"),
        ]),
    ]


def seed_nav() -> int:
    """nav_groups 为空时灌入种子导航数据（不覆盖已有数据）。"""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) AS c FROM nav_groups").fetchone()["c"] > 0:
        return 0
    ts = now_str()
    n = 0
    for gi, (gname, items) in enumerate(_nav_seed(), start=1):
        cur = conn.execute(
            "INSERT INTO nav_groups(name, sort, created_at) VALUES(?,?,?)", (gname, gi, ts))
        gid = cur.lastrowid
        for si, (name, url, desc, tags, color) in enumerate(items, start=1):
            conn.execute(
                """INSERT INTO nav_items(group_id, name, url, description, tags, color, sort,
                     visible_to, probe, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,'guest',1,?,?)""",
                (gid, name, url, desc, json.dumps(tags, ensure_ascii=False), color, si, ts, ts))
            n += 1
    log(f"已灌入导航种子数据：{len(_nav_seed())} 个分组 / {n} 个系统")
    return n


# ---- 在线探测 ----

NAV_STATUS = {}
NAV_STATUS_LOCK = threading.Lock()
NAV_STATUS_CHECKED = ""


def parse_target(url: str):
    s = (url or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        p = urllib.parse.urlparse(s)
    except ValueError:
        return None
    host = p.hostname
    if not host:
        return None
    port = p.port
    if port is None:
        port = 443 if p.scheme == "https" else 21 if p.scheme == "ftp" else 80
    return host, port


def _parse_ip(text: str):
    """把字符串当 IP 解析；带 :port 或 [v6]:port 也能处理。"""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("["):
        t = t[1:t.find("]")] if "]" in t else t[1:]
    elif t.count(":") == 1:
        t = t.split(":", 1)[0]
    try:
        return ipaddress.ip_address(t)
    except ValueError:
        return None


def is_private_host(host: str) -> bool:
    """是否内网地址（决定要不要探测）。

    这里必须用 ipaddress 判定，不能像以前那样写字符串前缀。旧写法是
    startswith("10.") / "192.168." / "127." 加正则匹配 172.16-31，会漏掉
    198.18.0.0/15 —— 而**本项目 235 个客户站点全部落在 198.19.x.x 段**。
    漏判的后果不是报错，而是这些站点被静默跳过、永远显示「未探测」。
    ipaddress 的 is_private 覆盖全部保留段（10/8、172.16/12、192.168/16、
    198.18/15、fc00::/7 等），一次到位。
    """
    ip = _parse_ip(host)
    if ip is None:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def probe_url(url: str, timeout: float = None) -> dict:
    """TCP 连通性探测。用 TCP 而不是 HTTP：对 FTP、HTTPS、非 HTTP 服务一样有效，
    也不会被 401/302 之类的状态码误判为离线。"""
    if timeout is None:
        timeout = NAV_PROBE_TIMEOUT
    target = parse_target(url)
    if not target:
        return {"ok": None, "reason": "地址无法解析"}
    host, port = target
    if not NAV_PROBE_ALLOW_PUBLIC and not is_private_host(host):
        return {"ok": None, "reason": "非内网地址，已跳过探测"}
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except OSError as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "reason": (str(e) or e.__class__.__name__)[:90]}


def _probe_summary(result: dict) -> str:
    """给日志用的一行摘要。"""
    up = sum(1 for r in result.values() if r.get("ok") is True)
    down = sum(1 for r in result.values() if r.get("ok") is False)
    skip = sum(1 for r in result.values() if r.get("ok") is None)
    return f"在线 {up} / 离线 {down} / 跳过 {skip}"


def run_nav_probe() -> int:
    """并发探测所有开启探测的条目。

    串行探 200+ 个目标是撑不住的：单目标超时 2.5 秒，最坏情况一轮要几分钟，
    期间 /api/nav/probe 这个同步接口会把请求线程一直占着。
    """
    global NAV_STATUS_CHECKED
    rows = get_db().execute("SELECT id, url FROM nav_items WHERE probe=1").fetchall()
    if not rows:
        with NAV_STATUS_LOCK:
            NAV_STATUS.clear()
            NAV_STATUS_CHECKED = now_str()
        return 0
    result = {}
    workers = min(NAV_PROBE_WORKERS, len(rows))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="navprobe") as pool:
        futs = {pool.submit(probe_url, r["url"]): r["id"] for r in rows}
        for fut, item_id in futs.items():
            try:
                result[item_id] = fut.result()
            except Exception as e:  # noqa: BLE001 — 单个目标出错不该拖垮整轮
                result[item_id] = {"ok": False, "reason": ("探测异常：" + repr(e))[:90]}
    with NAV_STATUS_LOCK:
        NAV_STATUS.clear()
        NAV_STATUS.update(result)
        NAV_STATUS_CHECKED = now_str()
    return len(result)


def nav_probe_loop() -> None:
    """后台探测循环。

    只在**状态真的变化**时打日志。以前每轮都打一行「导航探测完成：N 个目标」，
    一天就是 1440 行噪音，把有用的日志全淹了。
    """
    prev_sig = None
    logged_first = False
    while True:
        try:
            cnt = run_nav_probe()
            with NAV_STATUS_LOCK:
                snapshot = dict(NAV_STATUS)
            # 用「每个目标的在线状态」做指纹，只有它变了才值得记一笔
            sig = tuple(sorted((k, v.get("ok")) for k, v in snapshot.items()))
            summary = _probe_summary(snapshot)
            if not logged_first:
                log(f"导航探测完成：{cnt} 个目标（{summary}）")
                logged_first = True
            elif sig != prev_sig:
                log(f"导航探测状态变化：{summary}（共 {cnt} 个目标）")
            prev_sig = sig
        except Exception as e:  # noqa: BLE001
            log("导航探测异常：", repr(e))
        time.sleep(max(15, NAV_PROBE_INTERVAL))


# ---- 导航读取 ----

def allowed_visibilities(user) -> set:
    """免登录（guest）是所有访问者都能看到的；登录后再放开 member，管理员再放开 admin。"""
    out = {"guest"}
    if user:
        out.add("member")
        if user["role"] == ROLE_ADMIN:
            out.add("admin")
    return out


def nav_public_status() -> dict:
    with NAV_STATUS_LOCK:
        return dict(NAV_STATUS), NAV_STATUS_CHECKED


_TAGS_MEMO: dict = {}


def parse_json_list(raw) -> list:
    """nav_items.tags 存的是 JSON 文本。

    每个请求都把全部条目重新解析一遍纯属浪费（profile 显示 272 行的
    json.loads 约占 nav_list 四分之一耗时），按原文 memo 即可——
    文本相同解析结果必然相同。返回副本，避免调用方改动污染缓存。
    """
    key = raw or "[]"
    hit = _TAGS_MEMO.get(key)
    if hit is None:
        try:
            v = json.loads(key)
        except (ValueError, TypeError):
            v = []
        if not isinstance(v, list):
            v = []
        if len(_TAGS_MEMO) > 4096:      # 防御性上限，正常远达不到
            _TAGS_MEMO.clear()
        _TAGS_MEMO[key] = hit = v
    return list(hit)


def nav_list(user) -> dict:
    allowed = allowed_visibilities(user)
    conn = get_db()
    groups = [dict(r) for r in conn.execute(
        "SELECT id, name, sort FROM nav_groups ORDER BY sort, id").fetchall()]
    status, checked = nav_public_status()

    items = []
    for r in conn.execute(
            "SELECT * FROM nav_items ORDER BY favorite DESC, sort, id").fetchall():
        d = dict(r)
        if d["visible_to"] not in allowed:
            continue
        d["tags"] = parse_json_list(d["tags"])
        d["status"] = status.get(d["id"]) or {"ok": None}
        d["vis_label"] = NAV_VIS_LABELS.get(d["visible_to"], d["visible_to"])
        items.append(d)

    # 一次分组：原实现对每个分组都把 items 全量过一遍，是 O(分组数×条目数)
    by_group = {}
    for i in items:
        by_group.setdefault(i["group_id"], []).append(i)

    gids = {g["id"] for g in groups}
    for g in groups:
        g["items"] = by_group.get(g["id"], [])
    orphans = [i for i in items if i["group_id"] not in gids]
    if orphans:
        groups.append({"id": 0, "name": "未分组", "sort": 9999, "items": orphans})

    recent = sorted([i for i in items if i.get("last_visit")],
                    key=lambda x: x["last_visit"], reverse=True)[:6]
    return {
        "groups": groups,
        "recent": recent,
        "checked_at": checked,
        "total": len(items),
        "online": sum(1 for i in items if i["status"].get("ok") is True),
        "offline": sum(1 for i in items if i["status"].get("ok") is False),
        "visibilities": [{"value": v, "label": NAV_VIS_LABELS[v]} for v in NAV_VISIBILITIES],
        "can_edit": bool(user and user["role"] == ROLE_ADMIN),
    }


def build_nav_item(body: dict, existing=None) -> dict:
    cur = dict(existing) if existing else {}

    def pick(key, default):
        return body[key] if key in body else cur.get(key, default)

    def to_int(key, default=0):
        try:
            return int(pick(key, default) or 0)
        except (TypeError, ValueError):
            return default

    def to_bool(key, default=0):
        return 1 if str(pick(key, default)).strip().lower() in ("1", "true", "on", "是") else 0

    name = clean_text(pick("name", ""))
    if not name:
        raise ValueError("名称不能为空")
    url = clean_text(pick("url", ""))
    if url and not re.match(r"^(https?|ftp)://", url, re.I):
        url = "http://" + url
    vis = clean_text(pick("visible_to", "guest")) or "guest"
    if vis not in NAV_VISIBILITIES:
        vis = "guest"
    launch = (clean_text(pick("launch", "web")) or "web").lower()
    if launch not in NAV_LAUNCHES:
        launch = "web"
    launch_host = clean_text(pick("launch_host", ""))[:200]
    launch_arg = clean_text(pick("launch_arg", ""))[:400]
    # Winbox 靠设备地址调起本地应用；两者都空就没有可打开的目标
    if launch == "winbox" and not launch_host and not url:
        raise ValueError("打开方式选 Winbox 时，必须填「设备地址」（如 192.168.88.1）")
    return {
        "group_id": to_int("group_id"),
        "name": name[:60],
        "subgroup": clean_text(pick("subgroup", ""))[:40],
        "url": url[:400],
        "description": clean_text(pick("description", ""))[:300],
        "tags": json.dumps(parse_tags(pick("tags", []))[:10], ensure_ascii=False),
        "icon": clean_text(pick("icon", ""))[:4],
        "color": (clean_text(pick("color", "#3b82f6")) or "#3b82f6")[:16],
        "sort": to_int("sort"),
        "visible_to": vis,
        "probe": to_bool("probe", 1),
        "favorite": to_bool("favorite", 0),
        "launch": launch,
        "launch_host": launch_host,
        "launch_arg": launch_arg,
    }


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------

PUBLIC_PATHS = {"/api/login", "/api/health", "/api/ingest"}

# 仅管理员可用的接口：(方法, 精确路径或正则)
ADMIN_ROUTES = [
    ("POST", "/api/notices"),
    ("PUT", r"^/api/notices/\d+$"),
    ("DELETE", r"^/api/notices/\d+$"),
    ("POST", "/api/import"),
    ("POST", "/api/upload"),
    ("POST", "/api/seed-demo"),
    ("POST", "/api/clear"),
    ("POST", "/api/categories"),
    ("DELETE", "/api/categories"),
    ("GET", "/api/sync/sources"),
    ("POST", "/api/sync/sources"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("PUT", r"^/api/users/\d+$"),
    ("DELETE", r"^/api/users/\d+$"),
    ("GET", "/api/audit"),
    ("POST", "/api/nav/items"),
    ("PUT", r"^/api/nav/items/\d+$"),
    ("DELETE", r"^/api/nav/items/\d+$"),
    ("POST", "/api/nav/groups"),
    ("PUT", r"^/api/nav/groups/\d+$"),
    ("DELETE", r"^/api/nav/groups/\d+$"),
    ("POST", "/api/nav/probe"),
]


def needs_admin(method: str, path: str) -> bool:
    for m, p in ADMIN_ROUTES:
        if m != method:
            continue
        if p == path or (p.startswith("^") and re.match(p, path)):
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "NoticeHub/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------
    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _compress(self, body: bytes, ctype: str, extra: dict) -> tuple:
        """按需 gzip，返回 (body, extra)。

        只对文本类、且超过阈值的响应动手；压不小就原样返回，
        避免为了压缩反而多传字节。HEAD 也走这里，这样
        Content-Length 与对应 GET 的编码后长度一致。
        """
        if (len(body) < GZIP_MIN_BYTES
                or not self._accepts_gzip()
                or "Content-Encoding" in (extra or {})
                or not ctype.lower().startswith(GZIP_TYPES)):
            return body, extra
        packed = gzip.compress(body, 5)
        if len(packed) >= len(body):
            return body, extra
        out = dict(extra or {})
        out["Content-Encoding"] = "gzip"
        # 同一个 URL 按 Accept-Encoding 有不同表示，必须声明，否则中间缓存会串味
        out["Vary"] = "Accept-Encoding"
        return packed, out

    def _send(self, code: int, body: bytes, ctype: str, extra: dict = None) -> None:
        body, extra = self._compress(body, ctype, extra)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code: int = 200, extra: dict = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _err(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _redirect(self, location: str, extra: dict = None) -> None:
        """303 See Other，用于原生表单提交后的跳转。"""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    # ---------------- 登录 ----------------
    def _login(self):
        """
        同时支持两种提交方式：
        - application/json（前端 fetch / 采集器）
        - application/x-www-form-urlencoded（浏览器原生表单提交）

        原生表单那条路是刻意保留的：只有真正走浏览器原生提交，
        密码管理器（Chrome/Edge/1Password 等）才会提示保存账号密码。
        用 fetch 提交时浏览器基本不会弹「保存密码」。
        """
        ctype = (self.headers.get("Content-Type") or "").lower()
        native_form = "application/x-www-form-urlencoded" in ctype

        if native_form:
            raw = self._read_body().decode("utf-8", "replace")
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        else:
            fields = self._json_body()

        username = str(fields.get("username") or "").strip()
        pwd = str(fields.get("password") or "")
        row = get_user_by_name(username) if username else None
        ok = bool(row) and bool(row["enabled"]) and verify_password(
            pwd, row["pwd_salt"], row["pwd_hash"])
        if not ok:
            # 统一提示，不暴露账号是否存在；失败时略作延迟，抑制暴力枚举
            log("login failed:", username or "(空)", "from", self.client_address[0])
            time.sleep(0.4)
            if native_form:
                return self._redirect("/#login-error")
            return self._err(401, "账号或密码不正确")

        conn = get_db()
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now_str(), row["id"]))
        conn.commit()
        audit("login", row["username"], f"ip={self.client_address[0]}")

        token, _ = issue_session(row["username"], row["pwd_hash"])
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                  f"Max-Age={SESSION_TTL}")

        if native_form:
            # 跳回首页，并把令牌放进 URL fragment 交给前端存到 localStorage。
            # 用 fragment 而不是 query：fragment 不会发给服务端，也不会出现在 Referer 里。
            return self._redirect("/#token=" + token, extra={"Set-Cookie": cookie})

        return self._json(
            {"ok": True, "token": token, "user": public_user(get_user_by_id(row["id"]))},
            extra={"Set-Cookie": cookie})

    def _ok(self, **kw) -> None:
        payload = {"ok": True}
        payload.update(kw)
        self._json(payload)

    def _cookies(self) -> dict:
        out = {}
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _current_user(self):
        """返回当前登录用户行；未登录或会话失效返回 None（每请求只查一次）。

        令牌来源按优先级：Cookie → X-Session-Token 头 → Authorization: Bearer。
        多一条头部通道是为了兼容**浏览器上下文不保存 cookie** 的场景
        （内嵌 webview / 预览面板 / 被代理剥离 cookie），否则会出现
        「登录成功但下一个请求立刻 401」的循环。
        """
        cached = getattr(self, "_user_cache", None)
        if cached is not None:
            return cached if cached != "MISS" else None

        cookie_tok = self._cookies().get(SESSION_COOKIE, "")
        header_tok = (self.headers.get("X-Session-Token") or "").strip()
        auth = self.headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""

        row, note = None, []
        if cookie_tok:
            row = decode_session(cookie_tok)
            note.append("cookie=" + ("有效" if row else "无效"))
        else:
            note.append("cookie=无")
        if row is None and header_tok:
            row = decode_session(header_tok)
            note.append("X-Session-Token=" + ("有效" if row else "无效"))
        if row is None and bearer:
            row = decode_session(bearer)
            note.append("Bearer=" + ("有效" if row else "无效"))

        self._auth_note = " ".join(note)
        self._user_cache = row if row is not None else "MISS"
        return row

    def _authed(self) -> bool:
        return self._current_user() is not None

    def _is_admin(self) -> bool:
        u = self._current_user()
        return bool(u and u["role"] == ROLE_ADMIN)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_UPLOAD:
            raise ValueError("请求体超过 64MB 上限")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {"items": obj}
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):
        pass  # 静音默认访问日志

    # ---------------- 路由 ----------------
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        # ⚠️ 必须在每个请求开始时重置认证缓存。
        # BaseHTTPRequestHandler 在 HTTP/1.1 keep-alive 下会用**同一个 handler 实例**
        # 连续处理同一连接上的多个请求；如果不重置，第二个请求会直接命中上一个请求
        # 留下的 `_user_cache='MISS'`，导致「明明带了令牌却仍被判为未登录」——
        # 表现就是登录成功、下一个请求立刻 401，且日志里显示请求头根本没被读过。
        self._user_cache = None
        self._auth_note = ""
        parsed = urllib.parse.urlparse(self.path)
        # unquote：浏览器会把中文/空格等百分号编码后发过来，不解码就永远匹配不到文件
        path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            self._dispatch(method, path, params)
        except ValueError as e:
            self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            log("ERROR", method, path, repr(e))
            self._err(500, f"服务器内部错误：{e}")

    def _dispatch(self, method: str, path: str, params: dict):
        # 上传的附件（data/uploads/，需要登录）
        if path.startswith("/uploads/"):
            return self._serve_upload(path)

        # 静态资源
        if method == "GET" and not path.startswith("/api/"):
            return self._serve_static(path)

        if path == "/api/health":
            return self._json({"ok": True, "service": "notice-hub", "time": now_str()})

        if path == "/api/login" and method == "POST":
            return self._login()

        if path == "/api/logout" and method == "POST":
            return self._json(
                {"ok": True},
                extra={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"},
            )

        # 采集器专用：令牌鉴权，不走会话
        if path == "/api/ingest" and method == "POST":
            tok = self.headers.get("X-Sync-Token") or ""
            if not tok or not hmac.compare_digest(tok, effective_sync_token()):
                return self._err(401, "同步令牌无效")
            body = self._json_body()
            items = body.get("items") if isinstance(body.get("items"), list) else []
            return self._ingest(items)

        # 内网导航：读取与访问计数免登录（导航页本身就是公开入口）
        if path == "/api/nav" and method == "GET":
            return self._json({"ok": True, **nav_list(self._current_user())})
        if path == "/api/nav/visit" and method == "POST":
            return self._nav_visit()

        # 其余接口需登录（记下令牌来源，便于排查「登录了却一直 401」）
        if not self._authed():
            log("401", method, path, "|", getattr(self, "_auth_note", "?"),
                "| from", self.client_address[0])
            return self._err(401, "未登录或会话已过期")

        # 写操作 / 账号管理仅管理员
        if needs_admin(method, path) and not self._is_admin():
            return self._err(403, "当前账号是查看权限，只能浏览和搜索")

        if path == "/api/session" and method == "GET":
            return self._json({"ok": True, "authed": True,
                               "user": public_user(self._current_user())})

        if path == "/api/me/password" and method == "POST":
            return self._change_own_password()

        # ---- 账号管理（接口本身已在 ADMIN_ROUTES 里限管理员） ----
        if path == "/api/users" and method == "GET":
            return self._json({
                "ok": True, "items": list_users(),
                "roles": [{"value": k, "label": v} for k, v in ROLE_LABELS.items()],
                "me": public_user(self._current_user()),
            })
        if path == "/api/users" and method == "POST":
            return self._create_user_route()
        if path == "/api/audit" and method == "GET":
            rows = get_db().execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
            return self._json({"ok": True, "items": [dict(r) for r in rows]})
        if path == "/api/notices" and method == "GET":
            return self._json({"ok": True, **query_notices(params)})
        if path == "/api/notices" and method == "POST":
            return self._create_one()
        if path == "/api/categories" and method == "GET":
            return self._json({"ok": True, "items": list_categories()})
        if path == "/api/categories" and method == "POST":
            body = self._json_body()
            name = clean_text(body.get("name"))
            if not name:
                return self._err(400, "分类名不能为空")
            conn = get_db()
            ensure_category(conn, name, clean_text(body.get("color")) or None)
            conn.commit()
            return self._json({"ok": True, "items": list_categories()})
        if path == "/api/categories" and method == "DELETE":
            name = clean_text(params.get("name"))
            if not name or name == "未分类":
                return self._err(400, "该分类不可删除")
            conn = get_db()
            conn.execute("UPDATE notices SET category='未分类' WHERE category=?", (name,))
            conn.execute("DELETE FROM categories WHERE name=?", (name,))
            conn.commit()
            return self._json({"ok": True, "items": list_categories()})
        if path == "/api/stats" and method == "GET":
            return self._json({"ok": True, **stats()})
        if path == "/api/export" and method == "GET":
            return self._export(params)
        if path == "/api/upload" and method == "POST":
            return self._upload()
        if path == "/api/import" and method == "POST":
            return self._import(params)
        if path == "/api/seed-demo" and method == "POST":
            return self._json({"ok": True, "created": seed_demo()})
        if path == "/api/clear" and method == "POST":
            body = self._json_body()
            scope = clean_text(body.get("scope")) or "demo"
            return self._json({"ok": True, "deleted": delete_expired(scope)})
        if path == "/api/sync/sources" and method == "GET":
            conn = get_db()
            rows = conn.execute("SELECT * FROM sync_sources ORDER BY id").fetchall()
            return self._json(
                {
                    "ok": True,
                    "items": [dict(r) for r in rows],
                    "endpoint": "/api/ingest",
                    "token": effective_sync_token(),
                }
            )
        if path == "/api/sync/sources" and method == "POST":
            body = self._json_body()
            conn = get_db()
            conn.execute(
                "INSERT INTO sync_sources(type, name, config, enabled, created_at) VALUES(?,?,?,?,?)",
                (clean_text(body.get("type")) or "imap", clean_text(body.get("name")) or "未命名",
                 json.dumps(body.get("config") or {}, ensure_ascii=False),
                 1 if body.get("enabled") else 0, now_str()),
            )
            conn.commit()
            return self._ok()

        # ---- 导航维护（接口本身已在 ADMIN_ROUTES 里限管理员） ----
        if path == "/api/nav/items" and method == "POST":
            return self._nav_item_save(None)
        if path == "/api/nav/groups" and method == "POST":
            return self._nav_group_save(None)
        if path == "/api/nav/probe" and method == "POST":
            n = run_nav_probe()
            return self._json({"ok": True, "probed": n, "checked_at": NAV_STATUS_CHECKED})

        mn = re.match(r"^/api/nav/items/(\d+)$", path)
        if mn:
            iid = int(mn.group(1))
            if method == "PUT":
                return self._nav_item_save(iid)
            if method == "DELETE":
                conn = get_db()
                row = conn.execute("SELECT name FROM nav_items WHERE id=?", (iid,)).fetchone()
                if not row:
                    return self._err(404, "导航项不存在")
                conn.execute("DELETE FROM nav_items WHERE id=?", (iid,))
                audit("nav_item_delete", row["name"])
                return self._json({"ok": True, **nav_list(self._current_user())})

        mg = re.match(r"^/api/nav/groups/(\d+)$", path)
        if mg:
            gid = int(mg.group(1))
            if method == "PUT":
                return self._nav_group_save(gid)
            if method == "DELETE":
                return self._nav_group_delete(gid)

        mu = re.match(r"^/api/users/(\d+)$", path)
        if mu:
            uid = int(mu.group(1))
            target = get_user_by_id(uid)
            if not target:
                return self._err(404, "账号不存在")
            if method == "PUT":
                return self._update_user(uid, target)
            if method == "DELETE":
                me = self._current_user()
                if uid == me["id"]:
                    return self._err(400, "不能删除自己的账号")
                if target["role"] == ROLE_ADMIN and target["enabled"] \
                        and count_enabled_admins(exclude_id=uid) == 0:
                    return self._err(400, "至少要保留一个启用的管理员")
                conn = get_db()
                conn.execute("DELETE FROM users WHERE id=?", (uid,))
                conn.commit()
                audit("user_delete", target["username"])
                return self._json({"ok": True, "items": list_users()})

        m = re.match(r"^/api/notices/(\d+)$", path)
        if m:
            nid = int(m.group(1))
            if method == "GET":
                conn = get_db()
                r = conn.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
                if not r:
                    return self._err(404, "通知不存在")
                return self._json({"ok": True, "item": row_to_dict(r)})
            if method in ("PUT", "POST"):
                return self._update_one(nid)
            if method == "DELETE":
                conn = get_db()
                conn.execute("DELETE FROM notices WHERE id=?", (nid,))
                conn.commit()
                audit("delete", str(nid))
                return self._ok()

        return self._err(404, f"未知接口 {method} {path}")

    # ---------------- 静态文件 ----------------
    def _not_modified(self, etag: str, mtime: float) -> bool:
        """浏览器带上 If-None-Match / If-Modified-Since 时判断能否回 304。"""
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm:
            return inm == etag or inm == "*"
        ims = (self.headers.get("If-Modified-Since") or "").strip()
        if not ims:
            return False
        try:
            since = parsedate_to_datetime(ims).timestamp()
        except (TypeError, ValueError, OverflowError):
            return False
        return int(mtime) <= int(since)

    def _serve_static(self, path: str):
        # 两个独立页面：/ 导航中心，/notices 重要通知中心（各自可加书签）
        if path in ("/", "/index.html"):
            rel = "index.html"
        elif path in ("/notices", "/notices.html"):
            rel = "notices.html"
        else:
            rel = path.lstrip("/")
        if rel.endswith("favicon.ico"):
            return self._send(204, b"", "image/x-icon")
        full = os.path.normpath(os.path.join(PUBLIC_DIR, rel))
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            return self._err(404, "文件不存在")

        st = os.stat(full)
        # 用纳秒 mtime + 体积做校验符：同秒内改文件也不会误判成没变
        etag = f'"{st.st_mtime_ns}-{st.st_size}"'
        last_mod = formatdate(st.st_mtime, usegmt=True)

        # 原来这里是 no-store，导致 139KB 的 index.html 每次访问都完整重下。
        # 改成 no-cache（每次带校验符来问，没变就回 304 空响应），
        # 页面改了也能立刻生效，不会拿到旧版本。
        if self._not_modified(etag, st.st_mtime):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        ctype = mimetypes_guess(full)
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype, {
            "ETag": etag,
            "Last-Modified": last_mod,
            "Cache-Control": "no-cache",
        })

    # ---------------- 上传的附件（data/uploads/） ----------------
    def _serve_upload(self, path: str):
        """粘贴/上传进来的图片和文档。需要登录：里面可能是客户截图、设备拓扑。"""
        if not self._authed():
            return self._err(401, "未登录或会话已过期")
        rel = os.path.basename(path[len("/uploads/"):])          # basename 直接挡掉目录穿越
        full = os.path.join(upload_dir(), rel)
        if not rel or not os.path.isfile(full):
            return self._err(404, "文件不存在")
        with open(full, "rb") as f:
            data = f.read()
        # 实体名是内容哈希，内容永不变化 → 可以放心让浏览器缓存
        self._send(200, data, mimetypes_guess(full),
                   {"Cache-Control": "private, max-age=86400"})

    # ---------------- 附件上传（返回 URL，供粘贴时插到正文里） ----------------
    def _upload(self):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "multipart/form-data" not in ctype:
            return self._err(400, "请用 multipart/form-data 上传")
        boundary = multipart_boundary(self.headers.get("Content-Type") or "")
        if not boundary:
            return self._err(400, "上传请求缺少 boundary")
        raw = self._read_body()
        if len(raw) > UPLOAD_MAX_TOTAL:
            return self._err(413, f"一次上传总量超限（上限 {UPLOAD_MAX_TOTAL // 1048576}MB）")
        _fields, files = parse_multipart(raw, boundary.encode())
        if not files:
            return self._err(400, "没有收到文件")
        items, errors = [], []
        for fn, payload in files:
            try:
                name, url, size = save_upload(fn, payload)
                items.append({"name": name, "url": url, "size": size,
                              "image": os.path.splitext(url)[1].lower() in IMAGE_EXTS})
            except ValueError as e:
                errors.append({"file": fn, "error": str(e)})
        if not items:
            return self._json({"ok": False, "error": errors[0]["error"] if errors else "上传失败",
                               "errors": errors}, 400)
        audit("upload", f"{len(items)} files", ", ".join(i["name"] for i in items)[:200])
        return self._json({"ok": True, "items": items, "errors": errors})

    # ---------------- 通知写操作 ----------------
    def _create_one(self):
        body = self._json_body()
        if not body:
            return self._err(400, "请求体为空")
        conn = get_db()
        row = build_notice_row(body)
        nid, created = upsert_notice(conn, row, update_existing=bool(body.get("update_existing")))
        conn.commit()
        audit("create" if created else "update", str(nid), row["title"][:80])
        r = conn.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
        return self._json({"ok": True, "created": created, "item": row_to_dict(r)})

    # ---------------- 内网导航 ----------------
    def _nav_visit(self):
        """记录一次跳转。导航页免登录，所以这个接口也免登录。"""
        body = self._json_body()
        try:
            iid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            iid = 0
        if not iid:
            return self._err(400, "缺少 id")
        conn = get_db()
        row = conn.execute("SELECT id, visible_to FROM nav_items WHERE id=?", (iid,)).fetchone()
        if not row or row["visible_to"] not in allowed_visibilities(self._current_user()):
            return self._err(404, "导航项不存在")
        conn.execute("UPDATE nav_items SET visit_count=visit_count+1, last_visit=? WHERE id=?",
                     (now_str(), iid))
        return self._json({"ok": True})

    def _nav_item_save(self, item_id):
        body = self._json_body()
        conn = get_db()
        move = clean_text(body.get("move") or "")
        if item_id and move in ("up", "down"):
            return self._nav_move(item_id, move)
        # 拖拽排序：reorder_after 是「要排到哪一项后面」，空值表示移到最前
        if item_id and "reorder_after" in body:
            return self._nav_reorder(item_id, body.get("reorder_after"))

        existing = None
        if item_id:
            existing = conn.execute("SELECT * FROM nav_items WHERE id=?", (item_id,)).fetchone()
            if not existing:
                return self._err(404, "导航项不存在")
        try:
            row = build_nav_item(body, existing)
        except ValueError as e:
            return self._err(400, str(e))

        if not row["group_id"]:
            first = conn.execute("SELECT id FROM nav_groups ORDER BY sort, id LIMIT 1").fetchone()
            if not first:
                return self._err(400, "请先创建一个分组")
            row["group_id"] = first["id"]

        ts = now_str()
        if item_id:
            conn.execute(
                """UPDATE nav_items SET group_id=?, name=?, url=?, description=?, subgroup=?,
                     tags=?, icon=?, color=?, sort=?, visible_to=?, probe=?, favorite=?,
                     launch=?, launch_host=?, launch_arg=?, updated_at=?
                   WHERE id=?""",
                (row["group_id"], row["name"], row["url"], row["description"], row["subgroup"],
                 row["tags"], row["icon"], row["color"], row["sort"], row["visible_to"],
                 row["probe"], row["favorite"], row["launch"], row["launch_host"],
                 row["launch_arg"], ts, item_id))
            audit("nav_item_update", row["name"])
        else:
            if not row["sort"]:
                nxt = conn.execute(
                    "SELECT COALESCE(MAX(sort),0)+1 AS s FROM nav_items WHERE group_id=?",
                    (row["group_id"],)).fetchone()["s"]
                row["sort"] = nxt
            cur = conn.execute(
                """INSERT INTO nav_items(group_id, name, url, description, subgroup, tags, icon,
                     color, sort, visible_to, probe, favorite, launch, launch_host, launch_arg,
                     created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["group_id"], row["name"], row["url"], row["description"], row["subgroup"],
                 row["tags"], row["icon"], row["color"], row["sort"], row["visible_to"],
                 row["probe"], row["favorite"], row["launch"], row["launch_host"],
                 row["launch_arg"], ts, ts))
            item_id = cur.lastrowid
            audit("nav_item_create", row["name"])
        return self._json({"ok": True, "id": item_id, **nav_list(self._current_user())})

    def _nav_move(self, item_id, direction):
        conn = get_db()
        cur = conn.execute("SELECT id, group_id FROM nav_items WHERE id=?", (item_id,)).fetchone()
        if not cur:
            return self._err(404, "导航项不存在")
        sibs = conn.execute("SELECT id FROM nav_items WHERE group_id=? ORDER BY sort, id",
                            (cur["group_id"],)).fetchall()
        ids = [r["id"] for r in sibs]
        i = ids.index(item_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, nid in enumerate(ids, start=1):
                conn.execute("UPDATE nav_items SET sort=? WHERE id=?", (pos, nid))
        return self._json({"ok": True, **nav_list(self._current_user())})

    def _nav_group_save(self, group_id):
        body = self._json_body()
        move = clean_text(body.get("move") or "")
        if group_id and move in ("up", "down"):
            return self._nav_group_move(group_id, move)
        if group_id and "reorder_after" in body:
            return self._nav_group_reorder(group_id, body.get("reorder_after"))
        name = clean_text(body.get("name") or "")[:40]
        if not name:
            return self._err(400, "分组名不能为空")
        try:
            sort = int(body.get("sort") or 0)
        except (TypeError, ValueError):
            sort = 0
        conn = get_db()
        if group_id:
            if not conn.execute("SELECT id FROM nav_groups WHERE id=?", (group_id,)).fetchone():
                return self._err(404, "分组不存在")
            conn.execute("UPDATE nav_groups SET name=?, sort=? WHERE id=?",
                         (name, sort, group_id))
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO nav_groups(name, sort, created_at) VALUES(?,?,?)",
                    (name, sort or 99, now_str()))
            except sqlite3.IntegrityError:
                return self._err(400, "同名分组已存在")
            group_id = cur.lastrowid
        audit("nav_group_save", name)
        return self._json({"ok": True, "id": group_id, **nav_list(self._current_user())})

    def _nav_reorder(self, item_id, after_id):
        """拖拽排序：把 item_id 挪到 after_id 之后；after_id 为空则移到最前。

        与 _nav_move 的区别是「一次跳到任意位置」，而不是只跟邻居交换 ——
        拖拽正是这个语义。整组重新编号，保证 sort 始终是 1..n 的连续值。
        """
        conn = get_db()
        cur = conn.execute("SELECT id, group_id FROM nav_items WHERE id=?", (item_id,)).fetchone()
        if not cur:
            return self._err(404, "导航项不存在")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM nav_items WHERE group_id=? ORDER BY sort, id",
            (cur["group_id"],)).fetchall()]
        if after_id in (None, "", 0, "0"):
            after_id = None
        else:
            try:
                after_id = int(after_id)
            except (TypeError, ValueError):
                return self._err(400, "目标项无效")
            if after_id == item_id:
                return self._json({"ok": True, **nav_list(self._current_user())})  # 拖到自己身上：不动
        ids.remove(item_id)
        if after_id is None:
            ids.insert(0, item_id)
        else:
            if after_id not in ids:
                return self._err(400, "只能在本分组内拖动排序")
            ids.insert(ids.index(after_id) + 1, item_id)
        for pos, nid in enumerate(ids, start=1):
            conn.execute("UPDATE nav_items SET sort=? WHERE id=?", (pos, nid))
        audit("nav_item_reorder", str(item_id), "after=" + (str(after_id) if after_id else "top"))
        return self._json({"ok": True, **nav_list(self._current_user())})

    def _nav_group_move(self, group_id, direction):
        conn = get_db()
        if not conn.execute("SELECT id FROM nav_groups WHERE id=?", (group_id,)).fetchone():
            return self._err(404, "分组不存在")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM nav_groups ORDER BY sort, id").fetchall()]
        i = ids.index(group_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, gid in enumerate(ids, start=1):
                conn.execute("UPDATE nav_groups SET sort=? WHERE id=?", (pos, gid))
        return self._json({"ok": True, **nav_list(self._current_user())})

    def _nav_group_reorder(self, group_id, after_id):
        """拖拽排序分组：把 group_id 挪到 after_id 之后；after_id 为空则移到最前。"""
        conn = get_db()
        if not conn.execute("SELECT id FROM nav_groups WHERE id=?", (group_id,)).fetchone():
            return self._err(404, "分组不存在")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM nav_groups ORDER BY sort, id").fetchall()]
        if after_id in (None, "", 0, "0"):
            after_id = None
        else:
            try:
                after_id = int(after_id)
            except (TypeError, ValueError):
                return self._err(400, "目标分组无效")
            if after_id == group_id:
                return self._json({"ok": True, **nav_list(self._current_user())})
        ids.remove(group_id)
        if after_id is None:
            ids.insert(0, group_id)
        else:
            if after_id not in ids:
                return self._err(400, "目标分组不存在")
            ids.insert(ids.index(after_id) + 1, group_id)
        for pos, gid in enumerate(ids, start=1):
            conn.execute("UPDATE nav_groups SET sort=? WHERE id=?", (pos, gid))
        audit("nav_group_reorder", str(group_id), "after=" + (str(after_id) if after_id else "top"))
        return self._json({"ok": True, **nav_list(self._current_user())})

    def _nav_group_delete(self, group_id):
        conn = get_db()
        g = conn.execute("SELECT id, name FROM nav_groups WHERE id=?", (group_id,)).fetchone()
        if not g:
            return self._err(404, "分组不存在")
        conn.execute("DELETE FROM nav_items WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM nav_groups WHERE id=?", (group_id,))
        audit("nav_group_delete", g["name"])
        return self._json({"ok": True, **nav_list(self._current_user())})

    # ---------------- 账号管理 ----------------
    def _change_own_password(self):
        body = self._json_body()
        me = self._current_user()
        old = str(body.get("old_password") or "")
        new = str(body.get("new_password") or "")
        if not verify_password(old, me["pwd_salt"], me["pwd_hash"]):
            return self._err(400, "原密码不正确")
        if len(new) < 6:
            return self._err(400, "新密码至少 6 位")
        if new == old:
            return self._err(400, "新密码不能与原密码相同")
        set_password(me["id"], new)
        audit("change_password", me["username"])
        row = get_user_by_id(me["id"])
        token, _ = issue_session(row["username"], row["pwd_hash"])
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                  f"Max-Age={SESSION_TTL}")
        # 改密码会让旧令牌签名失效，这里把新令牌一并返回，供前端更新本地副本
        return self._json({"ok": True, "token": token, "user": public_user(row)},
                          extra={"Set-Cookie": cookie})

    def _create_user_route(self):
        body = self._json_body()
        me = self._current_user()
        try:
            uid = create_user(
                body.get("username") or "",
                str(body.get("password") or ""),
                body.get("role") or ROLE_VIEWER,
                clean_text(body.get("display_name")),
                created_by=me["username"],
            )
        except ValueError as e:
            return self._err(400, str(e))
        except sqlite3.IntegrityError:
            try:
                get_db().rollback()
            except Exception:
                pass
            return self._err(400, "该账号已存在")
        except sqlite3.Error as e:
            try:
                get_db().rollback()
            except Exception:
                pass
            return self._err(500, f"写入失败：{e}")
        audit("user_create", str(body.get("username")), f"role={body.get('role')}")
        return self._json({"ok": True, "items": list_users(), "id": uid})

    def _update_user(self, uid: int, target):
        body = self._json_body()
        me = self._current_user()
        conn = get_db()
        sets, args, changed = [], [], []

        new_role = body.get("role")
        if new_role in (ROLE_ADMIN, ROLE_VIEWER) and new_role != target["role"]:
            if uid == me["id"] and new_role != ROLE_ADMIN:
                return self._err(400, "不能把自己降级为查看账号")
            if target["role"] == ROLE_ADMIN and target["enabled"] \
                    and count_enabled_admins(exclude_id=uid) == 0:
                return self._err(400, "至少要保留一个启用的管理员")
            sets.append("role=?")
            args.append(new_role)
            changed.append("role")

        if "enabled" in body:
            new_enabled = 1 if str(body.get("enabled")).strip() in ("1", "true", "True", "on") else 0
            if uid == me["id"] and not new_enabled:
                return self._err(400, "不能停用自己的账号")
            if target["role"] == ROLE_ADMIN and target["enabled"] and not new_enabled \
                    and count_enabled_admins(exclude_id=uid) == 0:
                return self._err(400, "至少要保留一个启用的管理员")
            sets.append("enabled=?")
            args.append(new_enabled)
            changed.append("enabled")

        if "display_name" in body:
            sets.append("display_name=?")
            args.append(clean_text(body.get("display_name")))
            changed.append("display_name")

        new_pwd = body.get("password")
        if new_pwd:
            if len(str(new_pwd)) < 6:
                return self._err(400, "密码至少 6 位")
            salt, pwd_hash = hash_password(str(new_pwd))
            sets += ["pwd_salt=?", "pwd_hash=?", "weak_pwd=?"]
            args += [salt, pwd_hash, 1 if is_weak_password(str(new_pwd)) else 0]
            changed.append("password")

        if not sets:
            return self._err(400, "没有需要修改的字段")
        sets.append("updated_at=?")
        args.append(now_str())
        args.append(uid)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        audit("user_update", target["username"], ",".join(changed))
        return self._json({"ok": True, "items": list_users()})

    def _update_one(self, nid: int):
        body = self._json_body()
        conn = get_db()
        r = conn.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
        if not r:
            return self._err(404, "通知不存在")
        merged = dict(r)
        merged.update({k: v for k, v in body.items() if k not in ("id", "ext_id")})
        merged["ext_id"] = r["ext_id"]
        row = build_notice_row(merged)
        row["ext_id"] = r["ext_id"]
        upsert_notice(conn, row, update_existing=True)
        conn.commit()
        audit("update", str(nid), row["title"][:80])
        r2 = conn.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
        return self._json({"ok": True, "item": row_to_dict(r2)})

    def _ingest(self, items: list) -> None:
        if not items:
            return self._err(400, "items 不能为空")
        conn = get_db()
        created = updated = 0
        ids = []
        for it in items:
            if not isinstance(it, dict):
                continue
            row = build_notice_row(it)
            nid, isnew = upsert_notice(conn, row, update_existing=True)
            ids.append(nid)
            created += 1 if isnew else 0
            updated += 0 if isnew else 1
        conn.commit()
        audit("ingest", f"{len(ids)} items", f"new={created} upd={updated}")
        log(f"ingest: {created} new, {updated} updated")
        return self._json({"ok": True, "parsed": len(ids), "created": created,
                           "updated": updated, "ids": ids})

    # ---------------- 导入 ----------------
    def _import(self, params: dict):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "multipart/form-data" not in ctype:
            body = self._json_body()
            text = body.get("text") or body.get("content") or ""
            items = parse_pasted(text)
            if not items:
                return self._err(400, "没有解析到任何通知，请检查格式")
            apply_paste_attachments(items, body.get("attachments"))
            return self._ingest(items)

        boundary = multipart_boundary(self.headers.get("Content-Type") or "")
        if not boundary:
            return self._err(400, "上传请求缺少 boundary")
        raw = self._read_body()
        fields, files = parse_multipart(raw, boundary.encode())
        text_field = (fields.get("text") or "").strip()

        # 带 text 字段 = 「粘贴文本 + 附件」一起提交：文件存成附件，不去解析成通知
        if text_field:
            items = parse_pasted(text_field)
            if not items:
                return self._err(400, "没有解析到任何通知，请检查文本格式")
            stored = []
            for fn, payload in files:
                try:
                    name, url, _size = save_upload(fn, payload)
                    stored.append({"name": name, "url": url})
                except ValueError as e:
                    log("upload skipped:", fn, str(e))
            apply_paste_attachments(items, stored)
            return self._ingest(items)

        if not files:
            return self._err(400, "没有收到上传文件")

        collected, report = [], []
        for fn, content in files:
            low = fn.lower()
            try:
                if low.endswith(".eml"):
                    collected.append(parse_eml_bytes(content))
                    report.append({"file": fn, "parsed": 1, "kind": "eml"})
                elif low.endswith(".xlsx"):
                    items = parse_xlsx_bytes(content)
                    collected.extend(items)
                    report.append({"file": fn, "parsed": len(items), "kind": "xlsx"})
                elif low.endswith(".json"):
                    items = parse_json_bytes(content)
                    collected.extend(items)
                    report.append({"file": fn, "parsed": len(items), "kind": "json"})
                elif low.endswith(".csv"):
                    items = parse_csv_bytes(content)
                    collected.extend(items)
                    report.append({"file": fn, "parsed": len(items), "kind": "csv"})
                elif low.endswith((".txt", ".md", ".log", ".html", ".htm", ".text")) \
                        or not os.path.splitext(low)[1]:
                    for enc in ("utf-8-sig", "utf-8", "gb18030"):
                        try:
                            txt = content.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        txt = content.decode("utf-8", "replace")
                    items = parse_pasted(txt)
                    collected.extend(items)
                    report.append({"file": fn, "parsed": len(items), "kind": "text"})
                else:
                    # 以前这里会把任意未知后缀硬当文本解析 —— 传张 png 进来就会变成一条乱码通知
                    raise ValueError("这个后缀不是可导入的数据格式（只认 .eml/.xlsx/.csv/.json/.txt/.md）；"
                                     "图片和文档请到「粘贴文本」页作为附件一起贴")
            except Exception as e:  # noqa: BLE001
                report.append({"file": fn, "parsed": 0, "kind": "error", "error": str(e)})

        if not collected:
            return self._json({"ok": False, "error": "没有解析到任何通知", "report": report}, 400)

        default_source = (params.get("source") or "").strip()
        conn = get_db()
        created = updated = 0
        for it in collected:
            if default_source and not (it.get("source") or it.get("来源")):
                it["source"] = default_source
            row = build_notice_row(it)
            _, isnew = upsert_notice(conn, row)
            created += 1 if isnew else 0
            updated += 0 if isnew else 1
        conn.commit()
        audit("import", f"{len(collected)} items", json.dumps(report, ensure_ascii=False)[:400])
        return self._json(
            {"ok": True, "parsed": len(collected), "created": created,
             "skipped": updated, "report": report}
        )

    # ---------------- 导出 ----------------
    def _export(self, params: dict):
        fmt = (params.get("format") or "csv").lower()
        params = dict(params)
        params["size"] = "200"
        rows, page = [], 1
        while True:
            params["page"] = str(page)
            # 导出要正文（CSV 的「正文」列 / JSON 全量），所以这里必须关掉裁剪
            res = query_notices(params, slim=False)
            rows.extend(res["items"])
            if page >= res["pages"] or page > 100:
                break
            page += 1

        stamp = now().strftime("%Y%m%d-%H%M%S")
        if fmt == "json":
            body = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="notices-{stamp}.json"'})

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", "标题", "来源", "群/发件人", "发件人地址", "发布人",
                         "发布时间", "接收时间", "分类", "级别", "标签", "置顶", "正文"])
        for r in rows:
            writer.writerow([
                r["id"], r["title"], r["source_label"], r["source_name"], r["sender"],
                r["publisher"], r["published_at"], r["received_at"], r["category"],
                r["level_label"], " ".join(r["tags"]), "是" if r["pinned"] else "",
                r["content"],
            ])
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": f'attachment; filename="notices-{stamp}.csv"'})


def mimetypes_guess(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".log": "text/plain; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".ico": "image/x-icon",
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip": "application/zip",
        ".woff2": "font/woff2",
    }.get(ext, "application/octet-stream")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# 上传限制：单文件 15MB、单次 40MB；只允许白名单后缀，避免被当成文件中转站
UPLOAD_MAX_FILE = 15 * 1024 * 1024
UPLOAD_MAX_TOTAL = 40 * 1024 * 1024
UPLOAD_OK_EXTS = IMAGE_EXTS + (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                               ".txt", ".log", ".md", ".csv", ".eml", ".zip")


def upload_dir() -> str:
    d = os.path.join(DATA_DIR, "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def save_upload(filename: str, payload: bytes) -> tuple:
    """上传文件落到 data/uploads/，返回 (显示名, 访问 URL, 字节数)。

    实体文件名用 ASCII（sha1 + 后缀）：静态路径没做 unquote 时中文名必然 404，
    这里直接避开；显示名保留用户原始文件名，存在通知的 attachments 里。
    """
    name = os.path.basename((filename or "").replace("\\", "/")).strip() or "未命名文件"
    ext = os.path.splitext(name)[1].lower()
    if ext not in UPLOAD_OK_EXTS:
        raise ValueError(f"不支持的文件类型：{ext or '（无后缀）'}")
    if len(payload) > UPLOAD_MAX_FILE:
        raise ValueError(f"文件过大（{len(payload) / 1048576:.1f}MB，上限 {UPLOAD_MAX_FILE // 1048576}MB）")
    digest = hashlib.sha1(payload).hexdigest()[:10]
    stored = f"up-{digest}{ext}"
    path = os.path.join(upload_dir(), stored)
    if not os.path.isfile(path):
        with open(path, "wb") as f:
            f.write(payload)
    return name[:120], f"/uploads/{stored}", len(payload)


def multipart_boundary(ctype: str) -> str:
    """从 Content-Type 里取 multipart boundary。

    ⚠️ boundary 是**大小写敏感**的，不能先把整个 Content-Type 转小写再取：
    浏览器生成的是 `----WebKitFormBoundary7MA4YWxkTrZu0gW` 这种混合大小写，
    小写化之后就对不上请求体，解析结果为空（历史版本这里的 bug 让「上传文件」整个不可用）。
    """
    for part in (ctype or "").split(";"):
        p = part.strip()
        if p.lower().startswith("boundary="):
            return p.split("=", 1)[1].strip().strip('"')
    return ""


def parse_multipart(raw: bytes, boundary: bytes) -> tuple:
    """极简 multipart/form-data 解析。

    返回 (文本字段 dict, 文件列表 [(filename, bytes)])。
    之前只取文件、把文本字段直接丢掉，所以「粘贴文本 + 附件」一起提交时文本会丢。
    """
    fields, files = {}, []
    delim = b"--" + boundary
    for part in raw.split(delim)[1:]:
        if part[:2] == b"--":
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        header_blob = part[:head_end].decode("utf-8", "replace")
        payload = part[head_end + 4:]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        fm = re.search(r'filename="([^"]*)"', header_blob)
        nm = re.search(r'name="([^"]*)"', header_blob)
        if fm and fm.group(1):
            files.append((fm.group(1), payload))
        elif nm:
            fields[nm.group(1)] = payload.decode("utf-8", "replace")
    return fields, files


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

class NoticeHubServer(ThreadingHTTPServer):
    """抬高 accept 队列。

    socketserver 的默认 request_queue_size 只有 5，并发稍高内核就开始丢 SYN，
    客户端要等 TCP 重传（RTO=1s）才连上——实测 20 并发时约 4.5% 的请求
    出现 ~1.05s 尖刺，12 并发以下则完全正常，正是这个队列溢出的特征。
    256 足够覆盖浏览器一次开多条连接 + 健康检查 + 采集器的情况。
    """

    daemon_threads = True
    request_queue_size = 256


def main() -> None:
    global SECRET
    init_db()
    SECRET = get_secret()
    if not SYNC_TOKEN:
        log("sync token (auto):", effective_sync_token())
    users = list_users()
    log(f"账号数：{len(users)}（管理员 {count_enabled_admins()} 个）")
    for u in users:
        if u["weak_pwd"]:
            log(f"⚠️  账号 {u['username']} 正在使用弱密码，请尽快修改")

    # 导航在线状态探测（后台线程，启动即探一轮，之后每 60 秒一轮）
    threading.Thread(target=nav_probe_loop, daemon=True).start()
    httpd = NoticeHubServer(("0.0.0.0", PORT), Handler)
    log(f"listening on 0.0.0.0:{PORT}  (data: {DATA_DIR})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        try:
            get_db().close()
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        init_db()
        SECRET = get_secret()
        print(f"seeded {seed_demo()} demo notices")
        sys.exit(0)

    # 忘记管理员密码时的应急入口（需要在服务器上执行，不经过 Web）
    if len(sys.argv) > 1 and sys.argv[1] == "reset-admin":
        init_db()
        SECRET = get_secret()
        argv = sys.argv[2:]
        newpwd = None
        if "--password" in argv:
            idx = argv.index("--password")
            if idx + 1 < len(argv):
                newpwd = argv[idx + 1]
        if not newpwd:
            print("用法：python server.py reset-admin --password <新密码>")
            sys.exit(1)
        row = get_db().execute(
            "SELECT * FROM users WHERE role=? ORDER BY id LIMIT 1", (ROLE_ADMIN,)
        ).fetchone()
        if row:
            set_password(row["id"], newpwd)
            get_db().execute("UPDATE users SET enabled=1, weak_pwd=? WHERE id=?",
                             (1 if is_weak_password(newpwd) else 0, row["id"]))
            audit("reset_admin_cli", row["username"])
            print(f"已重置管理员 {row['username']} 的密码，并确保该账号为启用状态")
        else:
            create_user(DEFAULT_ADMIN_USER, newpwd, ROLE_ADMIN, "系统管理员", "cli",
                        enforce_strength=False)
            print(f"未找到管理员账号，已创建 {DEFAULT_ADMIN_USER}")
        if is_weak_password(newpwd):
            print("⚠️  该密码较弱，建议尽快登录后改成更强的密码")
        sys.exit(0)

    main()
