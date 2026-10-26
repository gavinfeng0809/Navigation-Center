#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Outlook 的 .msg 批量转成 .eml，方便交给 import_text.py 导入「通知中心」。

为什么要转：.msg 是 OLE 复合二进制格式，Python 标准库读不了；
            .eml 是标准 MIME 文本，server.py 自带解析器。

依赖 extract-msg（不是标准库），所以要用装了它的 python 跑：
    /data/notice-hub/.venv/bin/python tools/msg_to_eml.py --dir inbox
    或先 source 那个 venv 再 python tools/msg_to_eml.py ...

用法：
    # 看转换结果，不落盘
    python tools/msg_to_eml.py --dir inbox --dry-run

    # 真转换：.msg 写进 inbox/converted/*.eml，原 .msg 挪到 inbox/msg_done/
    python tools/msg_to_eml.py --dir inbox --move-done inbox/msg_done

    # 只看某个文件抽出来什么（排查用）
    python tools/msg_to_eml.py --file "某个邮件.msg" --inspect

转换规则：
- 主题/发件人/收件人/抄送/日期 原样搬过去；Message-ID 保留 → 导入时的 ext_id
  和服务端算法一致，重复导入不会新增记录
- 正文优先取纯文本；没有纯文本就放 HTML（导入脚本会转成文本）
- 附件一并搬过去；超过 --max-attach-mb 的大附件只保留文件名（不把几 MB 塞进库里）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

APP_TZ = timezone(timedelta(hours=8))
BAD_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def load_extract_msg():
    try:
        import extract_msg  # noqa: PLC0415
        return extract_msg
    except ImportError:
        sys.exit(
            "缺少 extract-msg。请用装了它的解释器运行，例如：\n"
            "    /data/notice-hub/.venv/bin/python tools/msg_to_eml.py --dir inbox\n"
            "（安装：python3 -m venv /data/notice-hub/.venv && "
            "/data/notice-hub/.venv/bin/pip install extract-msg）"
        )


def safe_name(name: str, fallback: str = "attachment.bin") -> str:
    n = (name or "").replace("\\", "/").split("/")[-1]
    n = BAD_FN.sub("_", n).strip() or fallback
    return n[:120]


def pick_header(msg, header, key: str) -> str:
    try:
        if header is not None and header.get(key):
            return str(header.get(key))
    except Exception:  # noqa: BLE001
        pass
    return ""


def convert_one(extract_msg, path: str, max_attach_mb: float, keep_inline: bool = False,
                inspect: bool = False) -> EmailMessage:
    with extract_msg.openMsg(path) as msg:
        header = getattr(msg, "header", None)

        subject = msg.subject or pick_header(msg, header, "Subject") or "(无主题)"
        sender = msg.sender or pick_header(msg, header, "From") or ""
        to = msg.to or pick_header(msg, header, "To") or ""
        cc = msg.cc or pick_header(msg, header, "Cc") or ""
        mid = (getattr(msg, "messageId", "") or pick_header(msg, header, "Message-ID") or "").strip()

        dt = getattr(msg, "date", None)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TZ)

        text_body = (msg.body or "").strip()
        html_body = ""
        raw_html = getattr(msg, "htmlBody", None)
        if raw_html:
            html_body = raw_html.decode("utf-8", "replace") if isinstance(raw_html, bytes) else str(raw_html)

        if inspect:
            print(f"  文件      : {os.path.basename(path)}")
            print(f"  主题      : {subject}")
            print(f"  发件人    : {sender}")
            print(f"  收件人    : {to[:80]}")
            print(f"  抄送      : {cc[:80]}")
            print(f"  日期      : {dt}")
            print(f"  Message-ID: {mid or '(无)'}")
            print(f"  纯文本正文: {len(text_body)} 字    HTML 正文: {len(html_body)} 字")
            print(f"  附件      : {len(msg.attachments)} 个")

        em = EmailMessage()
        em["Subject"] = subject
        em["From"] = sender or "unknown@unknown"
        if to:
            em["To"] = to
        if cc:
            em["Cc"] = cc
        if mid:
            em["Message-ID"] = mid
        em["Date"] = format_datetime(dt) if dt else format_datetime(__import__("datetime").datetime.now(APP_TZ))
        if text_body:
            em.set_content(text_body)
            if html_body:
                em.add_alternative(html_body, subtype="html")
        elif html_body:
            em.set_content(html_body, subtype="html")
        else:
            em.set_content("(邮件正文为空)")

        cap = max_attach_mb * 1024 * 1024
        skipped = 0
        oversized = 0
        inline_dropped = 0
        for att in msg.attachments:
            try:
                cid = str(getattr(att, "cid", None) or "").strip()
                if cid and not keep_inline:
                    # cid 非空 = 正文里引用的内嵌图片（多半是邮件签名/截图），不是真附件
                    inline_dropped += 1
                    continue
                name = safe_name(
                    getattr(att, "longFilename", None)
                    or getattr(att, "shortFilename", None)
                    or (att.getFilename() if hasattr(att, "getFilename") else None)
                    or "attachment.bin"
                )
                data = getattr(att, "data", None)
                if not isinstance(data, (bytes, bytearray)):
                    skipped += 1
                    continue
                if len(data) > cap:
                    em.add_attachment(
                        f"[附件过大已省略：{name}，{len(data) / 1048576:.1f} MB]".encode(),
                        maintype="application", subtype="octet-stream", filename=name,
                    )
                    oversized += 1
                    continue
                maintype, _, subtype = (getattr(att, "mimetype", "") or "application/octet-stream").partition("/")
                em.add_attachment(bytes(data), maintype=maintype or "application",
                                  subtype=subtype or "octet-stream", filename=name)
            except Exception as e:  # noqa: BLE001
                skipped += 1
                if inspect:
                    print(f"    ! 附件跳过：{e!r}")
        if inspect:
            print(f"  转换结果  : 正文 {len(text_body or html_body)} 字，"
                  f"搬走附件 {len(msg.attachments) - skipped - oversized - inline_dropped} 个，"
                  f"过滤内嵌图片 {inline_dropped} 个，"
                  f"过大只留文件名 {oversized} 个，读不了 {skipped} 个")
        return em


