#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把纯文本通知批量导入「通知中心」（notice-hub）。

目标：文本怎么长的都能导进来，而且**先看解析结果再决定入库**。

用法（在 /data/notice-hub 目录下执行）：

    # ⓪ 什么参数都不带 = 扫描 inbox/ 目录（.eml 邮件 / .txt / .md 一起收）
    python3 tools/import_text.py --dry-run

    # ① 先干跑：只解析、不写库，把解析结果打成清单给你看
    python3 tools/import_text.py --dir inbox --dry-run

    # ② 确认解析对了再真入库，入完把文件挪进 processed/ 免得下次重复扫
    python3 tools/import_text.py --dir inbox --category 网络变更 \
        --move-done inbox/processed

    # ③ 单个文件
    python3 tools/import_text.py --file /tmp/my.txt --dry-run

支持的文件：`.eml`（一封邮件 = 一条通知，自动解出主题/发件人/时间/正文/附件，
ext_id 取 Message-ID，和服务端算法一致）、`.txt` / `.md`（按下面的分段规则）。
`.msg` 读不了，会在输出里告诉你怎么转成 .eml。

为什么不用页面的「上传文件」：
    服务端的 parse_pasted 有个坑——只要头部里出现「标题:」这一行，它立刻切换进
    「正文模式」，**后面所有字段（来源/时间/级别…）全部被当成正文字符串吞掉**，
    字段静默丢失（README 里的示例写法就是错的，已实测）。
    本工具不受这个限制：标题可以写在头部任意位置。

入库走 POST /api/ingest + X-Sync-Token（不需要账号口令），按 ext_id 幂等：
    重复执行同一批文本 = 只更新、不新增，可以放心重跑。

⚠️ ext_id 由服务端按 source|title|published_at|sender 生成，**正文不参与**。
   所以同一来源 + 同一标题 + 同一发布时间的多条通知会被视为同一条（互相覆盖）。
   标题重复的（例如都叫「【重要通知】」）请务必让每条带上不同的发布时间。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from datetime import timedelta, timezone
from html import unescape

# 与服务端 APP_TZ 对齐：邮件头里的时间统一换算到 UTC+8 再入库
APP_TZ = timezone(timedelta(hours=8))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# 字段别名：与 server.py 的 FIELD_ALIASES 保持同步，另补几个邮件场景的词
# --------------------------------------------------------------------------
FIELD_ALIASES = {
    "标题": "title", "title": "title", "主题": "title", "subject": "title", "通知标题": "title",
    "正文": "content", "content": "content", "内容": "content", "body": "content", "通知内容": "content",
    "来源": "source", "source": "source", "渠道": "source",
    "群名称": "source_name", "群": "source_name", "source_name": "source_name",
    "群/发件人": "source_name", "会话": "source_name", "邮件文件夹": "source_name",
    "发布人": "publisher", "publisher": "publisher", "通知人": "publisher",
    "发件人": "sender", "sender": "sender", "发件人地址": "sender", "发件人邮箱": "sender",
    "时间": "published_at", "发布时间": "published_at", "published_at": "published_at",
    "发送时间": "published_at", "日期": "published_at", "date": "published_at",
    "分类": "category", "category": "category", "类型": "category",
    "级别": "level", "重要级别": "level", "level": "level", "优先级": "level",
    "标签": "tags", "tags": "tags", "tag": "tags",
    "附件": "attachments", "attachments": "attachments",
    "置顶": "pinned", "pinned": "pinned",
    "外部ID": "ext_id", "ext_id": "ext_id", "消息ID": "ext_id", "Message-ID": "ext_id",
}
# 这些字段解析出来后直接丢弃（邮件导出里的噪音，留着只会污染正文）
DROP_KEYS = {"收件人", "抄送", "密送", "to", "cc", "bcc", "importance", "重要性"}

