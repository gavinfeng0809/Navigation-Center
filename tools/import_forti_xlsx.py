#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按《Forti 版本升级 7.4.12-202609.xlsx》的「飞塔登录信息参考」列重排
notice-hub 的「客户站点防火墙」分组。

规则（已与使用方确认）：
  1. 子分组 = Service Id 前缀（如 ATKN-001 → ATKN），共 84 个客户
  2. **保留导航里已有的深链**，xlsx 只用来补全「主机:端口」。
     例：导航 https://198.19.222.91/ng/routing/bgp  +  xlsx https://198.19.222.91
         → 保持不变（深链不丢）；只有当 xlsx 的 host:port 与导航不同时才改写 host:port、保留路径
  3. 导航里独有、xlsx 没有的设备（如整个 WWZG）**全部保留**，不被清理
  4. xlsx 有、导航没有的设备 → 新增

默认 dry-run，只打印报告；加 --apply 才写库。写库前自动备份数据库。

    python3 tools/import_forti_xlsx.py                 # 预演
    python3 tools/import_forti_xlsx.py --apply         # 落库
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from xml.etree import ElementTree as ET
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(BASE, "docs", "Forti 版本升级 7.4.12-202609.xlsx")
DB = os.environ.get("NOTICE_DB_PATH", os.path.join(BASE, "data", "notices.db"))
GROUP_NAME = "客户站点防火墙"   # 目标分组；被改名时按前缀「客户」兜底（见 find_group）

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# xlsx 里「飞塔登录信息参考」所在的列号（0 基）
COL_LOGIN = 5
COL_SERVICE_ID = 0
COL_MODEL = 2


