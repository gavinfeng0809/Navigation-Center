#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Winbox 地址簿（.cdb）→ 导航中心「Winbox 客户登录」分组
========================================================

Winbox 导出的地址簿是**二进制**（不是文本），格式：

    magic  0D F0 1D C0
    重复：  [4 字节小端长度 N][N 字节记录]
            记录 = "M2" + 若干 flag 字节 + 字段序列
            字段 = [id:1][0x00][0x00][0x21][len:1][字符串 len 字节]

实测字段 id（依据 149 条真实样本反推，非官方文档）：
    0x01 地址        0x02 用户名     0x03 密码
    0x04 名称        0x06 归属(<own>) 0x08 备注（多数为空）

⚠️ **密码与用户名一律不入库、不进日志**。
   导航中心是给全组人看的（查看账号也能拿到 /api/nav 的完整 JSON），
   把 19 种共用密码写进去等于对所有人公开；本项目的既定策略是
   「不做凭据库，凭据由各人的 Winbox 自己保存」。
   本脚本解析它们只是为了校验字段位置，随后丢弃。

用法：
    python3 tools/import_winbox_cdb.py --cdb 123.cdb --dry-run    # 先看
    python3 tools/import_winbox_cdb.py --cdb 123.cdb              # 写入

    --group "Winbox 客户登录"   目标分组名（不存在则创建）
    --db      /data/notice-hub/data/notices.db
    --probe                     开启在线探测（默认关，见下方说明）
    --keep-dups                 同一地址的多条记录都保留（默认按地址去重，只留第一条）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE, "data", "notices.db")
WINBOX_PORT = 8291          # Winbox 默认端口；.cdb 里不存端口，按默认走

MAGIC = bytes.fromhex("0df01dc0")
FIELD_RE = re.compile(rb"([\x01-\x20])\x00\x00\x21([\x00-\xff])", re.S)


# ---------------------------------------------------------------- 解析

def parse_cdb(path: str) -> list:
    """返回 [{addr, name, user, pwd}] —— user/pwd 只用于校验，不落库。"""
    data = open(path, "rb").read()
    if data[:4] != MAGIC:
        raise SystemExit(f"不是 Winbox 地址簿：magic={data[:4].hex()}（期望 {MAGIC.hex()}）")

    off, recs = 4, []
    while off + 4 <= len(data):
        n = struct.unpack_from("<I", data, off)[0]
        if n == 0 or off + 4 + n > len(data):
            break
        recs.append(data[off + 4: off + 4 + n])
        off += 4 + n

    out = []
    for r in recs:
        d = {}
        for m in FIELD_RE.finditer(r):
            fid, ln = m.group(1)[0], m.group(2)[0]
            # setdefault：同 id 取第一次出现，和页面/客户端行为一致
            d.setdefault(fid, r[m.end(): m.end() + ln].decode("utf-8", "replace"))
        out.append({
            "addr": (d.get(0x01) or "").strip(),
            "name": (d.get(0x04) or "").strip(),
            "user": d.get(0x02) or "",
            "pwd":  d.get(0x03) or "",
        })
    return out


# ---------------------------------------------------------------- 组装

def site_prefix(name: str) -> str:
    """从名称里取站点前缀当副标题：SZX5-ULOO → SZX5；MikroTik → MikroTik。"""
    head = re.split(r"[-_.\s/]", name, maxsplit=1)[0]
    return head[:40]


def build_rows(records: list, keep_dups: bool, probe: bool) -> tuple:
    """去重并组装成 nav_items 字段。返回 (rows, 被合并掉的重复明细)。"""
    seen, rows, merged = {}, [], []
    for rec in records:
        addr, name = rec["addr"], rec["name"] or "MikroTik"
        if not addr:
            continue
        if addr in seen and not keep_dups:
            prev = seen[addr]
            if prev["name"] != name:          # 同地址不同名，值得报告
                merged.append((addr, prev["name"], name))
            continue
        item = {
            # 名称带地址：导出里有 38 条都叫 SZX5-ULOO，只有 IP 能区分
            "name": f"{name} · {addr}"[:60],
            "subgroup": site_prefix(name),
            # url 用 Winbox 端口而不是 80：在线探测会 TCP 连 8291，
            # 探到的正是「这台路由器能不能连 Winbox」，比探 80 有意义。
            "url": f"http://{addr}:{WINBOX_PORT}",
            "launch": "winbox",
            "launch_host": addr,
            "probe": 1 if probe else 0,
        }
        seen[addr] = {"name": name}
        rows.append(item)
    return rows, merged