HDR_RE = re.compile(r"^\s*([^:：]{1,14})\s*[:：]\s*(.*)$")
DASH_RE = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
# Outlook / Foxmail 导出文本的头部行
MAIL_HDR_RE = re.compile(r"^\s*(发件人|发件人地址|主题|发送时间|收件人|抄送)\s*[:：]")
# 以日期开头的行（\u4e00 之类不用，直接列出常见写法）
DATE_LINE_RE = re.compile(
    r"^\s*[\[【]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}[-/月]\d{1,2}\s*日?)"
    r"(\s+\d{1,2}:\d{2}(:\d{2})?)?"
)
BLANK_SPLIT_RE = re.compile(r"\n[ \t]*\n[ \t]*\n+")
NAME_MAIL_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<\s*(?P<mail>[^<>\s]+@[^<>\s]+)\s*>\s*$")
# 单独占一行的日期/时间（不是「时间: xxx」这种键值对，而是裸的时间行）
BARE_DATE_RE = re.compile(
    r"^\s*[\[【(]?\s*\d{4}[-/年]\d{1,2}[-/月]\d{1,2}\s*日?"
    r"(\s+\d{1,2}:\d{2}(:\d{2})?)?\s*[\]】)]?\s*$"
)


def key_of(raw: str) -> str | None:
    k = (raw or "").strip()
    if k in DROP_KEYS or k.lower() in DROP_KEYS:
        return "__drop__"
    return FIELD_ALIASES.get(k) or FIELD_ALIASES.get(k.lower())


