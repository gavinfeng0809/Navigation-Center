#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性迁移脚本：把单文件 SPA（public/index.html）拆成
  public/common.css   共用样式（原 <style> 原样搬过去）
  public/common.js    共用库（window.NH 命名空间）
  public/index.html   导航中心（仅门户）
  public/notices.html 重要通知中心（仅通知）

做法：按「顶层块」切分原 IIFE 内部代码，再用三份函数名清单归桶。
顶层块靠大括号深度归零来切，函数体不会被拆开。

    python3 tools/split_frontend.py            # 预演：只打印分桶结果
    python3 tools/split_frontend.py --write    # 真正写文件（会先备份原 index.html）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "public", "index.html")

# ---------------------------------------------------------------- 分桶清单
SHARED = [
    "svg", "ic", "esc", "pad", "parseDT", "timeAgo", "fmtDT", "todayStr", "hl",
    "cleanSummary", "isImageUrl", "linkify", "renderAttachList",
    "toast", "copyText", "fallbackCopy", "api", "getToken", "setToken",
    "isAdmin", "renderMe", "fillPwdBanner", "pwdBannerClick", "openChangePwd",
    "applyTheme", "initTheme", "showLogin", "readLoginHash", "submitLoginByFetch",
    "loginRememberUser", "armLoginFallback", "mountModal", "closeModal", "go",
    "queryParams",
]
PORTAL = [
    "loadNav", "renderPortal", "renderNavSide", "renderPane", "navSideItem",
    "applyNavFilter", "navPaintChips", "navRecordVisit", "navToggleFav", "navLinkClick",
    "navInitial", "navCardHtml", "groupBodyHtml", "flatRowHtml", "renderFlatList",
    "navSearchText", "navRank", "hostOf", "navOpenTarget", "navIsApp", "internalRoute",
    "lsGet", "lsSet", "navSelGroup", "setNavSelGroup", "navSubSel", "setNavSubSel",
    "cmdRender", "cmdOpen", "cmdClose", "cmdMove", "cmdGo",
    "navManageShell", "openNavManage", "navManageChange", "navDragRow", "navClearDropMarks",
    "navResetDrag", "navDragStart", "navDragOver", "navDragLeave", "navDrop", "navDragEnd",
    "navManageList", "renderNavManageList", "findNavItem", "navItemForm", "showNavItemForm",
    "navManageClick", "afterNavChange", "navRefreshAnd",
    "openUsers", "rndInt", "genPassword", "navUserMenuItem", "openNavUserMenu",
    "closeNavUserMenu", "renderNavUser",
]
NOTICES = [
    "loadNotices", "loadCategories", "loadStats", "toggleIn", "fItem", "renderSidebar",
    "cardHtml", "tableHtml", "emptyHtml", "renderList", "renderCount", "renderPager",
    "openDetail", "catOptions", "openEditor", "openImport", "polishKeywords", "isTitleJunk",
    "pickTitleLine", "polishRecord", "polishPaste", "doExport", "openStats", "openMenu",
    "openCategories", "setView", "toggleDrawer",
]
DROPPED = ["currentRoute", "applyRoute", "renderAll", "boot"]   # SPA 路由与旧 boot 由新结构取代

PORTAL_IDS = set(
    "navQ navQClear navSide navGroups navFlat navRecent navRefresh navAccount navManage "
    "navUserChip navUserMenu navPwdBanner navTheme brandMark icRefresh icAccount "
    "icNavManage cmdk cmdkQ cmdkList cmdkCnt siSearch".split())
NOTICE_IDS = set(
    "q btnClearQ btnDrawer btnBackPortal btnImport btnStats btnMenu btnTheme sidebar "
    "listArea pager count sizeSel sortSel viewCard viewTable noticeBrandMark icImport "
    "icDown pwdBanner btnExport".split())
LOGIN_IDS = set(
    "loginScreen loginForm loginBtn loginUser loginPwd loginRemember loginErr loginLogo".split())

# 顶层 var / 长量按名字归桶
# 注意要带上 "var " 前缀：块的实际文本是 "var LS_NAVOPEN = ..."，
# 只写 "LS_NAVOPEN" 永远匹配不上（曾经因此把门户的 LS_NAV* 误分到共用桶）。
PORTAL_VARS = ("var LS_NAVOPEN", "var US =", "var NAVDRAG =", "var cmdkItems", "var navTimer")
NOTICE_VARS = ("var IMPORT_EXAMPLE", "var POLISH_", "var searchTimer", "var drawerOpen")