# ---------------------------------------------------------------- 落库

def ensure_group(conn, group: str) -> int:
    row = conn.execute("SELECT id FROM nav_groups WHERE name=?", (group,)).fetchone()
    if row:
        return row[0]
    nxt = (conn.execute("SELECT COALESCE(MAX(sort),0)+1 FROM nav_groups").fetchone()[0])
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("INSERT INTO nav_groups(name, sort, created_at) VALUES(?,?,?)",
                       (group, nxt, ts))
    print(f"  新建分组「{group}」id={cur.lastrowid} sort={nxt}")
    return cur.lastrowid


def existing_hosts(conn) -> set:
    """库里已有的主机（所有分组），用来跳过重复导入。"""
    hosts = set()
    for (u,) in conn.execute("SELECT url FROM nav_items"):
        if not u:
            continue
        m = re.match(r"^[a-zA-Z]+://([^/:\s]+)", u.strip())
        hosts.add(m.group(1) if m else u.strip().split("/")[0].split(":")[0])
    return hosts


def main() -> int:
    ap = argparse.ArgumentParser(description="Winbox 地址簿 .cdb → 导航中心")
    ap.add_argument("--cdb", required=True, help="Winbox 导出的 .cdb 文件")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--group", default="Winbox 客户登录")
    ap.add_argument("--color", default="#0ea5e9")
    # 默认**不**开探测。实测：这批 MikroTik 的管理地址有一半以上是
    # 「服务器所在网段到不了」，80/443/22/8291 全关 —— 不是设备挂了。
    # 开了会在页面上显示一片红点，误导工程师以为设备宕机。
    ap.add_argument("--probe", action="store_true",
                    help="开启在线探测（TCP 8291）。注意服务器到不了的网段会全部显示离线")
    ap.add_argument("--keep-dups", action="store_true", help="同地址的多条都保留")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写库")
    args = ap.parse_args()

    records = parse_cdb(args.cdb)
    print(f"解析 {args.cdb}：{len(records)} 条记录"
          f"（带用户名 {sum(1 for r in records if r['user'])} 条、"
          f"带密码 {sum(1 for r in records if r['pwd'])} 条 —— 均不入库）")

    rows, merged = build_rows(records, args.keep_dups, args.probe)
    print(f"按地址去重后：{len(rows)} 条待新增")
    if merged:
        print(f"同地址不同名被合并 {len(merged)} 处（原文名 / 被丢弃的同地址名）：")
        for addr, kept, dropped in merged:
            print(f"   {addr:<18} 保留 {kept!r}，丢弃 {dropped!r}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    hosts = existing_hosts(conn)
    fresh = [r for r in rows if r["launch_host"] not in hosts]
    dup = len(rows) - len(fresh)
    print(f"与现有导航项重复被跳过：{dup} 条；实际新增 {len(fresh)} 条")

    if args.dry_run:
        print("\n--dry-run，前 8 条预览：")
        for r in fresh[:8]:
            print(f"   {r['name']:<32} sub={r['subgroup']:<8} url={r['url']}")
        conn.close()
        return 0

    if not fresh:
        print("没有需要新增的条目。")
        conn.close()
        return 0

    gid = ensure_group(conn, args.group)
    base_sort = conn.execute(
        "SELECT COALESCE(MAX(sort),0) FROM nav_items WHERE group_id=?", (gid,)).fetchone()[0]
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tags = json.dumps(["MikroTik", "Winbox"], ensure_ascii=False)

    for i, r in enumerate(fresh, 1):
        conn.execute(
            """INSERT INTO nav_items(group_id, name, url, description, subgroup, tags,
                 icon, color, sort, visible_to, probe, favorite, launch, launch_host,
                 launch_arg, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, r["name"], r["url"], "", r["subgroup"], tags,
             "", args.color, base_sort + i, "member", r["probe"], 0,
             "winbox", r["launch_host"], "", ts, ts))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM nav_items WHERE group_id=?",
                         (gid,)).fetchone()[0]
    conn.close()

    print(f"\n已写入分组「{args.group}」（id={gid}）：新增 {len(fresh)} 条，该组现有 {total} 条")
    print("可见性 member（需登录）；在线探测 " + ("开" if args.probe else "关（推荐）")
          + f"（端口 {WINBOX_PORT}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
