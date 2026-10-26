#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 nav_items.sort 归一化为「每个分组内 1..n 连续且唯一」。

为什么需要：xlsx 导入脚本当时把序号按**子分组**（客户）编号，导致
「客户Forti站点登录方式」里 269 项只有 19 个不同的 sort 值
（所有客户的第 1 台都是 sort=1、第 2 台都是 sort=2…）。
后果：
  - 按客户筛选时顺序是对的（每个客户内部 1..n）
  - 但「全部」视图变成「每个客户轮流取一台」的交错排列
  - 拖拽排序的落点计算也基于这个交错顺序，手感会很怪

归一化规则（保持既有意图）：
  - 有子分组的分组：先按「设备数多的客户优先」（与页面 chip 顺序一致），
    客户内部保持原有 sort 顺序
  - 无子分组的分组：保持原有 (sort, id) 顺序
  两种情况都重新编号成 1..n。

默认 dry-run，加 --apply 才写库，写库前自动备份。

    python3 tools/normalize_nav_sort.py
    python3 tools/normalize_nav_sort.py --apply
"""

from __future__ import annotations

import argparse
import collections
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("NOTICE_DB_PATH", os.path.join(BASE, "data", "notices.db"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    groups = [dict(r) for r in conn.execute("SELECT id, name FROM nav_groups ORDER BY sort, id")]

    plans, changed_groups = [], []
    for g in groups:
        items = [dict(r) for r in conn.execute(
            "SELECT id, name, subgroup, sort FROM nav_items WHERE group_id=? ORDER BY sort, id",
            (g["id"],))]
        if not items:
            continue
        counts = collections.Counter(
            (i["subgroup"] or "").strip() for i in items if (i["subgroup"] or "").strip())

        def key(i):
            sg = (i["subgroup"] or "").strip()
            if not sg:
                return (0, 0, "", i["sort"], i["id"])
            return (1, -counts[sg], sg, i["sort"], i["id"])   # 设备多的客户在前，与 chip 一致

        ordered = sorted(items, key=key)
        before = [i["sort"] for i in items]
        after = list(range(1, len(ordered) + 1))
        if before != after or [i["id"] for i in items] != [i["id"] for i in ordered]:
            changed_groups.append((g, items, ordered, before))
        for pos, it in enumerate(ordered, start=1):
            plans.append((pos, it["id"]))

    print("=" * 74)
    print(f"分组 {len(groups)} 个，导航项 {len(plans)} 项")
    print(f"需要调整的分组：{len(changed_groups)} 个")
    print("=" * 74)
    for g, items, ordered, before in changed_groups:
        dup = len(before) - len(set(before))
        print(f"\n  「{g['name']}」 {len(items)} 项"
              + (f"，重复 sort {dup} 个" if dup else ""))
        print(f"    原 sort 前 12: {before[:12]}")
        print(f"    新顺序前 12: "
              + ", ".join(f"{i['name']}[{(i['subgroup'] or '-')}]" for i in ordered[:12]))
        if g["name"].startswith("客户") or dup:
            subs = collections.Counter((i["subgroup"] or "").strip() for i in ordered
                                       if (i["subgroup"] or "").strip())
            print(f"    客户出现顺序: {[s for s, _ in subs.most_common(8)]} …")

    if not changed_groups:
        print("\n所有分组的 sort 已经是 1..n 连续，无需改动。")
        return 0

    print("\n--- 归一会带来的影响 ---")
    print("  · 按客户筛选时的顺序：不变（每个客户内部仍按原顺序）")
    print("  · 「全部」视图：从「每个客户轮流取一台」变成「按客户聚在一起」")
    print("  · 拖拽落点计算：变得可预期")

    if not args.apply:
        print("\n" + "=" * 74)
        print("预演结束，未写入任何数据。确认无误后加 --apply 落库。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suf):
            shutil.copy2(args.db + suf, f"{args.db}.pre-normsort-{stamp}{suf}")
    print(f"\n已备份数据库 → {args.db}.pre-normsort-{stamp}*")

    for pos, iid in plans:
        conn.execute("UPDATE nav_items SET sort=? WHERE id=?", (pos, iid))
    conn.commit()

    bad = []
    for g in groups:
        s = [r["sort"] for r in conn.execute(
            "SELECT sort FROM nav_items WHERE group_id=? ORDER BY sort, id", (g["id"],))]
        if s and s != list(range(1, len(s) + 1)):
            bad.append(g["name"])
    print(f"完成：重编号 {len(plans)} 项；仍不连续的分组: {bad or '无 ✓'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