def split_blocks(inner: str):
    """按大括号深度 + 语句结束符切顶层块。

    关键：不能一见到 depth==0 就切 —— 多行语句（如 var X = 'a' + 'b';）
    每行深度都是 0，会被逐行打散。必须等这一条语句真的结束：
    深度归零 且 以 ; 或 } 收尾，或者遇到空行。
    """
    # 注意：不要做「去掉 2 个前导空格」的 dedent —— IIFE 体内本来就是顶格写的，
    # 去缩进会把函数内部的嵌套代码顶到行首，既毁掉缩进，又让 ^var 之类的
    # 行首匹配把局部变量误认成顶层声明。
    lines = inner.split("\n")
    blocks, buf, depth = [], [], 0
    for ln in lines:
        if not buf and not ln.strip():
            continue
        buf.append(ln)
        depth += ln.count("{") - ln.count("}")
        if depth < 0:
            depth = 0
        text = "\n".join(buf).rstrip()
        if depth == 0 and (text.endswith(";") or text.endswith("}") or
                           (not ln.strip() and buf)):
            blocks.append(text)
            buf, depth = [], 0
    if buf and "\n".join(buf).strip():
        blocks.append("\n".join(buf).rstrip())
    return blocks


# 块首注释：很多函数/变量前面挂着说明注释，必须先剥掉，
# 否则按名字分类会失配，退化成语义不准的「按元素 ID 猜」。
LEAD_COMMENT = re.compile(r"^(?:\s*/\*[\s\S]*?\*/|\s*//[^\n]*)\s*")


def block_kind(b: str):
    body = b
    while True:
        stripped = LEAD_COMMENT.sub("", body, count=1)
        if stripped == body:
            break
        body = stripped
    m = re.match(r"^function ([A-Za-z0-9_]+)\s*\(", body)
    if m:
        name = m.group(1)
        for bucket, names in (("portal", PORTAL), ("notices", NOTICES), ("shared", SHARED)):
            if name in names:
                return bucket, name
        if name in DROPPED:
            return "drop", name
        return "shared", name
    for p in PORTAL_VARS:
        if body.startswith(p) or b.startswith(p):
            return "portal", p
    for p in NOTICE_VARS:
        if body.startswith(p) or b.startswith(p):
            return "notices", p
    # 顶层接线：看它引用哪个页面的元素
    ids = set(re.findall(r"getElementById\('([^']+)'\)", b))
    p, n, lg = bool(ids & PORTAL_IDS), bool(ids & NOTICE_IDS), bool(ids & LOGIN_IDS)
    if p and not n:
        return "portal", "wiring"
    if n and not p:
        return "notices", "wiring"
    # 登录屏只存在于通知中心页。这类接线**绝不能进共用**：
    # 它没有 null 守卫，导航中心页上会 null.addEventListener 抛错，
    # 页面脚本在 boot() 之前就死掉（表现：图标空、侧栏空、一直「正在加载…」）。
    if lg and not p and not n:
        return "notices", "wiring"
    # `if (loginBtn) { ... }` 这种后续块里没有 getElementById（用的是上一个块声明的变量），
    # 按 ID 猜不到，必须按标识符再兜一层，否则它会留在共用里、在门户页上
    # 引用未定义的 loginBtn 而抛错。
    if re.search(r"\bloginBtn\b|\bloginForm\b", b):
        return "notices", "wiring"
    return "shared", "wiring"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    s = open(SRC, encoding="utf-8").read()
    style = re.search(r"<style[^>]*>(.*?)</style>", s, re.S).group(1)
    js = re.search(r"<script[^>]*>(.*?)</script>", s, re.S).group(1)

    inner = js[js.index("(function () {") + len("(function () {"): js.rindex("})();")]
    blocks = split_blocks(inner)
    buckets = {"shared": [], "portal": [], "notices": [], "drop": []}
    for b in blocks:
        kind, name = block_kind(b)
        buckets[kind].append(b)

    print("=" * 70)
    print(f"顶层块 {len(blocks)} 个")
    for k in ("shared", "portal", "notices", "drop"):
        tot = sum(len(x) for x in buckets[k])
        print(f"  {k:<8} {len(buckets[k]):>3} 块  {tot:>7} 字符")
    print("=" * 70)
    if not args.write:
        print("\n预演结束。加 --write 生成文件。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(SRC, os.path.join(BASE, "backups", f"index.html.bak-split-{stamp}"))
    print(f"\n已备份 → backups/index.html.bak-split-{stamp}")

    pub = os.path.join(BASE, "public")
    open(os.path.join(pub, "common.css"), "w", encoding="utf-8").write(style.strip() + "\n")
    print("  写出 public/common.css")
    print("\n各桶已就绪，接下来由 build_pages.py 生成 common.js / index.html / notices.html")
    import json
    i_login = s.index('<div class="login" id="loginScreen"')
    i_portal = s.index('<div id="portalView">')
    i_app = s.index('<div id="app" hidden>')
    i_modal = s.index('<div id="modalRoot">')
    json.dump({
        "buckets": buckets,
        "head": s[:s.index("<style")].strip(),
        "login_mk": s[i_login:i_portal].rstrip(),
        "portal_mk": s[i_portal:i_app].rstrip(),
        "notices_mk": s[i_app:i_modal].rstrip(),
        "tail": ('<div id="modalRoot"></div>\n'
                 '<div class="toast" id="toastRoot"></div>\n'
                 '<div id="scrim"></div>\n'),
    }, open("/tmp/nh_buckets.json", "w", encoding="utf-8"), ensure_ascii=False)
    print("  分桶结果 → /tmp/nh_buckets.json（含 head 与三段 markup）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