# ---------------------------------------------------------------- xlsx 解析
def col_idx(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path: str) -> list:
    z = zipfile.ZipFile(path)
    shared = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{NS}si"):
        shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.iter(f"{NS}row"):
        cells = {}
        for c in row.findall(f"{NS}c"):
            ci = col_idx(c.get("r"))
            t = c.get("t")
            v = c.find(f"{NS}v")
            isel = c.find(f"{NS}is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr" and isel is not None:
                val = "".join(x.text or "" for x in isel.iter(f"{NS}t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[ci] = val
        if cells:
            rows.append([(cells.get(i) or "").strip() for i in range(max(cells) + 1)])
    return rows


def cell(r: list, i: int) -> str:
    return r[i] if i < len(r) else ""


def prefix_of(sid: str) -> str:
    m = re.match(r"^([A-Za-z\u4e00-\u9fff]+)", sid or "")
    return m.group(1).upper() if m else ""


def norm_url(u: str) -> str:
    """补协议头、去掉多余的尾部斜杠。"""
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^[a-z]+://", u, re.I):
        u = "https://" + u
    m = re.match(r"^([a-z]+://[^/\s]+)(/\S*)?$", u, re.I)
    if m:
        path = m.group(2) or ""
        if path in ("", "/"):
            return m.group(1)
        return m.group(1) + path
    return u


IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$")
URL_RE = re.compile(r"(https?://[^\s，,；;、]+)", re.I)
# 裸主机（IP 或域名）+ 可选端口 + 可选路径，如 198.19.227.180:8443/login?redir=%2F
BARE_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)"
    r"(?::\d+)?(?:[/?#]\S*)?$", re.I)


def extract_login(cell: str) -> tuple:
    """从登录信息单元格里取出 (主地址, 附加说明列表)。

    源表里有四类脏数据，直接当 URL 用会点不开：
      1. 主备双地址：'https://a:8443/ng/interface ⏎ 或 ⏎ https://b:8443/ng/interface'
      2. 带备注：   'https://a:8443/ ⏎ 分流设备：https://b'
      3. 坏空格：   'https://a :8443或https://b:8443'
      4. 裸主机带路径：'198.19.227.180:8443/login?redir=%2F'
    规则：第一个地址当主地址，其余一律进说明（不丢信息）。
    """
    raw = (cell or "").strip()
    if not raw:
        return "", []
    # 去掉「:端口」前的空格（源表里有 '148 :8443' 这种写法）
    s = re.sub(r"[ \t]+:", ":", raw)
    # 换行与「或」都当成分隔符
    s = re.sub(r"\s*或\s*", "\n", s)
    parts = [p.strip(" \t:：，,;；、") for p in re.split(r"[\n\r]+", s)]
    primary, extras = "", []
    for p in parts:
        if not p:
            continue
        m = URL_RE.search(p)
        if m:
            u = m.group(1)
            rest = (p[:m.start()] + p[m.end():]).strip(" \t:：，,;；、")
        elif BARE_RE.match(p):
            u = "https://" + p
            rest = ""
        else:
            extras.append(p)
            continue
        if not primary:
            primary = u
        else:
            extras.append(u)
        if rest:
            extras.append(rest)
    return norm_url(primary), extras


def split_host(u: str) -> str:
    """取 scheme://host:port 部分（小写）。"""
    u = norm_url(u)
    m = re.match(r"^([a-z]+://[^/]+)", u, re.I)
    return m.group(1).lower() if m else ""


def hostport(u: str) -> str:
    h = split_host(u)
    return re.sub(r"^[a-z]+://", "", h)


def swap_host(nav_url: str, xlsx_url: str) -> str:
    """把 nav_url 的 scheme://host:port 换成 xlsx 的，路径与查询串保留。"""
    xn = norm_url(xlsx_url)
    xh = split_host(xn)
    if not xh:
        return nav_url
    nn = norm_url(nav_url) or xn
    m = re.match(r"^[a-z]+://[^/]+(/.*)?$", nn, re.I)
    path = (m.group(1) or "") if m else ""
    return xh + path


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--xlsx", default=XLSX)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--group", default="", help="目标分组名（默认自动匹配）")
    args = ap.parse_args()

    if not os.path.exists(args.xlsx):
        print(f"找不到 xlsx：{args.xlsx}", file=sys.stderr)
        return 1
    if not os.path.exists(args.db):
        print(f"找不到数据库：{args.db}", file=sys.stderr)
        return 1

    rows = read_xlsx(args.xlsx)
    data = [r for r in rows[2:] if cell(r, COL_SERVICE_ID)]      # 行2 是表头

    # xlsx 设备（按 Service Id 去重；重复时保留第一条并记下来）
    devices, dupes = {}, []
    for r in data:
        sid = cell(r, COL_SERVICE_ID)
        if sid in devices:
            dupes.append(sid)
            continue
        url, extras = extract_login(cell(r, COL_LOGIN))
        devices[sid] = {
            "sid": sid,
            "url": url,
            "extras": extras,
            "model": cell(r, COL_MODEL),
            "prefix": prefix_of(sid),
        }
    by_host = {}
    for d in devices.values():
        by_host.setdefault(hostport(d["url"]), d["sid"])

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    def find_group(want):
        """分组可能被改过名，所以：精确 → 前缀「客户」→ 唯一含「客户」的分组。"""
        if want:
            r = conn.execute("SELECT id, name FROM nav_groups WHERE name=?", (want,)).fetchone()
            if r:
                return r
        r = conn.execute("SELECT id, name FROM nav_groups WHERE name=?", (GROUP_NAME,)).fetchone()
        if r:
            return r
        cands = [dict(x) for x in conn.execute(
            "SELECT id, name FROM nav_groups WHERE name LIKE '客户%' ORDER BY sort, id")]
        if len(cands) == 1:
            return cands[0]
        have = ", ".join(x["name"] for x in conn.execute(
            "SELECT name FROM nav_groups ORDER BY sort, id"))
        print(f"找不到目标分组（找过「{want or GROUP_NAME}」和前缀「客户」，命中 {len(cands)} 个）\n"
              f"现有分组：{have}\n用 --group 指定分组名。", file=sys.stderr)
        return None

    grp = find_group(args.group)
    if not grp:
        return 1
    if grp["name"] != GROUP_NAME:
        print(f"目标分组自动匹配为「{grp['name']}」")
    gid = grp["id"]

    cur_items = [dict(r) for r in conn.execute(
        "SELECT * FROM nav_items WHERE group_id=? ORDER BY sort, id", (gid,))]
    # 全库其它分组的 host，用来避免跨分组重复插入同一台设备
    other_hosts = {}
    for r in conn.execute("SELECT id, name, url, group_id FROM nav_items WHERE group_id<>?", (gid,)):
        hp = hostport(r["url"])
        if hp:
            other_hosts.setdefault(hp, dict(r))

    print("=" * 74)
    print(f"xlsx 数据行 {len(data)} → 设备 {len(devices)} 台"
          + (f"（重复 Service Id 已跳过：{', '.join(sorted(set(dupes)))}）" if dupes else ""))
    print(f"数据库「{grp['name']}」现有 {len(cur_items)} 项")
    print(f"其它分组已有 host {len(other_hosts)} 个（用于防跨分组重复）")
    print("=" * 74)

    consumed = set()
    plans = []

    for it in cur_items:
        name = it["name"] or ""
        pre = prefix_of(name)
        old_sub = (it["subgroup"] or "").strip()
        # 子分组按 Service Id 前缀推导，但**已经是代码形式的分组要保留**：
        # 那是人工归置的结果（例如 SASE-SH 被手动并进 KARL 子分组，
        # 按名称前缀会被算成 SASE，重跑就会把人工归置冲掉）。
        # 旧方案用的是中文名（铸成/华港…）或空，那些才需要重排成客户代码。
        if old_sub and re.match(r"^[A-Za-z0-9_\-]+$", old_sub):
            new_sub = old_sub
        else:
            new_sub = pre or old_sub or "未分组"
        # 先按名称精确匹配，再按 host:port 匹配
        sid = name if name in devices else by_host.get(hostport(it["url"]))
        d = devices.get(sid) if sid else None
        if d:
            consumed.add(d["sid"])
            new = it["url"] or ""
            if hostport(d["url"]) and hostport(d["url"]) != hostport(it["url"]):
                new = swap_host(it["url"], d["url"])       # 只换 host:port，保留深链
                note = f"改地址 {it['url']} → {new}"
            else:
                # 导航地址比裸 host:port 长 → 带路径，是深链，必须原样留着
                note = "保留（深链未动）" if len(norm_url(it["url"])) > len(split_host(it["url"])) else "保留"
            plans.append({"act": "update", "id": it["id"], "name": name, "url": new,
                          "subgroup": new_sub, "old_subgroup": old_sub, "note": note})
        else:
            plans.append({"act": "keep", "id": it["id"], "name": name, "url": it["url"],
                          "subgroup": new_sub, "old_subgroup": old_sub,
                          "note": "xlsx 无此设备，按原样保留"})

    # 先把现有项排好（顺序待最后统一编号）
    for idx, p in enumerate(plans):
        p["_ord"] = idx

    for sid, d in devices.items():
        if sid in consumed:
            continue
        hp = hostport(d["url"])
        if hp in other_hosts:
            plans.append({"act": "skip-dup", "id": other_hosts[hp]["id"], "name": sid,
                          "url": d["url"], "subgroup": d["prefix"], "old_subgroup": "",
                          "note": f"其它分组已有同 host（{other_hosts[hp]['name']}），不重复插入"})
            continue
        plans.append({"act": "insert", "id": None, "name": sid, "url": d["url"],
                      "subgroup": d["prefix"], "old_subgroup": "", "model": d["model"],
                      "extras": d["extras"], "_ord": len(plans), "note": "新增"})

    # ---------------- 统一编号 ----------------
    # sort 必须是「分组内 1..n 连续且唯一」。曾经按子分组各自从 1 开始编号，
    # 导致一个分组里每个客户的第 1 台都是 sort=1（269 项只有 19 个不同值），
    # 「全部」视图会变成「每个客户轮流取一台」的交错排列，拖拽落点也会失准。
    for n, p in enumerate(sorted(plans, key=lambda x: (x["subgroup"], x.get("_ord", 0))), start=1):
        p["sort"] = n

    # ---------------- 报告（全部由 plans 现算，避免计数器之间对不上） ----------------
    active = [p for p in plans if p["act"] != "skip-dup"]
    added = sum(1 for p in plans if p["act"] == "insert")
    rehost = sum(1 for p in plans if p["act"] == "update" and p["note"].startswith("改地址"))
    kept = sum(1 for p in plans if p["act"] == "update")
    navonly = sum(1 for p in plans if p["act"] == "keep")
    xdup = sum(1 for p in plans if p["act"] == "skip-dup")
    resub = sum(1 for p in active if p["act"] != "insert" and p["subgroup"] != p["old_subgroup"])
    subs = sorted({p["subgroup"] for p in active})

    print(f"\n新增设备        : {added}")
    print(f"改地址(host变了) : {rehost}")
    print(f"命中 xlsx 保留  : {kept}  （其中深链未动 "
          f"{sum(1 for p in plans if '深链未动' in p['note'])}）")
    print(f"导航独有保留    : {navonly}")
    print(f"跨分组重复跳过  : {xdup}")
    print(f"子分组重排      : {resub}  （中文名/旧分组 → 客户代码）")
    print(f"重排后客户分组  : {len(subs)} 个")
    print(f"重排后总项数    : {len(active)}")

    print("\n--- 会改地址的项（host:port 变了，路径保留） ---")
    hits = [p for p in plans if p["act"] == "update" and p["note"].startswith("改地址")]
    for p in hits[:10]:
        print(f"  {p['name']:<22} {p['note']}")
    if not hits:
        print("  无 —— xlsx 里的 host:port 与导航现有值完全一致，无需改写")

    print("\n--- 深链示例：确认没有被裸地址覆盖 ---")
    shown = 0
    for p in plans:
        if p["act"] == "update" and "深链未动" in p.get("note", ""):
            print(f"  {p['name']:<22} {p['url']}")
            shown += 1
            if shown >= 6:
                break
    if shown == 0:
        print("  （无）")

    print("\n--- 客户分组（前 20，按设备数） ---")
    cnt = {}
    for p in plans:
        if p["act"] != "skip-dup":
            cnt[p["subgroup"]] = cnt.get(p["subgroup"], 0) + 1
    for k, v in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:20]:
        print(f"  {k:<12} {v} 台")

    print("\n--- 跨分组重复（跳过插入） ---")
    for p in plans:
        if p["act"] == "skip-dup":
            print(f"  {p['name']:<18} {p['note']}")

    # 落库前的自检：地址里绝不能残留空白/换行/「或」等备注文字
    bad = [p for p in active if re.search(r"\s", p["url"] or "")]
    print("\n--- 地址自检 ---")
    if bad:
        print(f"  ✗ 仍有 {len(bad)} 条地址含空白字符，已中止：")
        for p in bad[:10]:
            print(f"     {p['name']:<20} {p['url']!r}")
        return 2
    print(f"  ✓ {len(active)} 条地址格式干净（无空白/换行/备注混入）")
    multi = [p for p in plans if p.get("extras")]
    print(f"  ✓ {len(multi)} 条原始单元格含主备双地址或备注，已拆到描述里")

    # 同一地址被多个 Service Id 共用（源表如此，保留两者并提示）
    hp = {}
    for p in active:
        hp.setdefault(hostport(p["url"]), []).append(p["name"])
    shared = {k: v for k, v in hp.items() if len(v) > 1}
    if shared:
        print(f"\n  注意：{len(shared)} 个地址被多个 Service Id 共用（源表如此，两条都保留）：")
        for k, v in shared.items():
            print(f"     {k:<32} <- {v}")

    if not args.apply:
        print("\n" + "=" * 74)
        print("预演结束，未写入任何数据。确认无误后加 --apply 落库。")
        return 0

    # ---------------- 落库 ----------------
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suf):
            shutil.copy2(args.db + suf, f"{args.db}.pre-xlsx-{stamp}{suf}")
    print(f"\n已备份数据库 → {args.db}.pre-xlsx-{stamp}*")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ins = upd = 0
    for p in plans:
        if p["act"] == "insert":
            tags = json.dumps(["Fortinet", "防火墙", "客户", p["subgroup"]], ensure_ascii=False)
            desc = p.get("model") or ""
            ex = p.get("extras") or []
            if ex:
                desc = (desc + " ｜ " if desc else "") + "备用/备注：" + " / ".join(ex)
            desc = desc[:300]
            conn.execute(
                """INSERT INTO nav_items(group_id, name, url, description, subgroup, tags,
                     icon, color, sort, visible_to, probe, favorite, launch, launch_host,
                     launch_arg, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, p["name"], p["url"], desc, p["subgroup"], tags,
                 # probe=1：客户站点大多落在 198.19.x.x（属 198.18.0.0/15）。
                 # 修好 is_private_host 之后这段能正常探测了，实测 66% 可达。
                 # 旧判定把它们当「非内网」整批跳过，所以当时只能写 0。
                 "", "#3b82f6", p["sort"], "member", 1, 0, "web", "", "", ts, ts))
            ins += 1
        elif p["act"] in ("update", "keep"):
            conn.execute("UPDATE nav_items SET url=?, subgroup=?, sort=?, updated_at=? WHERE id=?",
                         (p["url"], p["subgroup"], p["sort"], ts, p["id"]))
            upd += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM nav_items WHERE group_id=?", (gid,)).fetchone()["c"]
    print(f"完成：新增 {ins} 项，更新 {upd} 项，分组现有 {total} 项")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
