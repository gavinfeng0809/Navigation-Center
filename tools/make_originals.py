#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把邮件原文生成成静态 HTML 页（供通知中心正文末尾的「查看原文」链接打开）。

输入：Outlook .msg（需要 extract-msg）或 .eml（纯标准库）
产出：
    <out>/<日期>-<随机串>.html    每封邮件一页：头部信息 + 完整正文 + 附件下载链接
    <out>/files/<附件名>          真附件实体（内嵌签名图默认不要）
    <out>/manifest.json           slug / 主题 / 附件 / 字节数 清单，用于回写通知正文

用法：
    /data/notice-hub/.venv/bin/python tools/make_originals.py --msg-dir inbox/msg_done --out /tmp/originals
    python3 tools/make_originals.py --eml-dir inbox/converted --out /tmp/originals

说明：
- 文件名带 16 位哈希，避免可猜；页面内不带导航，纯原文，方便直接丢给链接。
- 附件实体一并导出，原始页里给下载链接（内嵌图片不算附件）。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import timedelta, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

APP_TZ = timezone(timedelta(hours=8))
BAD_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name: str, fallback: str = "attachment.bin") -> str:
    n = (name or "").replace("\\", "/").split("/")[-1]
    n = BAD_FN.sub("_", n).strip() or fallback
    return n[:120]


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


def load_email(path: str) -> dict:
    """统一读出 {subject, sender_name, sender_addr, to, cc, date, message_id, body, attachments:[(name, bytes)]}"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".msg":
        try:
            import extract_msg  # noqa: PLC0415
        except ImportError:
            sys.exit("读 .msg 需要 extract-msg，请用 /data/notice-hub/.venv/bin/python 运行")
        with extract_msg.openMsg(path) as m:
            header = getattr(m, "header", None)
            dt = getattr(m, "date", None)
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=APP_TZ)
            atts = []
            for a in m.attachments:
                try:
                    cid = str(getattr(a, "cid", None) or "").strip()
                    if cid:            # 内嵌图片（签名/截图）不算真附件
                        continue
                    name = (getattr(a, "longFilename", None) or getattr(a, "shortFilename", None)
                            or (a.getFilename() if hasattr(a, "getFilename") else None))
                    data = getattr(a, "data", None)
                    if isinstance(data, (bytes, bytearray)):
                        atts.append((safe_name(name), bytes(data)))
                except Exception:  # noqa: BLE001
                    continue
            sender = m.sender or ""
            name, addr = parseaddr(sender)
            return {
                "subject": m.subject or "(无主题)",
                "sender_name": name, "sender_addr": addr,
                "to": m.to or "", "cc": m.cc or "",
                "date": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
                "message_id": (getattr(m, "messageId", "") or "").strip(),
                "body": (m.body or "").strip(),
                "attachments": atts,
            }

    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parsebytes(f.read())
    dt = None
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(str(msg.get("Date")))
            if dt.tzinfo is not None:
                dt = dt.astimezone(APP_TZ)
        except Exception:  # noqa: BLE001
            dt = None
    text_body = html_body = ""
    atts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        fname = part.get_filename()
        disp = part.get_content_disposition()
        if disp == "attachment" or (fname and part.get_content_maintype() != "text"):
            if not fname:
                continue
            if disp == "inline" and part.get_content_maintype() == "image":
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, (bytes, bytearray)):
                atts.append((safe_name(decode_mime(fname)), bytes(payload)))
            continue
        try:
            payload = part.get_content()
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, str):
            continue
        if part.get_content_type() == "text/plain" and not text_body:
            text_body = payload
        elif part.get_content_type() == "text/html" and not html_body:
            html_body = payload
    name, addr = parseaddr(decode_mime(msg.get("From")))
    if not text_body and html_body:
        text_body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_body)
        text_body = re.sub(r"(?i)<br\s*/?>", "\n", text_body)
        text_body = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text_body)
        text_body = re.sub(r"(?is)<[^>]+>", "", text_body)
        import html as _h  # noqa: PLC0415
        text_body = _h.unescape(text_body)
    return {
        "subject": decode_mime(msg.get("Subject")) or "(无主题)",
        "sender_name": decode_mime(name), "sender_addr": addr,
        "to": decode_mime(msg.get("To")), "cc": decode_mime(msg.get("Cc")),
        "date": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
        "message_id": (msg.get("Message-ID") or "").strip(),
        "body": text_body.strip(),
        "attachments": atts,
    }


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{subject}</title>
<style>
 :root{{color-scheme:light dark}}
 *{{box-sizing:border-box}}
 body{{margin:0;padding:20px;font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background:#f5f6f8;color:#1f2328}}
 .wrap{{max-width:900px;margin:0 auto}}
 .card{{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:22px 24px;margin-bottom:16px}}
 h1{{font-size:19px;line-height:1.5;margin:0 0 14px}}
 table{{border-collapse:collapse;width:100%;font-size:13.5px}}
 td{{padding:5px 0;vertical-align:top}}
 td.k{{color:#6b7280;width:82px;white-space:nowrap}}
 .body{{white-space:pre-wrap;word-wrap:break-word;font-size:14.5px;line-height:1.85}}
 .quote{{color:#8b8f96}}
 .att a{{color:#1d6fe0;text-decoration:none}} .att a:hover{{text-decoration:underline}}
 .tag{{display:inline-block;background:#eef2f7;border-radius:6px;padding:2px 8px;font-size:12.5px;color:#4b5563;margin-left:6px}}
 .foot{{color:#8b8f96;font-size:12.5px;text-align:center;padding:6px 0 20px}}
 a.back{{color:#1d6fe0;text-decoration:none;font-size:13.5px}}
 @media (prefers-color-scheme:dark){{
   body{{background:#16181d;color:#e6e8eb}}
   .card{{background:#1e2127;border-color:#2e3238}}
   .tag{{background:#2a2e35;color:#c3c8d0}} .foot{{color:#7c828c}}
   a.back,.att a{{color:#6aa8ff}}
 }}
</style></head><body><div class="wrap">
<div class="card">
  <h1>{subject}</h1>
  <table>
    <tr><td class="k">发件人</td><td>{sender}</td></tr>
    <tr><td class="k">收件人</td><td>{to}</td></tr>
    {cc_row}
    <tr><td class="k">时间</td><td>{date} <span class="tag">UTC+8</span></td></tr>
    {mid_row}
    <tr><td class="k">来源文件</td><td>{srcfile}</td></tr>
  </table>
</div>
{att_card}
<div class="card"><div class="body{quote_cls}">{body}</div></div>
<div class="foot"><a class="back" href="/#/notices">← 返回通知中心</a>　·　邮件原文存档页（自动生成，请勿编辑）</div>
</div></body></html>
"""


