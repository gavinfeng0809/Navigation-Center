#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把浏览器导出的书签（Netscape bookmarks HTML）里某个文件夹的内容，
按目录路径重新分类后导入「太平洋电信导航中心」。

用法：
    python tools/import_bookmarks.py \
        --file "D:/.../bookmarks.html" \
        --folder "PBS办公" \
        --base http://10.0.0.10:8091 \
        --user admin --password admin \
        [--dry-run] [--tree] [--guest]

⚠️ 解析用标准库 html.parser，能正确处理任意层嵌套。
   **不要**改成非贪婪正则 —— 用 <DL>(.*?)</DL> 匹配嵌套结构时，遇到第一个内层
   </DL> 就会截断，会静默漏掉所有子文件夹里的内容（本项目就踩过这个坑：
   实际 265 条只导入了 24 条，看起来还"成功"了）。

- 分类按**目录路径**规则（classify 函数）匹配，比按标题正则可靠。
- 幂等：按 URL 去重，已存在的跳过，重复执行不会产生重复项。
- 默认导入为「需登录可见」；设备管理地址不适合免登录暴露，用 --guest 改成免登录。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# 分组定义：名称 -> (排序, 颜色, 描述, 标签)
# --------------------------------------------------------------------------
GROUP_DEFS = [
    ("KARL 站点防火墙",   20, "#D85A30", "KARL 各站点 FortiGate 管理入口", ["Fortinet", "防火墙", "KARL"]),
    ("客户站点防火墙",     21, "#E24B4A", "客户站点 FortiGate（搜索客户代号可过滤）", ["Fortinet", "防火墙", "客户"]),
    ("SASE POP",          22, "#185FA5", "SASE POP 节点 FortiGate", ["SASE", "POP", "Fortinet"]),
    ("集中管理平台",       23, "#534AB7", "FortiManager / FortiAnalyzer / EMS", ["Fortinet", "集中管理"]),
    ("Forti 文档与工具",   24, "#7F77DD", "官方手册、社区、知识库", ["Fortinet", "文档"]),
    ("系统与平台",         25, "#1D9E75", "公司内部系统与业务平台", ["系统", "平台"]),
    ("流量与监控",         26, "#EF9F27", "流量图、NetFlow、监控看板", ["监控", "流量"]),
    ("POP 服务器",         27, "#0F6E56", "各 POP 的 ESXi / 虚拟化平台", ["POP", "虚拟化"]),
    ("机房与授权",         28, "#888780", "机房门户与进出授权", ["机房", "授权"]),
    ("网络工具与查询",     29, "#378ADD", "IP / 端口 / BGP / 测速 等在线工具", ["工具", "查询"]),
    ("门户与云账号",       30, "#D4537E", "云厂商与厂商支持门户", ["云", "门户"]),
    ("业务与协作",         31, "#639922", "客户资料与办公协作", ["业务", "协作"]),
    ("其它工具",           32, "#5F5E5A", "杂项工具与文档", ["工具"]),
]
GROUP_COLOR = {g[0]: g[2] for g in GROUP_DEFS}
GROUP_TAGS = {g[0]: g[4] for g in GROUP_DEFS}
GROUP_ORDER = [g[0] for g in GROUP_DEFS]

# 集中管理相关的标题关键字（从任意 Forti 目录里挑出来）
MGMT_RE = re.compile(
    r"FortiManager|FortiAnalyzer|^FAZ|FortiClient Endpoint|EMS-Server|EMS handbook|FortiAuthenticator",
    re.I)