def main() -> None:
    extract_msg = load_extract_msg()
    ap = argparse.ArgumentParser(description=".msg → .eml 批量转换（供 notice-hub 导入）")
    ap.add_argument("--file", action="append", help="单个 .msg（可重复）")
    ap.add_argument("--dir", action="append", help="目录（可重复），递归找 .msg")
    ap.add_argument("--out", default="", help="输出目录，默认 <输入目录>/converted")
    ap.add_argument("--move-done", default="", help="转换后把原 .msg 挪到该目录")
    ap.add_argument("--max-attach-mb", type=float, default=5.0, help="单个附件超过多少 MB 只留文件名，默认 5")
    ap.add_argument("--keep-inline-images", action="store_true",
                    help="保留正文内嵌图片（邮件签名图 image001.png 之类）。默认过滤掉，只留真正的附件")
    ap.add_argument("--overwrite", action="store_true", help="同名 .eml 已存在时覆盖")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--inspect", action="store_true", help="打印每个文件抽出来的字段")
    args = ap.parse_args()

    files = list(args.file or [])
    dirs = list(args.dir or [])
    if not files and not dirs:
        ap.error("用 --file 或 --dir 指定 .msg 文件")
    for d in dirs:
        for root, sub, names in os.walk(d):
            sub[:] = [x for x in sub if x not in ("converted", "msg_done", "processed")]
            for n in sorted(names):
                if n.lower().endswith(".msg"):
                    files.append(os.path.join(root, n))
    if not files:
        sys.exit("没有找到 .msg 文件")

    out_default = args.out or os.path.join(os.path.dirname(os.path.abspath(files[0])), "converted")
    if not args.dry_run:
        os.makedirs(out_default, exist_ok=True)

    print(f"共 {len(files)} 个 .msg：")
    ok = 0
    for path in files:
        try:
            em = convert_one(extract_msg, path, args.max_attach_mb,
                             args.keep_inline_images, args.inspect)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {os.path.basename(path)}  解析失败：{e!r}")
            continue
        stem = safe_name(os.path.splitext(os.path.basename(path))[0], "mail")
        dest = os.path.join(out_default, stem + ".eml")
        size = len(em.as_bytes())
        if args.dry_run:
            print(f"  · {stem[:60]}\n      → 会写出 {dest}  ({size} 字节)")
            ok += 1
            continue
        if os.path.exists(dest) and not args.overwrite:
            print(f"  = {os.path.basename(dest)} 已存在，跳过（--overwrite 可覆盖）")
            continue
        with open(dest, "wb") as f:
            f.write(em.as_bytes())
        print(f"  ✓ {os.path.basename(dest)}  ({size} 字节)")
        ok += 1
        if args.move_done:
            os.makedirs(args.move_done, exist_ok=True)
            try:
                shutil.move(path, os.path.join(args.move_done, os.path.basename(path)))
            except Exception as e:  # noqa: BLE001
                print(f"    ! 原 .msg 移动失败：{e!r}")

    print(f"\n完成：成功 {ok}/{len(files)}，输出目录 {out_default}")
    if not args.dry_run:
        print("下一步（先干跑看解析结果）：")
        print(f"    python3 tools/import_text.py --dir {out_default} --dry-run")


if __name__ == "__main__":
    main()