def build_page(info: dict, att_links: list, srcfile: str) -> str:
    def esc(s: str) -> str:
        return html.escape(s or "", quote=True)

    sender = f"{esc(info['sender_name'])} &lt;{esc(info['sender_addr'])}&gt;" if info["sender_addr"] else esc(info["sender_name"])
    cc_row = f'<tr><td class="k">抄送</td><td>{esc(info["cc"])}</td></tr>' if info["cc"] else ""
    mid_row = f'<tr><td class="k">Message-ID</td><td>{esc(info["message_id"])}</td></tr>' if info["message_id"] else ""
    if att_links:
        items = "".join(
            f'<div class="att">📎 <a href="files/{esc(a["file"])}" download="{esc(a["name"])}">{esc(a["name"])}</a>'
            f'<span class="tag">{a["size"]}</span></div>' for a in att_links)
        att_card = f'<div class="card"><b>附件（{len(att_links)}）</b><div style="margin-top:8px">{items}</div></div>'
    else:
        att_card = ('<div class="card"><b>附件</b><div style="margin-top:6px;color:#8b8f96;font-size:13.5px">'
                    '无（原邮件仅含正文内嵌图片，已省略）</div></div>')
    return PAGE.format(
        subject=esc(info["subject"]), sender=sender, to=esc(info["to"]) or "—",
        cc_row=cc_row, date=esc(info["date"]) or "未知", mid_row=mid_row,
        srcfile=esc(srcfile), att_card=att_card,
        body=esc(info["body"]), quote_cls="",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="邮件原文 → 静态 HTML 页（供「查看原文」链接）")
    ap.add_argument("--msg-dir", action="append", help=".msg 目录（可重复）")
    ap.add_argument("--eml-dir", action="append", help=".eml 目录（可重复）")
    ap.add_argument("--file", action="append", help="单个邮件文件（可重复）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--no-attachments", action="store_true", help="不导出附件实体，只列文件名")
    args = ap.parse_args()

    files = list(args.file or [])
    dirs = [(d, ".msg") for d in (args.msg_dir or [])] + [(d, ".eml") for d in (args.eml_dir or [])]
    for d, want in dirs:
        if d:
            for root, sub, names in os.walk(d):
                sub[:] = [x for x in sub if x not in ("processed", "converted", "msg_done")]
                for n in sorted(names):
                    if n.lower().endswith(want):
                        files.append(os.path.join(root, n))
    if not files:
        sys.exit("没有输入：用 --msg-dir / --eml-dir / --file 指定")

    files_dir = os.path.join(args.out, "files")
    os.makedirs(files_dir, exist_ok=True)
    manifest = []
    print(f"共 {len(files)} 封：")
    for path in files:
        info = load_email(path)
        key = info["message_id"] or (info["subject"] + info["date"])
        slug = f"{info['date'][:10]}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
        att_links = []
        for idx, (fn, data) in enumerate(info["attachments"], 1):
            tag = f"{len(data) / 1048576:.1f} MB" if len(data) >= 1048576 else f"{max(1, len(data) // 1024)} KB"
            # 实体文件名用 ASCII：服务端 _route() 没有对 URL 做 unquote，
            # 中文文件名在浏览器里会被百分号编码 → 必然 404。显示名保留中文。
            ext = os.path.splitext(fn)[1].lower() or ".bin"
            ascii_fn = f"att{idx}-{hashlib.sha1(fn.encode('utf-8')).hexdigest()[:8]}{ext}"
            att_links.append({"file": ascii_fn, "name": fn, "size": tag})
            if not args.no_attachments:
                with open(os.path.join(files_dir, ascii_fn), "wb") as f:
                    f.write(data)
        page = build_page(info, att_links, os.path.basename(path))
        outfile = os.path.join(args.out, slug + ".html")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"  ✓ {slug}.html  「{info['subject'][:44]}」 正文 {len(info['body'])} 字，附件 {len(att_links)} 个")
        manifest.append({
            "slug": slug, "subject": info["subject"], "date": info["date"],
            "message_id": info["message_id"], "sender": f"{info['sender_name']} <{info['sender_addr']}>",
            "body_len": len(info["body"]), "attachments": att_links,
            "source_file": os.path.basename(path),
        })
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n输出目录 {args.out}")
    print("下一步：把整个目录拷到 /data/notice-hub/public/originals/ 并重建镜像")


if __name__ == "__main__":
    main()