def classify(path: tuple, title: str, url: str) -> tuple:
    """返回 (组名, 额外标签列表)。path 是相对目标文件夹的目录路径元组。"""
    p = "/".join(path)
    tags: list = []

    if path[:1] == ("Forti",):
        rest = path[1:]
        if rest[:1] == ("SASEPOP",):
            if MGMT_RE.search(title):
                return "集中管理平台", []
            return "SASE POP", ["SASE POP"]
        if rest[:1] == ("KARL",):
            if MGMT_RE.search(title):
                return "集中管理平台", []
            return "KARL 站点防火墙", ["KARL"]
        if rest:
            # Forti/<客户代号>/... —— 用客户文件夹名当标签
            cust = rest[0]
            if MGMT_RE.search(title):
                return "集中管理平台", [cust]
            return "客户站点防火墙", [cust]
        # Forti 根目录下的散项
        if MGMT_RE.search(title) or re.search(r"\bEMS\b", title, re.I):
            return "集中管理平台", []
        return "Forti 文档与工具", []

    if p == "系统":
        return "系统与平台", []
    if p == "流量图":
        return "流量与监控", []
    if p == "POP服务器":
        return "POP 服务器", []
    if p == "机房授权":
        return "机房与授权", []
    if p == "IP测试":
        return "网络工具与查询", []
    if p == "门户支持":
        if path[1:2] == ("云业务",):
            return "门户与云账号", ["云业务"]
        return "门户与云账号", []
    if p == "Panabit":
        return "流量与监控", []

    # 目标文件夹根目录下的散项，按标题判断
    if re.search(r"NFSEN|ZABBIX|流量|NETWORK|延迟", title, re.I):
        return "流量与监控", []
    if re.search(r"呼叫中心|出勤|VVIP|Maintenance|服务器信息收集", title, re.I):
        return "业务与协作", []
    if re.search(r"SDWAN|VCO|数据平面|velo", title, re.I):
        return "门户与云账号", []
    return "其它工具", []


# --------------------------------------------------------------------------
# 书签解析：标准库 HTMLParser，正确支持任意层嵌套
# --------------------------------------------------------------------------

class BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"name": "ROOT", "url": "", "dir": True, "children": []}
        self.stack = [self.root]
        self.pushed: list = []
        self.folder = None
        self.link = None
        self.in_h3 = False
        self.in_a = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self.in_h3 = True
            self.folder = {"name": "", "url": "", "dir": True, "children": []}
        elif tag == "a":
            self.in_a = True
            self.link = {"name": "", "url": a.get("href", ""), "dir": False, "children": []}
        elif tag == "dl":
            if self.folder is not None:
                self.stack[-1]["children"].append(self.folder)
                self.stack.append(self.folder)
                self.pushed.append(True)
                self.folder = None
            else:
                # 没有文件夹的裸 <DL>（如根节点），透明处理
                self.stack.append(self.stack[-1])
                self.pushed.append(False)

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_h3 = False
        elif tag == "a":
            self.in_a = False
            if self.link is not None:
                self.stack[-1]["children"].append(self.link)
                self.link = None
        elif tag == "dl":
            if self.pushed and self.pushed.pop():
                self.stack.pop()

    def handle_data(self, data):
        if self.in_h3 and self.folder is not None:
            self.folder["name"] += data
        elif self.in_a and self.link is not None:
            self.link["name"] += data


def find_folder(nodes, target):
    for x in nodes:
        if x["dir"]:
            if x["name"].strip() == target:
                return x
            hit = find_folder(x["children"], target)
            if hit is not None:
                return hit
    return None


def walk_links(node, path, out):
    for x in node["children"]:
        if x["dir"]:
            walk_links(x, path + (x["name"].strip(),), out)
        else:
            url = x["url"]
            if url and not url.lower().startswith(("javascript:", "place:")):
                out.append((path, x["name"].strip(), url))
    return out


# --------------------------------------------------------------------------
# 名称与图标
# --------------------------------------------------------------------------

CITY2 = re.compile(r"(?<![A-Z])[A-Z]{2}(?![A-Z])")
CODE4 = re.compile(r"^([A-Z][A-Z0-9]{1,4}?)\d")
ICON_HINTS = [("FortiManager", "FM"), ("FortiAnalyzer", "FAZ"), ("^FAZ", "FAZ"),
              ("FortiClient Endpoint", "EMS"), ("EMS", "EMS"), ("FortiAuthenticator", "FAC")]