# --------------------------------------------------------------------------
# 分段
# --------------------------------------------------------------------------
def split_blocks(text: str, strategy: str) -> tuple[list[str], str]:
    """返回 (文本块列表, 实际使用的策略名)。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if strategy == "dash":
        return [b for b in re.split(r"^\s*-{3,}\s*$", text, flags=re.MULTILINE)], "dash"
    if strategy == "blank":
        return [b for b in BLANK_SPLIT_RE.split(text)], "blank"
    if strategy == "whole":
        return [text], "whole"
    if strategy in ("auto", "mail"):
        starts = [i for i, ln in enumerate(text.splitlines()) if MAIL_HDR_RE.match(ln)]
        if strategy == "mail" or (strategy == "auto" and len(starts) >= 2):
            return _split_by_line_starts(text, starts), "mail"
    if strategy in ("auto", "date"):
        starts = [i for i, ln in enumerate(text.splitlines()) if DATE_LINE_RE.match(ln)]
        if strategy == "date" or (strategy == "auto" and len(starts) >= 2):
            return _split_by_line_starts(text, starts), "date"
    if strategy == "auto":
        if DASH_RE.search(text):
            return [b for b in DASH_RE.split(text)], "dash"
        if BLANK_SPLIT_RE.search(text):
            return [b for b in BLANK_SPLIT_RE.split(text)], "blank"
    return [text], "whole"


def _split_by_line_starts(text: str, starts: list[int]) -> list[str]:
    """按「新记录起始行」切块；起始行之前的开头部分单独作为第 0 块返回（可能为前言）。"""
    lines = text.splitlines()
    if not starts:
        return [text]
    blocks = []
    if starts[0] > 0:
        head = "\n".join(lines[: starts[0]]).strip()
        if head:
            blocks.append("__PREAMBLE__" + head)
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        blocks.append("\n".join(lines[s:e]))
    return blocks


# --------------------------------------------------------------------------
# 单条解析
# --------------------------------------------------------------------------
def parse_record(block: str, defaults: dict) -> dict | None:
    """解析一条记录。

    头部区域 = 第一个空行之前的所有行，在里面**任意顺序**都能认：
      - `键: 值`（键在别名表里）→ 对应字段
      - 单独一行的日期时间        → published_at
      - 第一个认不出来的行        → 标题
      - 其余认不出来的行          → 正文开头
    空行之后的内容全部是正文。

    注意这跟服务端 parse_pasted 不一样：那边只要看到「标题:」就立刻切换进正文模式，
    后面的字段全部被当正文字符串吞掉（README 示例就是这种写法，实测会丢字段）。
    """
    lines = block.splitlines()
    bi = next((i for i, ln in enumerate(lines) if not ln.strip()), len(lines))
    head, body = lines[:bi], lines[bi + 1:]

    data: dict = {}
    body_head: list[str] = []
    for line in head:
        if not line.strip():
            continue
        m = HDR_RE.match(line)
        key = key_of(m.group(1)) if m else None
        if key == "__drop__":
            continue
        if key and key != "content":
            if not data.get(key):
                data[key] = m.group(2).strip()
            continue
        if key == "content":
            body_head.append(m.group(2).strip())
            continue
        if BARE_DATE_RE.match(line) and not data.get("published_at"):
            data["published_at"] = line.strip()
            continue
        if not data.get("title"):
            data["title"] = line.strip()
            continue
        body_head.append(line)

    body_lines = body_head + body
    content = "\n".join(body_lines).strip()

    # 发件人 "名字 <邮箱>" 拆开
    if data.get("sender"):
        m2 = NAME_MAIL_RE.match(data["sender"])
        if m2:
            if not data.get("publisher"):
                data["publisher"] = m2.group("name").strip()
            data["sender"] = m2.group("mail").strip()

    if content:
        data["content"] = content
    for k, v in defaults.items():                  # 兜底默认值，文本里写了就不覆盖
        if v and not data.get(k):
            data[k] = v
    if not data.get("title"):
        first = next((ln for ln in content.splitlines() if ln.strip()), "")
        if first:
            data["_title_preview"] = first.strip()[:120]   # 实际由服务端从正文首行取标题
    if not data.get("content") and not data.get("title"):
        return None
    if data.get("title"):
        data.pop("_title_preview", None)
    return data


def ext_id_guess(rec: dict) -> str:
    """复刻服务端 make_ext_id：source|title|published_at|sender —— 用于提前发现互相覆盖。"""
    src = rec.get("source") or "manual"
    title = rec.get("title") or rec.get("_title_preview") or ""
    raw = "|".join([src, title, rec.get("published_at") or "", rec.get("sender") or ""])
    return "auto-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# .eml 邮件解析（算法与 server.py 的 parse_eml_bytes 保持一致，
# 所以同一封邮件无论走脚本还是页面上传，算出的 ext_id 相同、不会重复入库）
# --------------------------------------------------------------------------
def decode_mime(raw) -> str:
    if not raw:
        return ""
    out = []
    for text, enc in decode_header(str(raw)):
        if isinstance(text, bytes):
            for e in ([enc] if enc else []) + ["utf-8", "gb18030", "latin-1"]:
                try:
                    out.append(text.decode(e))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def parse_eml(path: str, defaults: dict) -> dict:
    """一封邮件 = 一条通知。"""
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parsebytes(f.read())

    subject = decode_mime(msg.get("Subject")).strip()
    from_name, from_addr = parseaddr(decode_mime(msg.get("From")))

    published = ""
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(str(msg.get("Date")))
            if dt.tzinfo is not None:
                dt = dt.astimezone(APP_TZ)
            published = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            published = ""

    ext_id = (msg.get("Message-ID") or "").strip()
    if ext_id:
        ext_id = "eml" + hashlib.sha1(ext_id.encode("utf-8")).hexdigest()[:28]

    text_body = html_body = ""
    attachments = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if part.get_content_disposition() == "attachment" or (
            filename and part.get_content_maintype() != "text"
        ):
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

    headers = [f"{h}: {decode_mime(msg.get(h))}" for h in ("To", "Cc", "Reply-To") if msg.get(h)]
    rec = {
        "ext_id": ext_id,
        "title": subject or "（无主题邮件）",
        "content": (text_body or html_to_text(html_body)).strip(),
        "source": "outlook",
        "sender": from_addr or "",
        "publisher": decode_mime(from_name) or from_addr or "",
        "published_at": published,
        "attachments": attachments,
        "origin": "\n".join(headers),
    }
    rec["_file"] = os.path.basename(path)
    for k, v in defaults.items():
        if v and not rec.get(k):
            rec[k] = v
    return rec


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
TEXT_EXT = (".txt", ".md")
MAIL_EXT = (".eml",)
# 这些子目录不扫：归档区 + 原始 .msg 存放区（.msg 要先转成 .eml）
SKIP_DIRS = {"processed", "done", "msg_done", ".git", "__pycache__", "backups"}


def collect_files(args) -> list[str]:
    files = list(args.file or [])
    dirs = list(args.dir or [])
    inbox = os.path.join(BASE_DIR, "inbox")
    if not files and not dirs and os.path.isdir(inbox):
        dirs = [inbox]
        print(f"（没指定 --file/--dir，默认扫描 {inbox}）")
    for d in dirs:
        for root, sub, names in os.walk(d):
            sub[:] = [x for x in sub if x not in SKIP_DIRS]
            for n in sorted(names):
                if n.startswith(".") or n.lower().startswith("readme"):
                    continue
                if os.path.splitext(n)[1].lower() in TEXT_EXT + MAIL_EXT + (".msg",):
                    files.append(os.path.join(root, n))
    return files


def read_text(path: str, encoding: str) -> str:
    raw = open(path, "rb").read()
    for enc in ([encoding] if encoding else []) + ["utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def post_ingest(base: str, token: str, items: list, batch: int, timeout: int) -> dict:
    total = {"parsed": 0, "created": 0, "updated": 0}
    payload = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
    for i in range(0, len(payload), batch):
        chunk = payload[i: i + batch]
        req = urllib.request.Request(
            base.rstrip("/") + "/api/ingest",
            data=json.dumps({"items": chunk}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Sync-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"入库失败 HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
        except urllib.error.URLError as e:
            raise SystemExit(f"连不上 {base}：{e}")
        for k in total:
            total[k] += int(res.get(k) or 0)
        print(f"  已入库 {min(i + batch, len(items))}/{len(items)}  → 新增 {res.get('created')} 更新 {res.get('updated')}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="批量导入纯文本通知到 notice-hub（幂等，可重复跑）")
    ap.add_argument("--file", action="append", help="输入文件（可重复），.txt/.md/.eml")
    ap.add_argument("--dir", action="append", help="输入目录（可重复），递归 .txt/.md/.eml；不指定则默认扫 inbox/")
    ap.add_argument("--split", default="auto",
                    choices=["auto", "dash", "mail", "date", "blank", "whole"],
                    help="分段策略，默认 auto：依次尝试 --- 分隔 → 邮件头部 → 日期行 → 空行 → 整文件一条")
    ap.add_argument("--encoding", default="", help="强制指定编码，默认自动（utf-8-sig/utf-8/gb18030/gbk）")
    # 兜底字段：文本里没写就用这里的值
    ap.add_argument("--source", default="", help="来源兜底：企微 / Outlook / 手工 / 其它")
    ap.add_argument("--category", default="", help="分类兜底，如 网络变更 / 故障公告 / 安全通告 / 系统维护")
    ap.add_argument("--level", default="", help="级别兜底：紧急 / 重要 / 普通")
    ap.add_argument("--tags", default="", help="标签兜底，逗号分隔")
    ap.add_argument("--source-name", default="", help="群名称/邮件文件夹 兜底")
    ap.add_argument("--publisher", default="", help="发布人兜底")
    # 目标与鉴权
    ap.add_argument("--base", default="http://10.127.207.71:8091", help="notice-hub 地址")
    ap.add_argument("--token", default="", help="同步令牌，默认读 ../.env 的 SYNC_TOKEN")
    ap.add_argument("--batch", type=int, default=200, help="每次提交多少条，默认 200")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP 超时秒")
    ap.add_argument("--dry-run", action="store_true", help="只解析并打印清单，不入库")
    ap.add_argument("--json-out", default="", help="把解析结果写成 JSON 文件（便于二次检查/改写）")
    ap.add_argument("--show-body", type=int, default=0, help="打印每条正文前 N 个字符（默认 0=只打印长度）")
    ap.add_argument("--move-done", default="", help="入库成功后把文件移到该目录，例如 /data/notice-hub/inbox/processed（避免下次重复扫）")
    args = ap.parse_args()

    files = collect_files(args)
    if not files:
        raise SystemExit("没有输入文件。用 --file 或 --dir 指定（.txt/.md）")

    defaults = {
        "source": args.source, "category": args.category, "level": args.level,
        "tags": args.tags, "source_name": args.source_name, "publisher": args.publisher,
    }
    defaults = {k: v for k, v in defaults.items() if v}

    all_items: list[dict] = []
    per_file: list[tuple[str, int]] = []
    print(f"输入 {len(files)} 个文件：")
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        items: list[dict] = []
        if ext in MAIL_EXT:
            try:
                items = [parse_eml(path, defaults)]
            except Exception as e:  # noqa: BLE001
                print(f"  {path}  [邮件]  解析失败：{e!r}")
            if items:
                att = items[0].get("attachments") or []
                print(f"  {path}  [邮件]  {items[0].get('published_at') or '(无日期)'}  "
                      f"{items[0].get('publisher') or items[0].get('sender') or '-'}  "
                      f"「{(items[0].get('title') or '')[:50]}」  "
                      f"正文 {len(items[0].get('content') or '')} 字"
                      + (f"  附件 {len(att)} 个：{', '.join(a[:20] for a in att[:3])}" if att else ""))
        elif ext == ".msg":
            print(f"  {path}\n      ✗ Outlook 的 .msg 是私有二进制格式，Python 标准库读不了。")
            print("        请在 Outlook 里选中邮件 → 文件 → 另存为 → 格式选「Outlook 邮件格式 (*.eml)」；")
            print("        最省事的办法是直接把邮件从 Outlook 拖到桌面，得到的通常就是 .eml。")
        elif ext in TEXT_EXT:
            text = read_text(path, args.encoding)
            blocks, used = split_blocks(text, args.split)
            preambles = [b for b in blocks if b.startswith("__PREAMBLE__")]
            blocks = [b for b in blocks if not b.startswith("__PREAMBLE__")]
            items = [r for r in (parse_record(b, defaults) for b in blocks) if r]
            print(f"  {path}  [{len(text)} 字符]  策略={used}  解析出 {len(items)} 条"
                  + (f"  （另有 {len(preambles)} 段前言已忽略）" if preambles else ""))
            for p in preambles:
                snip = p.replace("__PREAMBLE__", "").strip().splitlines()[0][:60]
                print(f"      ! 前言未导入：{snip}…（要保留就手动加 --- 分隔后重跑）")
        else:
            print(f"  {path}  跳过（不支持的扩展名 {ext}）")
        per_file.append((path, len(items)))
        all_items.extend(items)

    if not all_items:
        raise SystemExit("\n没解析到任何通知。文件内容长什么样？先把前 30 行发我，或者用 --split whole 整篇当一条。")

    # 预览
    print(f"\n解析结果预览（共 {len(all_items)} 条）：")
    seen: dict[str, list[int]] = {}
    for n, it in enumerate(all_items, 1):
        title = it.get("title") or it.get("_title_preview") or "(无标题)"
        body = it.get("content") or ""
        src_file = f"  <{it['_file']}>" if it.get("_file") else ""
        line = (f"  [{n:>3}] {it.get('published_at','(无时间)')}  "
                f"{it.get('source','-')}/{it.get('level','-')}/{it.get('category','-')}  "
                f"{title[:60]}  (正文 {len(body)} 字){src_file}")
        print(line)
        if args.show_body:
            print("        " + body[: args.show_body].replace("\n", " ⏎ "))
        seen.setdefault(it.get("ext_id") or ext_id_guess(it), []).append(n)
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    if dup:
        print("\n⚠️ 以下条目会被当成同一条互相覆盖（ext_id 相同 = 来源+标题+时间+发件人 全一样）：")
        for _k, ns in dup.items():
            print("   序号 " + ", ".join(map(str, ns)) + "  → " + (all_items[ns[0] - 1].get("title") or all_items[ns[0] - 1].get("_title_preview") or "")[:60])
        print("   解决：给每条补上不同的发布时间，或换个标题。")
    no_time = [n for n, it in enumerate(all_items, 1) if not it.get("published_at")]
    if no_time:
        print(f"⚠️ {len(no_time)} 条没有时间，入库时会用「导入时刻」当发布时间：序号 {no_time[:10]}{' …' if len(no_time) > 10 else ''}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        print(f"\n解析结果已写出：{args.json_out}")

    if args.dry_run:
        print("\n--dry-run：没有写库。确认无误后去掉 --dry-run 重跑即可。")
        return

    token = args.token or ""
    if not token:
        env_path = os.path.join(BASE_DIR, ".env")
        if os.path.isfile(env_path):
            m = re.search(r"^\s*SYNC_TOKEN\s*=\s*(\S+)\s*$", read_text(env_path, "utf-8"), re.MULTILINE)
            token = m.group(1) if m else ""
    if not token:
        raise SystemExit("没有同步令牌。用 --token 指定，或确认 ../.env 里有 SYNC_TOKEN")

    print(f"\n开始入库到 {args.base}（每批 {args.batch} 条）…")
    total = post_ingest(args.base, token, all_items, args.batch, args.timeout)
    print(f"完成：解析 {total['parsed']} 条，新增 {total['created']}，更新 {total['updated']}")
    print("（同样的文本再跑一次会是「新增 0 / 更新 N」，这就是幂等生效，不用担心重复）")

    if args.move_done:
        os.makedirs(args.move_done, exist_ok=True)
        moved, failed = 0, 0
        for path, cnt in per_file:
            if not cnt:
                continue
            try:
                dest = os.path.join(args.move_done, os.path.basename(path))
                if os.path.exists(dest):
                    dest = dest + "." + hashlib.sha1(path.encode()).hexdigest()[:6]
                shutil.move(path, dest)
                moved += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  移动失败 {path}：{e!r}")
        print(f"已移动 {moved} 个文件到 {args.move_done}" + (f"，{failed} 个失败" if failed else ""))


if __name__ == "__main__":
    main()
