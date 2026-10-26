#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二阶段：把 split_frontend.py 分好的桶生成成三个文件。
    common.js / index.html（导航中心） / notices.html（通知中心）

    python3 tools/build_pages.py            # 预演
    python3 tools/build_pages.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(BASE, "public")
SRC = os.path.join(PUB, "index.html")
BUCKETS = "/tmp/nh_buckets.json"

# 页面脚本需要从 NH 解构的共用名字（按实际引用自动补，这里列出必给的）
ALWAYS = ["esc", "ic", "svg", "toast", "api", "S", "isAdmin", "mountModal", "closeModal",
          "go", "getToken", "setToken", "renderMe", "applyTheme", "initTheme",
          "fillPwdBanner", "openChangePwd", "readLoginHash", "showLogin",
          "submitLoginByFetch", "armLoginFallback", "loginRememberUser", "copyText",
          "queryParams", "timeAgo", "fmtDT", "parseDT", "pad", "hl", "cleanSummary",
          "linkify", "isImageUrl", "renderAttachList", "todayStr", "fallbackCopy"]

PORTAL_BOOT = """
/* ======================= 启动（导航中心） ======================= */
function boot() {
  initTheme();
  document.getElementById('brandMark').innerHTML = ic('home', 17);
  document.getElementById('siSearch').innerHTML = ic('search', 17);
  document.getElementById('navQClear').innerHTML = ic('close', 15);
  document.getElementById('icRefresh').innerHTML = ic('refresh', 16);
  document.getElementById('icNavManage').innerHTML = ic('grid', 16);
  document.getElementById('icAccount').innerHTML = ic('user', 16);

  api('/api/session').then(function (res) { S.me = res.user; })
    .catch(function () { S.me = null; })
    .then(function () { renderMe(); loadNav(); });
}

/* 导航中心顶栏：非管理员藏掉管理员入口 */
NH.hooks.paintChrome = function () {
  ['navAccount', 'navManage', 'navRefresh'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.hidden = !isAdmin();
  });
  renderNavUser();
};
boot();
"""

NOTICES_BOOT = """
/* ======================= 启动（通知中心） ======================= */
function boot() {
  initTheme();
  document.getElementById('noticeBrandMark').innerHTML = ic('shield', 17);
  document.getElementById('siSearch').innerHTML = ic('search', 17);
  document.getElementById('icImport').innerHTML = ic('upload', 16);
  document.getElementById('icDown').innerHTML = ic('down', 16);
  document.getElementById('btnDrawer').innerHTML = ic('filter', 17);
  document.getElementById('btnStats').innerHTML = ic('chart', 17);
  document.getElementById('btnMenu').innerHTML = ic('menu', 17);
  document.getElementById('btnClearQ').innerHTML = ic('close', 15);
  setView(localStorage.getItem(NH.LS_VIEW) || 'card');
  try {
    var su = localStorage.getItem(NH.LS_USER) || '';
    if (su) document.getElementById('loginUser').value = su;
  } catch (e) {}

  var lh = readLoginHash();
  api('/api/session').then(function (res) {
    S.me = res.user;
    renderMe();
    enter();
  }).catch(function () {
    S.me = null;
    if (lh.error) S.loginMsg = lh.error;
    showLogin(S.loginMsg || '');
    S.loginMsg = '';
  });
}

/* 登录通过后进入通知列表 */
function enter() {
  document.getElementById('loginScreen').hidden = true;
  document.getElementById('app').hidden = false;
  renderMe();
  loadStats().catch(function () {}).then(function () {
    renderSidebar();
    return loadCategories();
  }).then(loadNotices);
}

/* 通知中心顶栏只有一个管理员入口要控制 */
NH.hooks.paintChrome = function () {
  var imp = document.getElementById('btnImport');
  if (imp) imp.hidden = !isAdmin();
};
boot();
"""