def clean_name(title: str) -> str:
    n = re.sub(r"^FortiGate\s*[-–—]?\s*", "", title, flags=re.I)
    n = re.sub(r"_(Primary|Backup|Main|Back)$", "", n, flags=re.I)
    n = n.replace("_", " ")
    n = re.sub(r"\s*[-–—]+\s*$", "", n).strip()
    return re.sub(r"\s{2,}", " ", n)


def make_icon(title: str, group: str) -> str:
    if group == "KARL 站点防火墙":
        hit = CITY2.findall(title)
        if hit:
            return hit[-1]
        m = re.search(r"KARL[-\s]?(\d{3})", title)
        return m.group(1) if m else "K"
    if group in ("客户站点防火墙", "SASE POP"):
        base = re.sub(r"^FortiGate\s*[-–—]?\s*", "", title, flags=re.I)
        m = CODE4.match(base)
        if m:
            return m.group(1)[:4]
        m2 = re.search(r"^([A-Z]{2,5})", base)
        return m2.group(1)[:4] if m2 else ""
    for pat, ic in ICON_HINTS:
        if re.search(pat, title, re.I):
            return ic
    return ""


def host_of(url: str) -> str:
    p = urllib.parse.urlparse(url)
    h = p.hostname or ""
    port = p.port
    if port and not ((p.scheme == "https" and port == 443) or (p.scheme == "http" and port == 80)):
        h += ":" + str(port)
    return h


# --------------------------------------------------------------------------
# 导航接口
# --------------------------------------------------------------------------

