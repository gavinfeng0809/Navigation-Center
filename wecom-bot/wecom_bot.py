#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企微「重要通知」自动归档采集器 —— 智能机器人 · WebSocket 长连接版
=================================================================

为什么用「长连接」而不是「设置接收消息回调地址」：
    notice-hub 只监听内网 10.0.0.10:8091，企微服务器回调不到它。
    长连接由本机主动外连 wss://openws.work.weixin.qq.com：
      · 不需要公网 IP / 回调 URL
      · 不需要处理消息加解密
      · 只需要管理后台给出的 BotID + Secret

工作方式（半自动，@即归档）：
    群里发重要通知时 @一下这个机器人 —— 企微把那条消息推给本脚本 ——
    脚本解析成通知结构 —— POST 到 notice-hub 的 /api/ingest（按 msgid 幂等）
    —— 然后在群里回一条「已归档」。

    为什么不能全自动收全部群消息：企微群机器人 webhook 只能发不能收，
    自建应用读不到群历史，智能机器人也只在「被@」时才收到那一@条。
    想全自动只有「会话内容存档」（需企业认证 + 确认函 + 购买授权）。

用法：
    ./venv/bin/python wecom_bot.py                # 正式跑（systemd 跑的就是这个）
    ./venv/bin/python wecom_bot.py --check        # 只检查配置和连通性，不连企微
    ./venv/bin/python wecom_bot.py --dry-run      # 连企微但不写库，只打印
    ./venv/bin/python wecom_bot.py --ws-url ws://127.0.0.1:8765   # 指向本地 mock 做联调

依赖：wecom-aibot-python-sdk（见 requirements.txt），装在 ./venv 里。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request

# 规则版「一键整理」（从 public/notices.html 逐行移植，有一致性对拍测试盯着）
from polish_rules import polish_record_ex
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
NOTICE_HUB = "/data/notice-hub"
DEFAULT_CONFIG = os.path.join(BASE, "config.env")

BEIJING = timezone(timedelta(hours=8))
LOG_FILE = None