def replace_func(src: str, name: str, new_text: str) -> str:
    """按大括号配对整段替换一个顶层函数（正则的非贪婪 .*?\n} 会在内层 } 提前收尾）。"""
    m = re.search(r"\nfunction " + re.escape(name) + r"\s*\(", src)
    if not m:
        raise SystemExit("找不到函数 " + name)
    i = src.index("{", m.start())
    d = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            d += 1
        elif src[j] == "}":
            d -= 1
            if d == 0:
                return src[:m.start()] + "\n" + new_text + src[j + 1:]
    raise SystemExit("大括号不配对: " + name)



def top_level_names(decl: str):
    """从 var 声明里取出所有变量名。

    只按**括号深度为 0** 的逗号分割：否则 `var P = { a: 1, b: 2 };`
    会被切成 ['P = { a: 1', ' b: 2 };']，把对象键当成变量名导出
    （曾经生成 NH.arrowDown 这种不存在的导出）。
    """
    parts, buf, depth = [], [], 0
    quote = None
    i = 0
    while i < len(decl):
        ch = decl[i]
        if quote:
            if ch == "\\":
                buf.append(decl[i:i + 2]); i += 2; continue
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "\"'`":
            quote = ch; buf.append(ch)
        elif ch in "([{":
            depth += 1; buf.append(ch)
        elif ch in ")]}":
            depth -= 1; buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    out = []
    for p in parts:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", p)
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    data = json.load(open(BUCKETS, encoding="utf-8"))
    if "buckets" not in data:
        raise SystemExit("buckets JSON 是旧格式，请先跑 tools/split_frontend.py --write")
    buckets = data["buckets"]
    head, tail = data["head"], data["tail"]
    login_mk, portal_mk, notices_mk = data["login_mk"], data["portal_mk"], data["notices_mk"]

    shared_src = "\n\n".join(buckets["shared"])

    # ---- 共用源码的必要改写 ----
    # 1) renderMe 改为「提醒条 + 页面钩子」，不再直接引用某一页的元素
    shared_src = replace_func(shared_src, "renderMe", """function renderMe() {
  /* 两个页面各有自己的顶栏，所以这里只做共用部分（弱密码提醒），
     页面专属的顶栏绘制交给 NH.hooks.paintChrome。 */
  fillPwdBanner(S.me);
  if (NH.hooks && NH.hooks.paintChrome) {
    try { NH.hooks.paintChrome(); } catch (e) { console.error(e); }
  }
}""")

    # 2) showLogin 容错：两个页面各只有一部分视图元素
    shared_src = shared_src.replace(
        """  document.getElementById('loginScreen').hidden = false;
  document.getElementById('app').hidden = true;
  document.getElementById('portalView').hidden = true;""",
        """  var ls = document.getElementById('loginScreen');
  if (ls) ls.hidden = false;
  ['app', 'portalView'].forEach(function (id) {
    var elx = document.getElementById(id);
    if (elx) elx.hidden = true;
  });""")

    # 3) armLoginFallback 里的 app 元素加守卫
    shared_src = shared_src.replace(
        "    if (!document.getElementById('app').hidden) return;           // 已经登录进去了",
        "    var appEl = document.getElementById('app');\n"
        "    if (appEl && !appEl.hidden) return;                           // 已经登录进去了")

    # 4) 登录成功后的跳转：优先 ?next=，否则重新加载本页
    shared_src = re.sub(
        r"      var wantManage = S\.pendingManage.*?\n      \}\n      \}\n",
        """      /* 拆成两个页面后不再有 SPA 内跳转：带 ?next= 就回那一页，否则重载本页 */
      var nx = queryParams().next;
      if (nx && nx.charAt(0) === '/') { location.replace(nx); return; }
      location.reload();
""", shared_src, count=1, flags=re.S)

    # 5) go() 改为整页跳转
    shared_src = replace_func(shared_src, "go", """/* 两个页面之间的跳转：整页导航（不再有 hash 路由） */
var ROUTES = { portal: '/', notices: '/notices' };
function go(route) { location.href = ROUTES[route] || '/'; }""")

    # 6) 门户：站内链接改用真实路径（拆分后已无 hash 路由）
    pj = "\n\n".join(buckets["portal"])
    pj = pj.replace("""    var h = (url.hash || '').replace(/^#\\/?/, '');
    return h.indexOf('notices') === 0 ? 'notices' : 'portal';""",
"""    var h = (url.hash || '').replace(/^#\\/?/, '');
    if (h.indexOf('notices') === 0) return '/notices';
    if (url.pathname === '/notices' || url.pathname === '/notices.html') return '/notices';
    if (url.pathname === '/' || url.pathname === '/index.html') return '/';
    return '';""")
    pj = pj.replace("""  var href = route ? ('#/' + route) : navOpenTarget(it);
  var linkAttrs = route
    ? ('data-route="' + route + '"')
    : 'target="_blank" rel="noopener noreferrer"';""",
"""  var href = route || navOpenTarget(it);
  /* 站内页面同标签页打开；站外/本地应用一律新标签 */
  var linkAttrs = route ? '' : 'target="_blank" rel="noopener noreferrer"';""")
    pj = pj.replace("""  var href = route ? ('#/' + route) : navOpenTarget(it);
  var attrs = route
    ? ('data-route="' + route + '"')
    : 'target="_blank" rel="noopener noreferrer"';""",
"""  var href = route || navOpenTarget(it);
  var attrs = route ? '' : 'target="_blank" rel="noopener noreferrer"';""")
    pj = pj.replace("""        return '<a class="navchip" href="' + esc(croute ? ('#/' + croute) : navOpenTarget(it)) + '" ' +
          (croute ? ('data-route="' + croute + '"')
                  : 'target="_blank" rel="noopener noreferrer"') +
          ' data-id="' + it.id + '">' +""",
"""        return '<a class="navchip" href="' + esc(croute || navOpenTarget(it)) + '" ' +
          (croute ? '' : 'target="_blank" rel="noopener noreferrer"') +
          ' data-id="' + it.id + '">' +""")
    pj = pj.replace("""  var route = a.getAttribute('data-route');
  // 站内链接：同标签页切换（中键 / Ctrl+点击 交给浏览器新开标签）
  if (route && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    go(route);
  }
}""","""  /* 站内链接（如「重要通知中心」）现在就是普通链接、整页跳转，
     不再需要 preventDefault 做 SPA 切换。中键/Ctrl+点击由浏览器照常新开标签。 */
}""")
    pj = pj.replace("""  if (route) { go(route); return; }
  window.open(navOpenTarget(it), '_blank', 'noopener');""",
"""  if (route) { location.href = route; return; }
  window.open(navOpenTarget(it), '_blank', 'noopener');""")
    buckets["portal"] = [pj]

    # 7) 去掉旧的 boot()/applyRoute 调用尾巴
    shared_src = re.sub(r"\nboot\(\);\s*$", "\n", shared_src.rstrip()) + "\n"

    # ---- 导出 ----
    # 导出名单：只认每个块**开头**的 function / var 声明。
    # 不能在块内扫 ^var —— 去缩进后函数内部的局部变量也顶到了行首，
    # 之前就把 mountModal 里的 `var bg` 当成顶层变量导出了（NH.bg is not defined）。
    LEAD = re.compile(r"^(?:\s*/\*[\s\S]*?\*/|\s*//[^\n]*)\s*")
    names = set()
    for b in buckets["shared"]:
        body = b
        while True:
            st = LEAD.sub("", body, count=1)
            if st == body:
                break
            body = st
        m = re.match(r"^function ([A-Za-z0-9_]+)\s*\(", body)
        if m:
            names.add(m.group(1))
            continue
        # var 声明可能是多名字的：var A = 1, B = 2, C = 3;
        # 只导出第一个会让页面拿不到 B/C（曾导致「LS_NAVSEL is not defined」，
        # 表现是导航中心整页空白）。
        if body.startswith("var "):
            for nm in top_level_names(body[4:]):
                names.add(nm)
    # 这几个是 var 声明在同一个块里（var A = 1, B = 2;），补进来
    names |= {"LS_THEME", "LS_VIEW", "LS_TOKEN", "LS_USER", "SRC_COLOR", "SRC_LABEL",
              "SRC_ICON", "LV_COLOR", "LV_LABEL"}
    # 只导出「确实在共用桶里声明过」的名字：rndInt/genPassword 属于门户桶，
    # 硬编码进来会生成 NH.genPassword = genPassword 而 common.js 里没有它。
    exported = sorted(n for n in names if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n))

    common_js = (
        "/* notice-hub 共用前端库 —— 导航中心与通知中心共用。\n"
        "   由 tools/split_frontend.py + tools/build_pages.py 从原单文件版拆分生成。 */\n"
        "(function () {\n'use strict';\n\n"
        + shared_src +
        "\n\n/* ---------------- 对外导出 ---------------- */\n"
        "var NH = window.NH = { hooks: {} };\n"
        + "".join(f"NH.{n} = {n};\n" for n in exported) +
        "NH.LS_VIEW = LS_VIEW; NH.LS_USER = LS_USER; NH.LS_TOKEN = LS_TOKEN; NH.LS_THEME = LS_THEME;\n"
        "})();\n")

    def page_script(bucket_key, extra_boot):
        blocks = "\n\n".join(buckets[bucket_key])
        used = set()
        for n in exported:
            if re.search(r"\b" + re.escape(n) + r"\b", blocks) or re.search(r"\b" + re.escape(n) + r"\b", extra_boot):
                used.add(n)
        used |= {n for n in ALWAYS if n in exported}
        decls = ", ".join(f"{n} = NH.{n}" for n in sorted(used))
        return ("(function () {\n'use strict';\n\n"
                "var NH = window.NH;\n"
                f"var {decls};\n\n"
                + blocks + "\n" + extra_boot + "\n})();\n")

    portal_js = page_script("portal", PORTAL_BOOT)
    notices_js = page_script("notices", NOTICES_BOOT)

    def page(title, markup, js):
        h = re.sub(r"<title>.*?</title>", "<title>" + title + "</title>", head)
        return (h +
                "\n<link rel=\"stylesheet\" href=\"/common.css\">\n</head>\n<body>\n" +
                markup + "\n" + tail +
                "<script src=\"/common.js\"></script>\n<script>\n" + js +
                "</script>\n</body>\n</html>\n")

    print("=" * 70)
    print(f"head        {len(head):>7}")
    print(f"login markup{len(login_mk):>7}")
    print(f"portal markup{len(portal_mk):>7}")
    print(f"notices markup{len(notices_mk):>7}")
    print(f"common.js   {len(common_js):>7}  导出 {len(exported)} 个名字")
    print(f"index.html js{len(portal_js):>7}  解构 {portal_js.count('= NH.')} 个")
    print(f"notices.html js{len(notices_js):>7}  解构 {notices_js.count('= NH.')} 个")
    print("=" * 70)

    if not args.write:
        print("\n预演结束，加 --write 生成。")
        return 0

    open(os.path.join(PUB, "common.js"), "w", encoding="utf-8").write(common_js)
    open(os.path.join(PUB, "index.html"), "w", encoding="utf-8").write(
        page("太平洋电信导航中心", portal_mk, portal_js))
    open(os.path.join(PUB, "notices.html"), "w", encoding="utf-8").write(
        page("重要通知中心", login_mk + "\n" + notices_mk, notices_js))
    print("\n已写出 public/common.js, public/index.html, public/notices.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
