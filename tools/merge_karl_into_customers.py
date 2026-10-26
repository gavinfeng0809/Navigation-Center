#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「KARL 站点防火墙」分组整体并入「客户站点防火墙」分组。

做法：
  1. 分组 10 的全部导航项 → group_id 改成客户站点分组，subgroup 设为 'KARL'
  2. KARL 子分组内按名称自然排序（KARL-001 < KARL-003 < … < KARL-960 < KARL-962）
  3. 搬空后删除「KARL 站点防火墙」这个空分组

安全点：
  - 默认 dry-run，只打印报告；--apply 才写库
  - 写库前自动备份数据库
  - 合并前检查两个分组之间是否有重复 host（同一台设备出现在两边）
  - 删除分组前确认里面已经一项不剩

    python3 tools/merge_karl_into_customers.py            # 预演
    python3 tools/merge_karl_into_customers.py --apply    # 落库
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("NOTICE_DB_PATH", os.path.join(BASE, "data", "notices.db"))

SRC_GROUP = "KARL 站点防火墙"
DST_GROUP = "客户站点防火墙"
SUBGROUP = "KARL"


def hostport(u: str) -> str:
    m = re.match(r"^[a-z]+://([^/]+)", (u or "").strip(), re.I)
    return m.group(1).lower() if m else ""


def natkey(s: str):
    """自然排序：KARL-2 排在 KARL-10 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row

    def grp(name):
        return conn.execute("SELECT id, name, sort FROM nav_groups WHERE name=?", (name,)).fetchone()

    src, dst = grp(SRC_GROUP), grp(DST_GROUP)
    if not src:
        print(f"找不到分组「{SRC_GROUP}」（可能已经合并过了）")
        return 0
    if not dst:
        print(f"找不到分组「{DST_GROUP}」", file=sys.stderr)
        return 1

    moving = [dict(r) for r in conn.execute(
        "SELECT * FROM nav_items WHERE group_id=? ORDER BY sort, id", (src["id"],))]
    staying = [dict(r) for r in conn.execute(
        "SELECT * FROM nav_items WHERE group_id=? AND subgroup=? ORDER BY sort, id",
        (dst["id"], SUBGROUP))]
    dst_total = conn.execute("SELECT COUNT(*) AS c FROM nav_items WHERE group_id=?",
                             (dst["id"],)).fetchone()["c"]

    print("=" * 74)
    print(f"源分组「{SRC_GROUP}」  id={src['id']}  共 {len(moving)} 项")
    print(f"目标分组「{DST_GROUP}」id={dst['id']}  共 {dst_total} 项"
          f"（其中 KARL 子分组已有 {len(staying)} 项）")
    print("=" * 74)

    # 重复 host 检查：同一台设备不能同时出现在两个分组里
    dst_hosts = {}
    for r in conn.execute("SELECT id, name, url, group_id FROM nav_items WHERE group_id<>?",
                          (src["id"],)):
        hp = hostport(r["url"])
        if hp:
            dst_hosts.setdefault(hp, dict(r))
    clashes = []
    for m in moving:
        hp = hostport(m["url"])
        if hp and hp in dst_hosts:
            clashes.append((m, dst_hosts[hp]))

    print(f"\n将搬入 {len(moving)} 项；搬迁后 KARL 子分组共 {len(moving) + len(staying)} 项")
    print(f"\n--- 两个分组之间重复 host 的设备 ---")
    if clashes:
        print(f"  ⚠ {len(clashes)} 台设备在目标分组里已经有一份：")
        for m, o in clashes:
            print(f"     {m['name']:<22} {hostport(m['url'])}")
            print(f"       源(分组10): {m['url']}")
            print(f"       已有      : [{o['group_id']}] {o['name']}  {o['url']}")
    else:
        print("  无 —— 17 台 KARL 设备与客户站点分组里现有设备没有重叠，直接搬即可")

    print(f"\n--- 搬入后的排序（按名称自然序）---")
    merged = sorted(moving + staying, key=lambda x: natkey(x["name"]))
    for i, it in enumerate(merged, 1):
        mark = "  ← 原有" if it in staying else ""
        print(f"  {i:>2}. {it['name']:<24} {it['url'][:58]}{mark}")

    if not args.apply:
        print("\n" + "=" * 74)
        print("预演结束，未写入任何数据。确认无误后加 --apply 落库。")
        return 0

    # ---------------- 落库 ----------------
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suf):
            shutil.copy2(args.db + suf, f"{args.db}.pre-karlmerge-{stamp}{suf}")
    print(f"\n已备份数据库 → {args.db}.pre-karlmerge-{stamp}*")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # 1) 搬运
    conn.execute("UPDATE nav_items SET group_id=?, subgroup=?, updated_at=? WHERE group_id=?",
                 (dst["id"], SUBGROUP, ts, src["id"]))
    # 2) 重排序号
    for i, it in enumerate(merged, 1):
        conn.execute("UPDATE nav_items SET sort=? WHERE id=?", (i, it["id"]))
    conn.commit()

    # 3) 确认源分组已空，再删
    left = conn.execute("SELECT COUNT(*) AS c FROM nav_items WHERE group_id=?",
                        (src["id"],)).fetchone()["c"]
    if left:
        print(f"  ✗ 源分组仍有 {left} 项，已跳过删除（请检查）", file=sys.stderr)
    else:
        conn.execute("DELETE FROM nav_groups WHERE id=?", (src["id"],))
        conn.commit()
        print(f"  已删除空分组「{SRC_GROUP}」")

    now_total = conn.execute("SELECT COUNT(*) AS c FROM nav_items WHERE group_id=?",
                             (dst["id"],)).fetchone()["c"]
    karl_now = conn.execute("SELECT COUNT(*) AS c FROM nav_items WHERE group_id=? AND subgroup=?",
                            (dst["id"], SUBGROUP)).fetchone()["c"]
    print(f"完成：客户站点分组现有 {now_total} 项，KARL 子分组 {karl_now} 项")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
