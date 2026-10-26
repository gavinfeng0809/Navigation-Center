#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inbox 自动处理流水线：丢进 inbox/ 的邮件自动变成「通知 + 原文页 + 可下载附件」。

做四件事（幂等，可反复跑）：
  1. inbox/*.msg          → 转成 .eml（归档 inbox/processed/，原 .msg 挪 inbox/msg_done/）
  2. inbox 顶层的新邮件   → 归档进 processed/，并生成静态原文页到 public/originals/
  3. 自动入库            → 正文 + 【附件】 + 【查看原文】链接，打上「待汇总」标签
  4. 记账                → data/inbox_state.json 记下已入库的 ext_id

**已入库过的 ext_id 一定跳过**（按 ext_id 去重，不看文件名）：
否则同一封邮件再丢一次，就会把已经写好的摘要覆盖回原始正文。

摘要（关键字 / 一句话 / 要点）不在这里做——那需要模型，由 Hermes 按「待汇总」标签补。

用法：
    python3 tools/inbox_pipeline.py --dry-run      # 只打印要干什么，不写库
    python3 tools/inbox_pipeline.py                # 真跑（systemd 服务跑的就是这个）
    python3 tools/inbox_pipeline.py --base http://127.0.0.1:8099 --token xxx \
        --state /tmp/s.json --public-dir /tmp/orig --inbox-dir /tmp/inbox   # 临时实例验证

依赖：.msg 转换需要 /data/notice-hub/.venv/bin/python（装了 extract-msg），其余纯标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "/data/notice-hub"
TOOLS = os.path.join(BASE, "tools")
VENV_PY = os.path.join(BASE, ".venv", "bin", "python")
TEXT_EXT = (".txt", ".md")
MAIL_EXT = (".eml",)

sys.path.insert(0, TOOLS)
import import_text as it  # noqa: E402  复用它的解析器和 ext_id 规则


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def run(cmd: list) -> bool:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log(f"  ! 命令失败：{(p.stderr or p.stdout).strip()[:300]}")
        return False
    return True


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.inbox = args.inbox_dir
        self.processed = os.path.join(self.inbox, "processed")
        self.msg_done = os.path.join(self.inbox, "msg_done")
        self.public = args.public_dir
        self.py = VENV_PY if os.path.exists(VENV_PY) else sys.executable

    # ---------- 1. .msg → .eml ----------
    def convert_msgs(self) -> None:
        msgs = [f for f in sorted(os.listdir(self.inbox))
                if os.path.isfile(os.path.join(self.inbox, f)) and f.lower().endswith(".msg")]
        if not msgs:
            return
        log(f"发现 {len(msgs)} 个 .msg，开始转换")
        conv = os.path.join(TOOLS, "msg_to_eml.py")
        ok = 0
        for name in msgs:
            src = os.path.join(self.inbox, name)
            if run([self.py, conv, "--file", src, "--out", self.processed,
                    "--overwrite", "--move-done", self.msg_done, "--max-attach-mb", "50"]):
                ok += 1
                log(f"  ✓ {name} → .eml")
        log(f"  .msg 转换完成 {ok}/{len(msgs)}")

    # ---------- 2. 归档顶层新文件 ----------
    def archive_incoming(self) -> None:
        os.makedirs(self.processed, exist_ok=True)
        moved = 0
        for name in sorted(os.listdir(self.inbox)):
            p = os.path.join(self.inbox, name)
            if not os.path.isfile(p):
                continue
            # 说明文档（README*）和隐藏文件留着原地，不当邮件收走
            if name.startswith(".") or name.lower().startswith("readme"):
                continue
            if os.path.splitext(name)[1].lower() in MAIL_EXT + TEXT_EXT:
                dest = os.path.join(self.processed, name)
                if os.path.exists(dest):
                    dest = f"{dest}.{int(time.time())}"
                shutil.move(p, dest)
                moved += 1
        if moved:
            log(f"归档 {moved} 个新文件到 processed/")

    def candidate_files(self) -> list:
        out = []
        for d in (self.processed, self.inbox):
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.startswith(".") or name.lower().startswith("readme"):
                    continue
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.path.splitext(name)[1].lower() in MAIL_EXT + TEXT_EXT:
                    out.append(p)
        return out

    # ---------- 3. 原文页 ----------
    def make_originals(self) -> dict:
        """从 .msg 归档 + processed/ 的 .eml 重建原文页。
        同时给 msg_done 是为了保真：.msg 里带全部附件二进制，.eml 可能是限额版本；
        同一封邮件两边都有时 slug 相同，后写的 .eml 版本覆盖前面的，所以转换时把上限放宽到 50MB。"""
        gen = os.path.join(TOOLS, "make_originals.py")
        cmd = [self.py, gen, "--out", self.public]
        if os.path.isdir(self.msg_done):
            cmd += ["--msg-dir", self.msg_done]
        if os.path.isdir(self.processed):
            cmd += ["--eml-dir", self.processed]
        if len(cmd) <= 4:
            return {}
        if not run(cmd):
            return {}
        man = os.path.join(self.public, "manifest.json")
        if not os.path.isfile(man):
            return {}
        with open(man, encoding="utf-8") as f:
            items = json.load(f)
        return {x["source_file"]: x for x in items}

    # ---------- 4. 解析 ----------
    def parse_file(self, path: str) -> dict | None:
        name = os.path.basename(path)
        if os.path.splitext(name)[1].lower() == ".eml":
            return it.parse_eml(path, {})
        text = it.read_text(path, "")
        blocks, _ = it.split_blocks(text, "auto")
        blocks = [b for b in blocks if not b.startswith("__PREAMBLE__")]
        items = [r for r in (it.parse_record(b, {}) for b in blocks) if r]
        if not items:
            return None
        if len(items) > 1:
            log(f"  ! {name} 解析出 {len(items)} 条，本次只处理第一条（请拆文件）")
        return items[0]

    def build_content(self, rec: dict, origin: dict) -> str:
        atts = rec.get("attachments") or []
        att_line = ("【附件】" + "；".join(atts) + f"（{len(atts)} 个，原文页可下载）") if atts \
            else "【附件】无（原邮件未带附件）"
        parts = [(rec.get("content") or "").strip(), "", att_line]
        if origin:
            parts.append(f"【查看原文】{self.args.base.rstrip('/')}/originals/{origin['slug']}.html")
        return "\n".join(parts).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="inbox 自动处理：邮件 → 原文页 + 入库")
    ap.add_argument("--base", default="http://10.0.0.10:8091")
    ap.add_argument("--token", default="", help="同步令牌，默认读 .env")
    ap.add_argument("--state", default=os.path.join(BASE, "data", "inbox_state.json"))
    ap.add_argument("--public-dir", default=os.path.join(BASE, "public", "originals"))
    ap.add_argument("--inbox-dir", default=os.path.join(BASE, "inbox"))
    ap.add_argument("--tags", default="待汇总", help="自动打的标签，逗号分隔；留空不打")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show-body", type=int, default=0, help="dry-run 时打印每条正文前 N 字")
    args = ap.parse_args()

    if not os.path.isdir(args.inbox_dir):
        log(f"! 目录不存在：{args.inbox_dir}")
        sys.exit(1)
    os.makedirs(args.public_dir, exist_ok=True)

    state = {}
    if os.path.isfile(args.state):
        try:
            with open(args.state, encoding="utf-8") as f:
                state = json.load(f)
        except (ValueError, OSError):
            log("! 状态文件读不了，按空处理")
    log(f"状态记录 {len(state)} 条已入库邮件")

    p = Pipeline(args)
    p.convert_msgs()
    p.archive_incoming()
    manifest = p.make_originals()

    files = p.candidate_files()
    log(f"归档区 {len(files)} 个文件，开始比对")

    fresh = []          # [(name, path, ext_id, rec, origin)]
    for path in files:
        name = os.path.basename(path)
        try:
            rec = p.parse_file(path)
        except Exception as e:  # noqa: BLE001
            log(f"  ! 解析失败 {name}: {e!r}")
            continue
        if not rec:
            log(f"  ! 没解析出内容，跳过 {name}")
            continue
        ext_id = rec.get("ext_id") or it.ext_id_guess(rec)
        if ext_id in state:
            continue
        fresh.append((name, path, ext_id, rec, manifest.get(name) or {}))

    log(f"新邮件 {len(fresh)} 个（其余 {len(files) - len(fresh)} 个已入库过，跳过）")
    if not fresh:
        log("没有新东西，结束")
        return

    tags = ",".join(t for t in re.split(r"[,，]", args.tags) if t.strip()) if args.tags else ""
    payload = []
    for name, _path, _eid, rec, origin in fresh:
        item = {k: v for k, v in rec.items() if not k.startswith("_")}
        item["content"] = p.build_content(rec, origin)
        if tags:
            item["tags"] = tags
        payload.append(item)
        log(f"  · {name}  「{(item.get('title') or '')[:38]}」  "
            f"正文 {len(item['content'])} 字  原文页 {'有' if origin else '无'}")

    if args.dry_run:
        log("--dry-run：没有写库。第一条最终正文如下：")
        print("-" * 60)
        body = payload[0]["content"]
        print(body if not args.show_body else body[: args.show_body])
        print("-" * 60)
        return

    token = args.token
    if not token:
        envp = os.path.join(BASE, ".env")
        if os.path.isfile(envp):
            m = re.search(r"^\s*SYNC_TOKEN\s*=\s*(\S+)\s*$", it.read_text(envp, "utf-8"), re.MULTILINE)
            token = m.group(1) if m else ""
    if not token:
        log("! 没有同步令牌，终止")
        sys.exit(1)

    try:
        req = urllib.request.Request(
            args.base.rstrip("/") + "/api/ingest",
            data=json.dumps({"items": payload}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Sync-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        log(f"! 入库失败：{e}")
        sys.exit(1)

    log(f"入库完成：解析 {res.get('parsed')} 新增 {res.get('created')} 更新 {res.get('updated')}")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for name, _path, ext_id, _rec, origin in fresh:
        state[ext_id] = {"file": name, "at": now, "slug": origin.get("slug", "")}
    tmp = args.state + ".tmp"
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.state)
    log(f"状态已更新：{len(state)} 条（重复丢同一封邮件不会再入库）")


if __name__ == "__main__":
    main()