# --------------------------------------------------------------------------
# 日志 / 配置
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def load_env_file(path: str) -> dict:
    """极简 KEY=VALUE 解析（带 # 注释、允许引号），不引入 yaml/dotenv 依赖。"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            out[k] = v
    return out


class Config:
    def __init__(self, path: str, overrides: dict):
        env = load_env_file(path)
        env.update({k: v for k, v in overrides.items() if v is not None})

        self.bot_id = env.get("WECOM_BOT_ID", "").strip()
        self.bot_secret = env.get("WECOM_BOT_SECRET", "").strip()
        self.ws_url = env.get("WECOM_WS_URL", "").strip()

        self.notice_base = env.get("NOTICE_BASE", "http://10.0.0.10:8091").rstrip("/")
        self.notice_token = env.get("NOTICE_TOKEN", "").strip()
        if not self.notice_token:
            # 复用 notice-hub 自己的 .env，避免同一个令牌写两处
            hub_env = load_env_file(os.path.join(NOTICE_HUB, ".env"))
            self.notice_token = hub_env.get("SYNC_TOKEN", "").strip()

        self.allow_chatids = split_list(env.get("ALLOW_CHATIDS", ""))
        self.allow_chattypes = split_list(env.get("ALLOW_CHATTYPES", "group,single")) or ["group"]
        self.require_keyword = env.get("REQUIRE_KEYWORD", "0").strip() in ("1", "true", "yes", "on")
        self.keywords = split_list(env.get("KEYWORDS", ""))
        self.strip_mention = env.get("STRIP_MENTION", "1").strip() not in ("0", "false", "no", "off")
        # 机器人显示名。名字里带空格时必须填，否则 @ 前缀会剥不干净、
        # 每条通知标题都会多出半截名字。
        self.bot_name = env.get("BOT_NAME", "").strip()
        # 把「提问」挡在归档之外（默认开）。关掉就退回「只要 @ 就归档」。
        self.skip_queries = env.get("SKIP_QUERIES", "1").strip() not in ("0", "false", "no", "off")
        # 入库前自动跑规则版整理（抽关键字 / 摘要点 / 行动句加粗）。
        # 纯规则、不联网、不调模型，所以当场就能出关键字。
        # 真正的「【一句话】」摘要需要模型，仍由 Hermes 按「待汇总」标签补。
        self.auto_polish = env.get("AUTO_POLISH", "1").strip() not in ("0", "false", "no", "off")
        # 健康播报间隔（秒）。注意 config.env 不会进 os.environ，必须从这里读。
        try:
            self.health_log_interval = int(env.get("HEALTH_LOG_INTERVAL", DEFAULT_HEALTH_LOG_INTERVAL))
        except ValueError:
            self.health_log_interval = DEFAULT_HEALTH_LOG_INTERVAL
        self.health_log_interval = max(60, self.health_log_interval)
        self.default_category = env.get("DEFAULT_CATEGORY", "重要通知").strip() or "重要通知"
        self.default_level = env.get("DEFAULT_LEVEL", "urgent").strip() or "urgent"
        self.tags = split_list(env.get("TAGS", "企微自动归档"))

        self.chat_names = parse_pairs(env.get("CHAT_NAMES", ""))
        # userid -> 显示名。企微回调里的 from **只有 userid**（没有显示名），
        # 长连接 SDK 也没有查用户资料的接口，所以姓名只能靠这份映射表。
        # 没配到的会原样显示 userid，并在日志里提示补。
        self.user_names = parse_pairs(env.get("USER_NAMES", ""))

        self.download_media = env.get("DOWNLOAD_MEDIA", "1").strip() not in ("0", "false", "no", "off")
        self.media_dir = env.get("MEDIA_DIR", os.path.join(NOTICE_HUB, "public", "wecom-media"))
        self.media_url_prefix = env.get("MEDIA_URL_PREFIX", "/wecom-media").rstrip("/")

        self.state_dir = env.get("STATE_DIR", os.path.join(BASE, "state"))
        self.reply_ack = env.get("REPLY_ACK", "1").strip() not in ("0", "false", "no", "off")
        self.dry_run = env.get("DRY_RUN", "0").strip() in ("1", "true", "yes", "on")

    def describe(self) -> str:
        def mask(s: str) -> str:
            if not s:
                return "(未配置)"
            return s[:4] + "*" * max(0, len(s) - 8) + s[-4:] if len(s) > 8 else "***"

        return "\n".join([
            f"  BotID            : {self.bot_id or '(未配置)'}",
            f"  Secret           : {mask(self.bot_secret)}",
            f"  ws_url           : {self.ws_url or '(SDK 默认 openws.work.weixin.qq.com)'}",
            f"  入库接口         : {self.notice_base}/api/ingest",
            f"  同步令牌         : {mask(self.notice_token)}",
            f"  允许的会话       : chatids={self.allow_chatids or '(不限)'} chattypes={self.allow_chattypes}",
            f"  关键词要求       : {'必须命中 ' + str(self.keywords) if self.require_keyword else '不要求（@即归档）'}",
            f"  机器人名         : {self.bot_name or '(未配，按正则剥 @ 前缀)'}",
            f"  挡提问           : {self.skip_queries}（识别成提问的不归档，只回执说明）",
            f"  自动整理重点     : {self.auto_polish}（规则版：抽关键字/摘要点，不调模型）",
            f"  健康播报         : 每 {self.health_log_interval} 秒在日志报一次",
            f"  默认分类/级别    : {self.default_category} / {self.default_level}",
            f"  附加标签         : {self.tags or '(无)'}",
            f"  下载图片         : {self.download_media}  ->  {self.media_dir}",
            f"  群名映射         : {self.chat_names or '(无，群名回退为 chatid)'}",
            f"  发布人映射       : {self.user_names or '(无，发布人显示 userid)'}",
            f"  群内回执         : {self.reply_ack}",
            f"  演练模式         : {self.dry_run}",
        ])


def split_list(s: str) -> list:
    return [x.strip() for x in re.split(r"[,，;；\n]", s or "") if x.strip()]


def parse_pairs(s: str) -> dict:
    """chatid=群名称, chatid2=群名称2"""
    out = {}
    for item in split_list(s):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
# 文本处理
# --------------------------------------------------------------------------

MENTION_RE = re.compile(r"^\s*@[^\s@]{1,40}\s*")

# 被动回复（stream.content）的官方上限：20480 字节 utf8，超了整帧被拒
STREAM_CONTENT_LIMIT = 20480

# 定期在日志里报一次「我还活着」。
# 用途只有一个：把「采集器挂了」和「没人 @ 机器人」区分开。
# 企微只在**被 @** 时才推消息，所以「日志安静」既可能是故障、也可能只是没人 @ ——
# 没有这行的话，两种情况看起来一模一样，只能靠翻心跳日志去猜。
# 标题上限。与 server.py 的 derive_title(limit=60) 对齐 —— 见 smart_title() 的说明。
SERVER_TITLE_LIMIT = 60

DEFAULT_HEALTH_LOG_INTERVAL = 600

# @机器人 时自己打的话短于这个长度，就认为它是「补归档一下」这类指令，
# 而不是通知正文——此时以被引用的内容为正文主体。
QUOTE_AS_MAIN_MAX = 30


def strip_mention(text: str, bot_name: str = "") -> str:
    """去掉开头的「@机器人名」——群里@机器人时正文前面必然带这个。

    正则只能按「@ + 一段非空白字符」去猜，所以**机器人名里带空格就会剥不干净**：
    名叫「通知归档 助手」时，正文会剩下「助手 【重要通知】…」，
    而标题默认取首行 —— 于是每条归档通知的标题都多一个「助手」。
    配了 BOT_NAME 就按名字精确剥，不依赖猜。
    """
    out = text or ""
    name = (bot_name or "").strip()
    # 先按已知名字精确剥（可以连着来几个 @）
    while name:
        head = out.lstrip()
        if head.startswith("@" + name):
            out = head[1 + len(name):]
            continue
        break
    # 再用正则兜底，处理没配名字或多带了别人名字的情况
    prev = None
    while prev != out:
        prev = out
        out = MENTION_RE.sub("", out, count=1)
    return out.strip()


# 企微通知的习惯写法是第一行只写个「【重要通知】」当标记，真正的标题在第二行。
# 只认「整行仅由标记构成」的情况，避免把「【重要通知】xxx」这种真标题也误伤。
MARKER_RE = re.compile(
    r"^[【\[\(（]?\s*(重要通知|紧急通知|通知|重要|紧急|公告)\s*[】\]\)）]?[：:!！。\.]?$"
)


def derive_title(content: str) -> str:
    lines = []
    for raw in (content or "").splitlines():
        s = raw.strip().strip("#").strip()
        if s:
            lines.append(s)
    if not lines:
        return "(无标题)"
    if MARKER_RE.match(lines[0]) and len(lines) > 1:
        return lines[1][:120]
    return lines[0][:120]


def smart_title(title: str, limit: int = SERVER_TITLE_LIMIT) -> str:
    """把标题截到 limit 字，尽量断在语义边界上。

    **必须与 `server.py` 的 `derive_title()` 截断段保持一致** ——
    我们自己传了 title，服务端就不再走它的 derive_title（那里才会截断），
    于是规则版整理出的最长 200 字的标题会原样进库，列表里长得没法看。
    这里复刻它的策略：60 字上限 → 优先在句末标点断 → 退而在逗号断 → 再兜底加省略号。
    """
    t = re.sub(r"\s+", " ", (title or "")).strip()
    if len(t) <= limit:
        return t
    head = t[:limit]
    m = re.search(r"[。；;！!?？]", head)
    m2 = re.search(r"[，,、]", head)
    if m and m.start() >= 12:
        return head[:m.start() + 1]
    if m2 and m2.start() >= 12:
        return head[:m2.start()] + "…"
    return head + "…"


def now_str() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 入库
# --------------------------------------------------------------------------

def post_ingest(cfg: Config, items: list) -> dict:
    payload = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg.notice_base + "/api/ingest",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Sync-Token": cfg.notice_token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_with_retry(cfg: Config, items: list, attempts: int = 3) -> dict:
    last = None
    for i in range(1, attempts + 1):
        try:
            return post_ingest(cfg, items)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            last = f"HTTP {e.code} {body}"
            if e.code in (401, 403):  # 令牌问题，重试没意义
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if i < attempts:
            time.sleep(min(2 ** i, 8))
    raise RuntimeError(last or "unknown error")


def pending_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "pending.jsonl")


def save_pending(cfg: Config, item: dict, err: str) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(pending_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps({"item": item, "err": err, "at": now_str()}, ensure_ascii=False) + "\n")
    log(f"⚠️  入库失败已落盘待重试：{item.get('ext_id')}（{err}）")


def flush_pending(cfg: Config) -> None:
    """把之前失败的消息重投一次。notice-hub 按 ext_id 幂等，重复投不会脏数据。"""
    p = pending_path(cfg)
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        log(f"⚠️  待重试文件解析失败，跳过：{e}")
        return
    if not rows:
        return

    log(f"重投上次失败的通知：{len(rows)} 条")
    left = []
    for r in rows:
        try:
            res = post_with_retry(cfg, [r["item"]], attempts=2)
            log(f"  ✅ 重投成功 {r['item'].get('ext_id')} -> id={res.get('ids')}")
        except Exception as e:
            left.append(r)
            log(f"  ❌ 重投仍失败 {r['item'].get('ext_id')}：{e}")
    if left:
        with open(p, "w", encoding="utf-8") as f:
            for r in left:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        os.remove(p)


# --------------------------------------------------------------------------
# 消息 -> 通知结构
# --------------------------------------------------------------------------

def extract_content(body: dict) -> tuple[str, list]:
    """返回 (正文文本, 附件项列表[{url, aeskey}])。

    覆盖企微实际会推来的几种 msgtype：
      text   —— 纯文本
      mixed  —— 图文混排（正文 + 图片交错）
      voice  —— 语音。企微**在服务端就做了识别**，推下来的是文字转写结果，
                没有音频、没有 media key，直接当正文用即可。
      image / file / video —— 单发的附件，只有预签名 url + 解密 key。
    """
    msgtype = body.get("msgtype", "")
    if msgtype == "text":
        return (body.get("text", {}) or {}).get("content", "") or "", []

    if msgtype == "voice":
        # 注意：语音不是「没正文」，转写文字就是正文。漏掉这一支的话，
        # 群里发语音 @机器人 会被当成空消息丢掉。
        return ((body.get("voice", {}) or {}).get("content", "") or "").strip(), []

    if msgtype in ("image", "file", "video"):
        media = body.get(msgtype, {}) or {}
        if media.get("url"):
            return "", [{"url": media["url"], "aeskey": media.get("aeskey", "")}]
        return "", []

    if msgtype == "mixed":
        texts, images = [], []
        for it in (body.get("mixed", {}) or {}).get("msg_item", []) or []:
            t = it.get("msgtype")
            if t == "text":
                texts.append((it.get("text", {}) or {}).get("content", "") or "")
            elif t == "voice":
                # 混排里的语音同样是转写文字
                texts.append(((it.get("voice", {}) or {}).get("content", "") or "").strip())
            elif t == "image":
                img = it.get("image", {}) or {}
                if img.get("url"):
                    images.append({"url": img["url"], "aeskey": img.get("aeskey", "")})
            elif t in ("file", "video"):
                m = it.get(t, {}) or {}
                if m.get("url"):
                    images.append({"url": m["url"], "aeskey": m.get("aeskey", "")})
        return "\n".join(x for x in texts if x), images

    return "", []


def build_item(cfg: Config, body: dict, text: str, attachments: list) -> dict:
    chattype = body.get("chattype", "")
    chatid = body.get("chatid", "") or ""
    userid = (body.get("from", {}) or {}).get("userid", "") or ""

    if chattype == "group":
        source_name = cfg.chat_names.get(chatid) or (chatid or "企微群")
    else:
        source_name = "企微单聊"

    title = derive_title(text)
    content = text
    keywords = []
    if cfg.auto_polish:
        # 规则版整理：标题认得更准（会跳过「各位同事：」这类），正文抽出
        # 关键字 + 【要点】，行动句加粗。跑在入库前，所以页面上当场就有。
        # 整理失败**绝不能丢通知** —— 出任何问题都退回原文。
        try:
            r = polish_record_ex(text)
            if r["content"]:
                content = r["content"]
            if r["title"]:
                title = smart_title(r["title"])
            keywords = r["keywords"]
        except Exception as e:  # noqa: BLE001
            log(f"  ⚠️  规则版整理失败，本条按原文入库：{e}")

    # 关键字同时进 tags 字段（页面左侧筛选/搜索直接能用），
    # 排在配置的基础标签之后 —— 「待汇总」得留着，Hermes 还要靠它补真摘要。
    tags = list(cfg.tags) + [k for k in keywords if k not in cfg.tags]

    return {
        "ext_id": body.get("msgid", ""),          # 企微 msgid —— 天然幂等键
        "source": "wecom",
        "source_name": source_name,
        # 显示名优先；没映射就退回 userid（至少不会显示空白）
        "publisher": cfg.user_names.get(userid, userid) if userid else "",
        # 原始 userid 另存一份：同名同姓或改过显示名时，靠它才能对得上人
        "sender": userid,
        "title": title,
        "content": content,
        "published_at": now_str(),
        "received_at": now_str(),
        "category": cfg.default_category,
        "level": cfg.default_level,
        "tags": tags,
        "attachments": attachments,
    }


def keyword_hit(cfg: Config, text: str) -> bool:
    if not cfg.require_keyword:
        return True
    low = text.lower()
    return any(k.lower() in low for k in cfg.keywords)


# 「这是在提问，不是在发通知」。
# 上线第一天就撞上了：同事把机器人当搜索用了 ——
#   @通知归档助手 帮我查询以下近期的重要通知内容
# 这条被当成「重要通知 / urgent」归档，成了通知中心里的一条垃圾。
# 不加约束的话，每个同事都会这么试一次。
#
# 判断刻意做得**很窄**，而且只看开头：
#   · 敬语（帮我/请/麻烦…）之后必须紧跟查询类动词
#   · 正文里出现「查询」二字不算，只有打头才算
#   · 再加长度闸门：超过 QUERY_MAX_LEN 就当成正文，不往「提问」上猜
# 宁可漏判也不能误判 —— 漏判会回执告知发的人（可补救），
# 误判则是**静默丢掉一条真通知**。
QUERY_RE = re.compile(
    r"^\s*(?:帮我|帮忙|请|麻烦|劳驾|能不能|能否|可以|想)?\s*"
    r"(?:查询|查找|查一下|查下|查查|查看|查阅|搜索|搜一下|搜下|检索|"
    r"找一下|找找|找出|列出|列举|显示|展示|看看|看一下|汇总|统计)"
)
QUERY_ALT_RE = re.compile(r"^\s*(?:有没有|有哪些|哪些|什么|多少|怎么|如何)")
QUERY_MAX_LEN = 50


def looks_like_query(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > QUERY_MAX_LEN:
        return False
    return bool(QUERY_RE.match(t) or QUERY_ALT_RE.match(t))


def chat_allowed(cfg: Config, body: dict) -> tuple[bool, str]:
    chattype = body.get("chattype", "")
    if cfg.allow_chattypes and chattype not in cfg.allow_chattypes:
        return False, f"会话类型 {chattype} 不在允许范围"
    if cfg.allow_chatids:
        chatid = body.get("chatid", "") or ""
        if chatid not in cfg.allow_chatids:
            return False, f"群 {chatid} 不在白名单"
    return True, ""


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------

class Collector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        self.seen = set()
        self.archived = 0
        self._flushing = False      # 防止并发重投同一个 pending 文件
        self._warned_users = set()  # 已提示过「没有显示名」的 userid

    # ---------- 图片落盘 ----------
    async def save_images(self, body: dict, images: list) -> list:
        if not images or not self.cfg.download_media:
            return []
        os.makedirs(self.cfg.media_dir, exist_ok=True)
        msgid = re.sub(r"[^A-Za-z0-9_-]", "", body.get("msgid", ""))[:40] or "msg"
        out = []
        for i, img in enumerate(images, 1):
            try:
                data, filename = await self.client.download_file(img["url"], img.get("aeskey") or None)
                ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                    ext = ".jpg"
                name = f"{msgid}-{i}{ext}"
                with open(os.path.join(self.cfg.media_dir, name), "wb") as f:
                    f.write(data)
                out.append({"name": f"图片{i}{ext}", "url": f"{self.cfg.media_url_prefix}/{name}"})
                log(f"  🖼  图片已存 {name}（{len(data)} bytes）")
            except Exception as e:
                log(f"  ⚠️  图片 {i} 下载失败（不影响正文归档）：{e}")
        return out

    # ---------- 回执 ----------
    async def reply(self, frame: dict, content: str) -> None:
        """在群里回一条执。

        ⚠️ 这里**不能用 msgtype=markdown**。被动回复走的是 aibot_respond_msg，
        官方 SDK 类型里它只认 stream / text / template_card / 媒体；
        markdown 是**主动发送**（aibot_send_msg）专用的。类型用错企微会直接拒收，
        表现是「库里归档成功了，群里却没回执」——而本地 mock 不校验 payload，
        所以这条错误在联调阶段完全测不出来。

        统一走 stream（finish=True）：一次封口，且 content 支持 Markdown
        （回执里有指向通知中心的链接）。
        另外两个坑一起挡掉：
          · content 上限 20480 字节（utf8），超了整帧被拒 —— 按字符边界截断；
          · 封口帧内容必须「有可见字符」，空白内容会被忽略并留下一个转圈的空气泡。
        """
        if not self.cfg.reply_ack:
            return
        text = (content or "").strip()
        if not text:
            return
        raw = text.encode("utf-8")
        if len(raw) > STREAM_CONTENT_LIMIT:
            cut = STREAM_CONTENT_LIMIT - 3          # 给省略号留位置
            while cut > 0 and (raw[cut] & 0xC0) == 0x80:
                cut -= 1                            # 退到字符边界，别把汉字切两半
            text = raw[:cut].decode("utf-8", "ignore") + "…"
        try:
            from aibot import generate_req_id
            await self.client.reply_stream(
                frame, generate_req_id("nh-ack"), text, finish=True)
        except Exception as e:
            log(f"  ⚠️  群内回执发送失败：{e}")

    # ---------- 消息处理 ----------
    async def handle(self, frame: dict) -> None:
        body = frame.get("body", {}) or {}
        msgid = body.get("msgid", "")
        chattype = body.get("chattype", "")

        if not msgid:
            log("⚠️  收到无 msgid 的回调，忽略")
            return
        if msgid in self.seen:
            log(f"↩️  重复回调（同进程内）已跳过：{msgid}")
            return
        self.seen.add(msgid)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])

        # 顺手重投之前失败的（README 里承诺过「下次收到消息会自动重投」，
        # 但原代码只在启动时重投一次，这句承诺其实是落空的）。
        # 丢到线程里跑：flush_pending 是同步阻塞的，直接在事件循环里做会卡住
        # 消息接收；没有 pending 文件时它近乎零成本。
        # 加个在途标记——两条消息挨得近时会并发跑两个 flush_pending，同时读写
        # pending.jsonl。有 ext_id 幂等兜底不会脏数据，但文件会被重复写入。
        if not self._flushing:
            self._flushing = True
            _fut = asyncio.get_running_loop().run_in_executor(None, flush_pending, self.cfg)
            _fut.add_done_callback(lambda _f: setattr(self, "_flushing", False))

        ok, why = chat_allowed(self.cfg, body)
        if not ok:
            log(f"⏭  跳过 {msgid}：{why}")
            return

        raw_text, images = extract_content(body)
        text = strip_mention(raw_text, self.cfg.bot_name) if self.cfg.strip_mention else raw_text

        # 引用某条消息再 @机器人，是很常见的用法（把老通知引用出来补归档）。
        # 三种情况分开处理，别让「补归档一下」这种话把真正的通知标题顶掉：
        #   · 自己没打字        -> 归档被引用的内容
        #   · 自己只打了句短话  -> 以被引用内容为主体，自己那句降为【补充说明】
        #   · 自己打了长正文    -> 以自己为主，引用内容附在后面当上下文
        quote = (body.get("quote", {}) or {})
        quote_text = ((quote.get("text", {}) or {}).get("content", "") or "").strip() \
            if quote.get("msgtype") == "text" else ""
        if quote_text:
            if not text:
                text = quote_text
            elif len(text) <= QUOTE_AS_MAIN_MAX:
                text = f"{quote_text}\n\n【补充说明】\n{text}"
            else:
                text = f"{text}\n\n【引用原文】\n{quote_text}"

        text = (text or "").strip()
        if not text and not images:
            log(f"⏭  跳过 {msgid}：正文为空（msgtype={body.get('msgtype')}）")
            return
        if not text:
            text = "（仅图片，无正文）"

        if self.cfg.skip_queries and looks_like_query(text):
            # 不归档，但一定要回执 —— 否则发的人以为坏了。
            log(f"⏭  跳过 {msgid}：看起来是提问，不是通知 ——「{text[:40]}」")
            await self.reply(
                frame,
                "我是**通知归档助手**，只做归档、不做查询。\n"
                "要归档请把通知正文 @我一下。\n"
                f"查通知请到[重要通知中心]({self.cfg.notice_base}/notices.html)。")
            return

        if not keyword_hit(self.cfg, text):
            # 回执必须说清楚**要加什么字**。只回「未命中关键词」的话，
            # 发的人不知道该怎么办，只会再试一次同样的内容。
            kws = "、".join(self.cfg.keywords) or "(未配置)"
            log(f"⏭  跳过 {msgid}：未命中关键词 {self.cfg.keywords} ——「{text[:40]}」")
            await self.reply(
                frame,
                f"本条**未归档**：正文里没有找到关键词。\n"
                f"当前要求正文包含：**{kws}**\n"
                f"如果这确实是通知，把关键词写进正文后再 @我一下。")
            return

        attachments = await self.save_images(body, images)
        item = build_item(self.cfg, body, text, attachments)

        log(f"📥 收到 @ 消息 {msgid}（{chattype}，来源 {item['source_name']}，"
            f"发布人 {item['publisher']}）：{item['title']}")

        # 没映射到显示名时提示一次，方便逐步把 USER_NAMES 补全
        _uid = (body.get("from", {}) or {}).get("userid", "") or ""
        if _uid and _uid not in self.cfg.user_names and _uid not in self._warned_users:
            self._warned_users.add(_uid)
            log(f"   ℹ️  「{_uid}」还没配显示名，页面上会显示这个 userid。"
                f"要显示中文名/英文名，在 config.env 的 USER_NAMES 里加「{_uid}=显示名」再重启。")

        if self.cfg.dry_run:
            log("🧪 dry-run，不写库。解析结果：\n" + json.dumps(item, ensure_ascii=False, indent=2))
            return

        try:
            # attempts=1：被动回复有 5 秒窗口，而 post_with_retry 的退避是
            # 2s + 4s，重试两下就超过窗口、回执必定发不出去。所以交互路径只试一次，
            # 失败就落 pending，由 flush_pending 在后台慢慢重投。
            res = post_with_retry(self.cfg, [item], attempts=1)
            nid = (res.get("ids") or [None])[0]
            kind = "新增" if res.get("created") else "更新"
            self.archived += 1
            log(f"  ✅ 已入库 id={nid}（{kind}）")
            await self.reply(frame,
                             f"✅ 已归档到重要通知中心（{kind}）\n"
                             f">标题：{item['title']}\n"
                             f">来源：{item['source_name']}\n"
                             f"[打开重要通知中心]({self.cfg.notice_base}/notices.html)")
        except Exception as e:
            log(f"  ❌ 入库失败：{e}")
            save_pending(self.cfg, item, str(e))
            await self.reply(frame, f"⚠️ 归档失败，已留待自动重试：{e}")

    # ---------- 启动 ----------
    def build_client(self):
        from aibot import WSClient, WSClientOptions

        opts = WSClientOptions(
            bot_id=self.cfg.bot_id,
            secret=self.cfg.bot_secret,
            max_reconnect_attempts=-1,   # 永久重连：这是常驻服务
            heartbeat_interval=30000,
        )
        if self.cfg.ws_url:
            opts.ws_url = self.cfg.ws_url
        client = WSClient(opts)

        @client.on("authenticated")
        def _on_auth():
            log("🔐 长连接认证成功，开始接收 @ 消息")

        @client.on("connected")
        def _on_conn():
            log("🔌 WebSocket 已连接")

        @client.on("reconnecting")
        def _on_reconn(attempt):
            log(f"🔄 断线重连中（第 {attempt} 次）")

        @client.on("error")
        def _on_err(err):
            log(f"⚠️  连接错误：{err}")

        @client.on("message.text")
        async def _on_text(frame):
            await self.handle(frame)

        @client.on("message.mixed")
        async def _on_mixed(frame):
            await self.handle(frame)

        # 这三种也要订阅，否则企微推来了也进不了 handle()。
        # 尤其是语音：群众发语音 @机器人 很常见，而企微已经把语音在服务端
        # 转成了文字，等于白送一份正文 —— 漏订阅就全丢了。
        @client.on("message.voice")
        async def _on_voice(frame):
            await self.handle(frame)

        @client.on("message.image")
        async def _on_image(frame):
            await self.handle(frame)

        @client.on("message.file")
        async def _on_file(frame):
            await self.handle(frame)

        @client.on("event.enter_chat")
        async def _on_enter(frame):
            try:
                await client.reply_welcome(frame, {
                    "msgtype": "text",
                    "text": {"content": "我是重要通知归档助手：发重要通知时 @我一下，我就把它归档到重要通知中心。"},
                })
            except Exception as e:
                log(f"  ⚠️  欢迎语发送失败：{e}")

        self.client = client
        return client

    async def run(self) -> None:
        log("=" * 68)
        log("企微重要通知采集器启动（智能机器人长连接）")
        log(self.cfg.describe())
        log("=" * 68)

        if not self.cfg.bot_id or not self.cfg.bot_secret:
            log("❌ 未配置 WECOM_BOT_ID / WECOM_BOT_SECRET，无法连接企微。")
            log("   请让企微管理员在「管理后台 → 智能机器人 → API模式 → 长连接」拿到这两个值，")
            log(f"   填进 {os.path.join(BASE, 'config.env')} 后重启服务。")
            sys.exit(2)
        if not self.cfg.notice_token:
            log("❌ 未配置同步令牌（NOTICE_TOKEN，或 /data/notice-hub/.env 里的 SYNC_TOKEN）")
            sys.exit(2)

        flush_pending(self.cfg)

        client = self.build_client()
        await client.connect()

        # 健康播报：每 HEALTH_LOG_INTERVAL 秒报一次，说明长连接还在、只是没人 @
        started = time.time()

        async def _health_log():
            while True:
                await asyncio.sleep(self.cfg.health_log_interval)
                mins = int((time.time() - started) / 60)
                log(f"💓 长连接正常（已运行 {mins} 分钟，本次已归档 {self.archived} 条）。"
                    f"群里发通知时记得 **@机器人**，不 @ 收不到。")

        health_task = asyncio.ensure_future(_health_log())

        stop = asyncio.Event()

        def _shutdown(*_):
            log("收到退出信号，正在断开长连接…")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                pass

        await stop.wait()
        health_task.cancel()
        try:
            client.disconnect()
        except Exception:
            pass
        log(f"已退出。本次运行归档 {self.archived} 条。")


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def self_check(cfg: Config) -> int:
    problems = []
    log("配置：")
    log(cfg.describe())
    log("-" * 68)

    if not cfg.bot_id:
        problems.append("WECOM_BOT_ID 未配置")
    if not cfg.bot_secret:
        problems.append("WECOM_BOT_SECRET 未配置")
    if not cfg.notice_token:
        problems.append("同步令牌未配置")

    # 入库接口连通性 + 令牌有效性（用空 items 试，不会写库）
    try:
        req = urllib.request.Request(
            cfg.notice_base + "/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            log(f"✅ notice-hub 健康检查：{resp.read().decode()[:120]}")
    except Exception as e:
        problems.append(f"notice-hub 不可达（{cfg.notice_base}）：{e}")

    try:
        req = urllib.request.Request(
            cfg.notice_base + "/api/ingest",
            data=json.dumps({"items": []}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Sync-Token": cfg.notice_token},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            log(f"✅ 入库接口令牌校验通过（空 items 不会写库）：{resp.read().decode()[:120]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:120]
        if e.code == 400:
            log(f"✅ 入库接口令牌有效（空 items 被拒是预期的）：{body}")
        else:
            problems.append(f"入库接口令牌校验失败：HTTP {e.code} {body}")
    except Exception as e:
        problems.append(f"入库接口不可达：{e}")

    # 媒体目录可写
    try:
        os.makedirs(cfg.media_dir, exist_ok=True)
        probe = os.path.join(cfg.media_dir, ".write-probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        log(f"✅ 媒体目录可写：{cfg.media_dir}")
    except Exception as e:
        problems.append(f"媒体目录不可写（{cfg.media_dir}）：{e}")

    log("-" * 68)
    if problems:
        log("❌ 自检未通过：")
        for p in problems:
            log(f"   · {p}")
        return 1
    log("✅ 自检全部通过。")
    log("   下一步：./venv/bin/python wecom_bot.py --verify  （真连企微验凭证）")
    log("   再启动 ：systemctl enable --now wecom-bot")
    return 0


async def verify_connect(cfg: Config, timeout: float = 20.0) -> int:
    """真连一次企微长连接，确认 BotID / Secret 能用。

    为什么值得单独做这么一次：凭证不对是这套东西最常见的失败原因，而它的表现是
    「服务正常起着、群里 @ 了却没反应」——要翻日志、还得知道该看哪一行才能定位。
    这里把它变成一条同步的、带明确退出码的结论。

    注意：同一机器人只允许一条长连接，新连接会把旧的踢掉。
    所以服务在跑的时候别执行这个校验（会把它踢下线，然后它自己重连）。
    """
    from aibot import WSClient, WSClientOptions

    done = asyncio.Event()
    errors: list[str] = []
    state = {"ok": False}

    opts = WSClientOptions(
        bot_id=cfg.bot_id,
        secret=cfg.bot_secret,
        max_reconnect_attempts=0,   # 校验只要一个结论，不要退避重连
        heartbeat_interval=30000,
    )
    if cfg.ws_url:
        opts.ws_url = cfg.ws_url
    client = WSClient(opts)

    @client.on("authenticated")
    def _ok():
        state["ok"] = True
        done.set()

    @client.on("error")
    def _err(e):
        errors.append(str(e))
        done.set()

    log(f"正在连接企微长连接（最多等 {timeout:.0f} 秒）…")
    try:
        await client.connect()
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")

    if not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            errors.append(f"等了 {timeout:.0f} 秒也没收到认证结果")
    try:
        client.disconnect()
    except Exception:
        pass

    log("-" * 68)
    if state["ok"]:
        log("✅ 企微长连接认证成功 —— BotID / Secret 有效，采集器可以上线")
        return 0

    # 「重连次数用尽」是 SDK 在放弃时补报的，真正有用的通常是它前面那条认证错误
    real = [e for e in errors if "Max reconnect" not in e]
    detail = (real or errors or ["(没有任何错误信息)"])[0]
    log("❌ 企微长连接认证失败：")
    log(f"   {detail}")
    log("")
    log("   常见原因，按可能性排序：")
    log("     · BotID / Secret 抄错（0 与 O、l 与 I、大小写最容易混）")
    log("     · 机器人配的是「设置接收消息回调地址」而不是「长连接」——只能二选一")
    log("     · 机器人被停用/删除了")
    log("     · 本机出不了外网（要能连 wss://openws.work.weixin.qq.com:443）")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="企微重要通知采集器（智能机器人长连接）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--check", action="store_true", help="只做配置与连通性自检（不连企微）")
    ap.add_argument("--verify", action="store_true", help="真连一次企微，验证 BotID/Secret 是否有效")
    ap.add_argument("--dry-run", action="store_true", help="连企微但不写库")
    ap.add_argument("--ws-url", default=None, help="自定义 WebSocket 地址（联调用）")
    ap.add_argument("--bot-id", default=None)
    ap.add_argument("--bot-secret", default=None)
    ap.add_argument("--notice-base", default=None)
    args = ap.parse_args()

    cfg = Config(args.config, {
        "WECOM_BOT_ID": args.bot_id,
        "WECOM_BOT_SECRET": args.bot_secret,
        "NOTICE_BASE": args.notice_base,
        "WECOM_WS_URL": args.ws_url,
    })
    if args.dry_run:
        cfg.dry_run = True

    if args.check:
        return self_check(cfg)

    if args.verify:
        if not cfg.bot_id or not cfg.bot_secret:
            log("❌ 未配置 WECOM_BOT_ID / WECOM_BOT_SECRET，无法验证。")
            return 2
        return asyncio.run(verify_connect(cfg))

    try:
        asyncio.run(Collector(cfg).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