class Nav:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.token = ""

    def call(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        m = method or ("POST" if data else "GET")
        hdrs = {}
        if data:
            hdrs["Content-Type"] = "application/json"
        if self.token:
            hdrs["X-Session-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=data, method=m, headers=hdrs)
        try:
            raw = self.op.open(req, timeout=30).read().decode("utf-8-sig")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"接口 {path} 返回 {e.code}: {e.read().decode('utf-8-sig')[:200]}")
        return json.loads(raw or "{}")

    def login(self, user, password):
        d = self.call("/api/login", {"username": user, "password": password})
        self.token = d.get("token", "")
        if not self.token:
            raise SystemExit("登录失败：未取得令牌")
        return d.get("user", {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--base", default="http://10.0.0.10:8091")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--guest", action="store_true", help="导入为免登录可见（默认需登录可见）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tree", action="store_true", help="先打印来源目录分布")
    ap.add_argument("--update-existing", action="store_true",
                    help="已存在的项（按 URL 匹配）也回填分组/二级分组/名称等字段")
    args = ap.parse_args()

    raw = open(args.file, "rb").read()
    text, enc = None, ""
    for e in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text, enc = raw.decode(e), e
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise SystemExit("无法识别书签文件编码")
    print(f"书签文件 {len(text)} 字符，编码 {enc}")

    bm = BookmarkParser()
    bm.feed(text)
    folder = find_folder(bm.root["children"], args.folder)
    if folder is None:
        tops = [x["name"].strip() for x in bm.root["children"] if x["dir"]]
        raise SystemExit(f"没找到文件夹「{args.folder}」。顶层：{tops}")

    links = walk_links(folder, (), [])
    print(f"文件夹「{args.folder}」共 {len(links)} 条链接")

    if args.tree:
        counts = {}
        for path, _, _ in links:
            key = "/".join(path) or "(根)"
            counts[key] = counts.get(key, 0) + 1
        print("来源目录分布：")
        for k in sorted(counts):
            print(f"  {counts[k]:>4}  {k}")
        print()
        if args.dry_run:
            return 0

    plan = []
    for path, title, url in links:
        group, extra = classify(path, title, url)
        # 「客户站点防火墙」用客户代号做组内二级分组
        subgroup = extra[0] if (group == "客户站点防火墙" and extra) else ""
        plan.append({
            "group": group,
            "subgroup": subgroup,
            "name": clean_name(title) or title,
            "url": url,
            "description": host_of(url),
            "icon": make_icon(title, group),
            "tags": extra,
            "path": "/".join(path) or "(根)",
            "orig": title,
        })

    # 同名去重：追加主机名
    seen_name = {}
    for it in plan:
        seen_name.setdefault((it["group"], it["name"]), []).append(it)
    for _, items in seen_name.items():
        if len(items) > 1:
            for it in items:
                it["name"] = f"{it['name']} ({host_of(it['url'])})"

    by_group = {}
    for it in plan:
        by_group.setdefault(it["group"], []).append(it)

    print("\n分类结果：")
    total = 0
    for gname in GROUP_ORDER:
        items = by_group.get(gname, [])
        if not items:
            continue
        total += len(items)
        srcs = sorted(set(i["path"] for i in items))
        show = ", ".join(srcs[:3]) + ("…" if len(srcs) > 3 else "")
        print(f"  {gname:<16} {len(items):>3} 项   来源：{show}")
    print(f"  {'合计':<16} {total:>3} 项\n")

    if args.dry_run:
        for gname in GROUP_ORDER:
            items = by_group.get(gname, [])
            if not items:
                continue
            print(f"[{gname}]")
            for it in items[:40]:
                print(f"    {it['icon'] or '·':<5}{it['name']:<32}{it['description']:<22}{it['url'][:44]}")
            if len(items) > 40:
                print(f"    … 其余 {len(items) - 40} 项省略")
        print("\n（--dry-run，未写入）")
        return 0

    nav = Nav(args.base)
    me = nav.login(args.user, args.password)
    print(f"已登录 {me.get('username')}（{me.get('role_label')}）")

    cur = nav.call("/api/nav")
    existing_groups = {g["name"]: g["id"] for g in cur["groups"]}
    existing_by_url = {i["url"]: i["id"] for g in cur["groups"] for i in g["items"]}

    gid = {}
    for gname, sort, color, desc, tags in GROUP_DEFS:
        if not by_group.get(gname):
            continue
        if gname in existing_groups:
            gid[gname] = existing_groups[gname]
            print(f"  分组已存在，复用：{gname}")
            continue
        gid[gname] = nav.call("/api/nav/groups", {"name": gname, "sort": sort})["id"]
        print(f"  新建分组：{gname}")

    vis = "guest" if args.guest else "member"
    ok = updated = skip = fail = 0
    for it in plan:
        existed_id = existing_by_url.get(it["url"])
        if existed_id is not None and not args.update_existing:
            skip += 1
            continue
        try:
            body = {
                "group_id": gid[it["group"]],
                "name": it["name"],
                "url": it["url"],
                "description": it["description"],
                "subgroup": it["subgroup"],
                "icon": it["icon"],
                "color": GROUP_COLOR[it["group"]],
                "tags": list(GROUP_TAGS[it["group"]]) + it["tags"],
                "probe": 0,
            }
            if existed_id is not None:
                nav.call(f"/api/nav/items/{existed_id}", body, "PUT")
                updated += 1
            else:
                body["visible_to"] = vis
                body["favorite"] = 0
                nav.call("/api/nav/items", body)
                ok += 1
        except SystemExit as e:
            fail += 1
            print(f"  写入失败 {it['name']}: {e}")

    print(f"\n导入完成：新增 {ok}，更新 {updated}，已存在跳过 {skip}，失败 {fail}")
    final = nav.call("/api/nav")
    print(f"导航现状：{final['total']} 个系统 / {len(final['groups'])} 个分组")
    return 0


if __name__ == "__main__":
    sys.exit(main())
