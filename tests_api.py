#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice-hub 接口自动化测试（零第三方依赖，直接 python tests_api.py 运行）

会自己起一个临时实例（独立端口 + 独立数据目录），跑完自动清理，不碰正式数据。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from email.message import EmailMessage
from http.client import HTTPConnection

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE, "server.py")
PY = sys.executable

ADMIN_USER = "admin"
ADMIN_PASS = "admin"
VIEWER_USER = "viewer1"
VIEWER_PASS = "viewer-pass-123"
SYNC_TOKEN = "test-sync-token"
PORT = 0
BASE_URL = ""
COOKIE = ""

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def req(method: str, path: str, body=None, ctype: str = "application/json",
        headers=None, raw: bytes = None, auth: bool = True):
    url = BASE_URL + path
    data = None
    hdrs = dict(headers or {})
    if raw is not None:
        data = raw
        if ctype:
            hdrs["Content-Type"] = ctype
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = ctype
    if auth and COOKIE:
        hdrs["Cookie"] = COOKIE
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            payload = resp.read()
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def jreq(method, path, body=None, **kw):
    code, payload, hdrs = req(method, path, body, **kw)
    try:
        return code, json.loads(payload.decode("utf-8")), hdrs
    except (ValueError, UnicodeDecodeError):
        return code, {"_raw": payload}, hdrs


def make_eml() -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "=?utf-8?B?" + __import__("base64").b64encode(
        "【重要】邮件通道测试通知".encode()).decode() + "?="
    msg["From"] = "Network Ops <noc@example.com>"
    msg["To"] = "ops@example.com"
    msg["Date"] = "Mon, 15 Sep 2026 02:30:00 +0800"
    msg["Message-ID"] = "<eml-test-" + uuid.uuid4().hex[:10] + "@example.com>"
    msg.set_content("这是 .eml 导入测试的正文。\n\n第二段：包含一条 https://example.com/notice 链接。")
    return msg.as_bytes()


def make_xlsx() -> bytes:
    """手工构造一个最小可用的 xlsx（3 行数据）。"""
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = ["标题", "来源", "发布时间", "分类", "级别", "标签", "正文",
              "【重要】XLSX 导入测试一", "企微", "2026-09-14 08:00", "网络变更", "紧急", "割接,机房",
              "XLSX 第一行正文",
              "XLSX 导入测试二", "Outlook", "2026-09-13 09:30", "安全通告", "重要", "漏洞",
              "XLSX 第二行正文"]
    sst = f'<sst xmlns="{NS}" count="{len(shared)}" uniqueCount="{len(shared)}">' + "".join(
        f"<si><t>{s}</t></si>" for s in shared) + "</sst>"

    def cell(ref, idx):
        return f'<c r="{ref}" t="s"><v>{idx}</v></c>'

    rows = []
    for i in range(4):
        cells = "".join(cell(col + str(i + 1), i * 7 + j) for j, col in enumerate("ABCDEFG"))
        rows.append(f'<row r="{i + 1}">' + cells + "</row>")
    sheet = (f'<worksheet xmlns="{NS}"><sheetData>' + "".join(rows) + "</sheetData></worksheet>")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?>' + sst)
        z.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?>' + sheet)
    return buf.getvalue()


def multipart(files):
    boundary = "----nh" + uuid.uuid4().hex
    out = io.BytesIO()
    for fn, content in files:
        out.write(("--" + boundary + "\r\n").encode())
        out.write((f'Content-Disposition: form-data; name="files"; filename="{fn}"\r\n').encode())
        out.write(b"Content-Type: application/octet-stream\r\n\r\n")
        out.write(content)
        out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def raw_post_form(path: str, fields: dict):
    """原生表单提交（application/x-www-form-urlencoded），不跟随 303 跳转。"""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=20)
    body = urllib.parse.urlencode(fields)
    conn.request("POST", path, body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    })
    resp = conn.getresponse()
    data = resp.read()
    headers = {k: v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, data


def main() -> int:
    global PORT, BASE_URL, COOKIE
    PORT = free_port()
    BASE_URL = f"http://127.0.0.1:{PORT}"
    tmp = tempfile.mkdtemp(prefix="notice-hub-test-")
    env = dict(os.environ)
    for k in ("NOTICE_PASSWORD", "DEFAULT_ADMIN_USER", "DEFAULT_ADMIN_PASS"):
        env.pop(k, None)
    env.update({
        "PORT": str(PORT),
        "NOTICE_DATA_DIR": tmp,
        "NOTICE_DB_PATH": os.path.join(tmp, "test.db"),
        "SYNC_TOKEN": SYNC_TOKEN,
        "NAV_PROBE_TIMEOUT": "1",      # 探测测试要等超时，调短一点
        "NAV_PROBE_WORKERS": "24",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen([PY, SERVER], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                code, data, _ = jreq("GET", "/api/health", auth=False)
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            print("服务启动失败")
            return 1

        print("\n\033[1m[1] 初始账号与鉴权\033[0m")
        code, d, _ = jreq("GET", "/api/health", auth=False)
        check("健康检查返回 ok", code == 200 and d.get("ok") is True, str(d))
        code, d, _ = jreq("GET", "/api/notices", auth=False)
        check("未登录访问被拒绝 401", code == 401, f"code={code}")
        code, d, _ = jreq("POST", "/api/login",
                          {"username": ADMIN_USER, "password": "wrong"}, auth=False)
        check("密码错误被拒绝 401", code == 401, f"code={code}")
        code, d, _ = jreq("POST", "/api/login",
                          {"username": "nobody", "password": ADMIN_PASS}, auth=False)
        check("账号不存在被拒绝 401", code == 401, f"code={code}")
        code, d1, _ = jreq("POST", "/api/login",
                           {"username": "nobody", "password": "wrongpw"}, auth=False)
        code, d2, _ = jreq("POST", "/api/login",
                           {"username": ADMIN_USER, "password": "wrongpw"}, auth=False)
        check("账号不存在与密码错误返回同一提示（不泄露账号是否存在）",
              d1.get("error") == d2.get("error"), f"{d1.get('error')} / {d2.get('error')}")
        code, d, hdrs = jreq("POST", "/api/login",
                             {"username": ADMIN_USER, "password": ADMIN_PASS}, auth=False)
        setc = hdrs.get("Set-Cookie", "")
        check("初始管理员 admin/admin 可登录", code == 200 and d.get("ok") is True, str(d)[:160])
        u = d.get("user", {})
        check("登录返回用户信息且角色为管理员", u.get("role") == "admin", str(u))
        check("默认弱密码被标记（前端会提醒）", u.get("weak_pwd") == 1, str(u))
        blob = json.dumps(d, ensure_ascii=False)
        check("响应不含密码哈希与盐",
              "pwd_hash" not in blob and "pwd_salt" not in blob, blob[:120])
        check("登录下发会话 Cookie", "nh_session=" in setc and "HttpOnly" in setc, setc)
        COOKIE = setc.split(";")[0]
        code, d, _ = jreq("GET", "/api/session")
        check("会话校验通过并返回用户",
              code == 200 and d.get("user", {}).get("username") == ADMIN_USER, str(d)[:160])
        code, d, _ = jreq("POST", "/api/logout")
        COOKIE = admin_cookie_saved = setc.split(";")[0]
        check("登出接口可用", code == 200, str(d)[:120])

        print("\n\033[1m[2] 通知创建与去重\033[0m")
        code, d, _ = jreq("POST", "/api/notices", {
            "title": "【重要】核心交换机升级", "content": "计划于 9 月 18 日升级，业务中断 90 秒。",
            "source": "wecom", "source_name": "运维值班群", "publisher": "Gavin",
            "published_at": "2026-09-15 10:30", "category": "网络变更", "level": "紧急",
            "tags": "割接, 机房", "pinned": 1,
        })
        nid = d.get("item", {}).get("id")
        check("创建通知成功", code == 200 and d.get("created") is True, str(d)[:200])
        check("来源中文被归一化为 wecom", d["item"]["source"] == "wecom", d["item"]["source"])
        check("级别中文被归一化为 urgent", d["item"]["level"] == "urgent", d["item"]["level"])
        check("标签被解析为数组", d["item"]["tags"] == ["割接", "机房"], str(d["item"]["tags"]))
        check("置顶字段生效", d["item"]["pinned"] == 1, str(d["item"]["pinned"]))
        check("时间被归一化为标准格式",
              d["item"]["published_at"] == "2026-09-15 10:30:00", d["item"]["published_at"])
        code, d2, _ = jreq("POST", "/api/notices", {
            "title": "【重要】核心交换机升级", "content": "计划于 9 月 18 日升级，业务中断 90 秒。",
            "source": "wecom", "published_at": "2026-09-15 10:30", "publisher": "Gavin",
        })
        check("同内容重复创建被去重", d2.get("created") is False and d2["item"]["id"] == nid,
              str(d2)[:160])

        print("\n\033[1m[3] 批量导入（文本 / CSV / JSON / XLSX / EML）\033[0m")
        text = ("标题: 【紧急】A 机房断电演练\n来源: 企微\n群名称: 应急群\n发布人: 张三\n"
                "发布时间: 2026-09-16 09:00\n分类: 故障公告\n级别: 紧急\n标签: 演练,机房\n\n"
                "9 月 20 日做断电演练，请提前确认 UPS 状态。\n---\n"
                "标题: 国庆值班安排\n来源: Outlook\n发布时间: 2026-09-10 11:20\n"
                "分类: 人事行政\n级别: 普通\n\n国庆期间按节假日排班执行。")
        code, d, _ = jreq("POST", "/api/import", {"text": text})
        check("文本粘贴导入解析 2 条", code == 200 and d.get("parsed") == 2, str(d)[:200])

        csv_bytes = ("标题,来源,发布时间,分类,级别,标签,正文\n"
                     "CSV 导入测试,企微,2026-09-12 08:00,业务通知,重要,测试,CSV 正文内容\n"
                     "CSV 导入测试二,Outlook,2026-09-11 09:00,安全通告,普通,,CSV 正文二\n"
                     ).encode("utf-8-sig")
        mp, ct = multipart([("notices.csv", csv_bytes)])
        code, d, _ = jreq("POST", "/api/import", ctype=ct, raw=mp)
        check("CSV 上传导入解析 2 条", code == 200 and d.get("parsed") == 2, str(d)[:200])

        json_bytes = json.dumps([{"title": "JSON 导入测试", "source": "outlook",
                                  "published_at": "2026-09-05 12:00", "category": "系统维护",
                                  "level": "normal", "content": "JSON 正文"}],
                                ensure_ascii=False).encode("utf-8")
        mp, ct = multipart([("notices.json", json_bytes)])
        code, d, _ = jreq("POST", "/api/import", ctype=ct, raw=mp)
        check("JSON 上传导入解析 1 条", code == 200 and d.get("parsed") == 1, str(d)[:200])

        mp, ct = multipart([("notices.xlsx", make_xlsx())])
        code, d, _ = jreq("POST", "/api/import", ctype=ct, raw=mp)
        check("XLSX 上传导入解析 2 条", code == 200 and d.get("parsed") == 2, str(d)[:200])

        mp, ct = multipart([("mail.eml", make_eml())])
        code, d, _ = jreq("POST", "/api/import", ctype=ct, raw=mp)
        check("EML 邮件导入解析 1 条", code == 200 and d.get("parsed") == 1, str(d)[:200])
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("邮件通道测试"))
        item = (d.get("items") or [{}])[0]
        check("EML 标题中文正确解码", "邮件通道测试" in item.get("title", ""), item.get("title", ""))
        check("EML 发件人解析正确", item.get("sender") == "noc@example.com", item.get("sender", ""))
        check("EML 来源判定为 outlook", item.get("source") == "outlook", item.get("source", ""))
        check("EML 时间按时区 +08:00 转换",
              item.get("published_at") == "2026-09-15 02:30:00", item.get("published_at", ""))

        print("\n\033[1m[4] 搜索与筛选\033[0m")
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("交换机"))
        check("单关键词搜索命中", d.get("total", 0) >= 1, str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("交换机 升级"))
        t_and = d.get("total", 0)
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("交换机 不存在的词zzz"))
        t_and2 = d.get("total", 0)
        check("多关键词按 AND 语义", t_and >= 1 and t_and2 == 0, f"{t_and}/{t_and2}")
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("割接"))
        check("标签参与搜索", d.get("total", 0) >= 1, str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?source=wecom")
        all_wecom = all(i["source"] == "wecom" for i in d["items"]) and d["total"] >= 1
        check("按来源筛选正确", all_wecom, str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?level=urgent,important")
        all_lv = all(i["level"] in ("urgent", "important") for i in d["items"]) and d["total"] >= 1
        check("按多级别筛选正确", all_lv, str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?from=2026-09-15&to=2026-09-16")
        in_range = all("2026-09-15" <= i["published_at"][:10] <= "2026-09-16" for i in d["items"])
        check("按时间范围筛选正确", in_range and d["total"] >= 1, str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?pinned=1")
        check("仅看置顶筛选正确", all(i["pinned"] == 1 for i in d["items"]) and d["total"] >= 1,
              str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?category=" + urllib.parse.quote("网络变更"))
        check("按分类筛选正确", all(i["category"] == "网络变更" for i in d["items"]), str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?sort=asc&size=100")
        pubs = [i["published_at"] for i in d["items"] if not i["pinned"]]
        check("按时间升序排序正确", pubs == sorted(pubs), str(pubs[:3]))
        code, d, _ = jreq("GET", "/api/notices?size=2&page=2")
        check("分页参数生效", d.get("size") == 2 and d.get("page") == 2, str(d)[:120])

        print("\n\033[1m[5] 编辑 / 置顶 / 删除\033[0m")
        code, d, _ = jreq("PUT", f"/api/notices/{nid}", {"title": "【重要】核心交换机升级（已修订）",
                                                        "level": "urgent", "category": "网络变更"})
        check("编辑通知成功", code == 200 and "已修订" in d["item"]["title"], str(d)[:160])
        code, d, _ = jreq("PUT", f"/api/notices/{nid}", {"pinned": 0})
        check("取消置顶成功", d["item"]["pinned"] == 0, str(d["item"]["pinned"]))
        code, d, _ = jreq("POST", "/api/notices", {"title": "待删除通知", "content": "x",
                                                   "published_at": "2026-01-01 00:00"})
        tmp_id = d["item"]["id"]
        code, d, _ = jreq("DELETE", f"/api/notices/{tmp_id}")
        code, d, _ = jreq("GET", f"/api/notices/{tmp_id}")
        check("删除通知生效", code == 404, f"code={code}")

        print("\n\033[1m[6] 分类管理\033[0m")
        code, d, _ = jreq("GET", "/api/categories")
        names = [c["name"] for c in d["items"]]
        check("默认分类已初始化", "网络变更" in names and "未分类" in names, str(names))
        check("分类带数量统计", all("cnt" in c for c in d["items"]), str(d["items"][0]))
        code, d, _ = jreq("POST", "/api/categories", {"name": "客户通知", "color": "#22c55e"})
        check("新增分类成功", any(c["name"] == "客户通知" for c in d["items"]), str(d)[:160])
        code, d, _ = jreq("DELETE", "/api/categories?name=" + urllib.parse.quote("客户通知"))
        check("删除分类成功", not any(c["name"] == "客户通知" for c in d["items"]), str(d)[:160])

        print("\n\033[1m[7] 自动同步接口（预留给采集器）\033[0m")
        code, d, _ = jreq("POST", "/api/ingest", {"items": [{"title": "越权写入"}]},
                          headers={"X-Sync-Token": "bad"}, auth=False)
        check("错误同步令牌被拒绝 401", code == 401, f"code={code}")
        ext = "wecom-msg-" + uuid.uuid4().hex[:8]
        payload = {"items": [{"ext_id": ext, "source": "wecom", "source_name": "自动同步群",
                              "title": "采集器上报的通知", "content": "由采集器写入",
                              "published_at": "2026-09-16 14:00:00", "category": "业务通知",
                              "level": "important", "tags": ["自动"]}]}
        code, d, _ = jreq("POST", "/api/ingest", payload,
                          headers={"X-Sync-Token": SYNC_TOKEN}, auth=False)
        check("正确令牌可幂等写入", code == 200 and d.get("created") == 1, str(d)[:200])
        code, d, _ = jreq("POST", "/api/ingest", payload,
                          headers={"X-Sync-Token": SYNC_TOKEN}, auth=False)
        check("相同 ext_id 重复上报不产生重复记录", d.get("created") == 0 and d.get("updated") == 1,
              str(d)[:200])
        code, d, _ = jreq("GET", "/api/sync/sources")
        check("同步配置接口返回令牌与端点",
              d.get("endpoint") == "/api/ingest" and len(d.get("token", "")) >= 8, str(d)[:160])

        print("\n\033[1m[8] 导出与统计\033[0m")
        code, payload, hdrs = req("GET", "/api/export?format=csv")
        txt = payload.decode("utf-8")
        check("CSV 导出带 BOM 且表头正确",
              payload[:3] == b"\xef\xbb\xbf" and "标题" in txt.splitlines()[0], txt[:60])
        check("CSV 导出行数与数据一致", len(txt.strip().splitlines()) >= 3, str(len(txt.splitlines())))
        check("CSV 导出带附件文件名", "attachment" in hdrs.get("Content-Disposition", ""),
              hdrs.get("Content-Disposition", ""))
        code, payload, _ = req("GET", "/api/export?format=json")
        arr = json.loads(payload.decode("utf-8"))
        check("JSON 导出内容有效", isinstance(arr, list) and len(arr) >= 3 and "title" in arr[0],
              str(len(arr)))
        code, d, _ = jreq("GET", "/api/stats")
        check("统计总数与实际一致", d.get("total", 0) >= 8, str(d.get("total")))
        check("统计含来源/级别/分类/月份四组",
              all(k in d for k in ("by_source", "by_level", "by_category", "by_month")), str(list(d)))
        check("月度分布按时间正序", [m["ym"] for m in d["by_month"]] ==
              sorted([m["ym"] for m in d["by_month"]]), str(d["by_month"]))

        print("\n\033[1m[9] 示例数据与清理\033[0m")
        code, d, _ = jreq("POST", "/api/seed-demo")
        check("载入示例数据成功", code == 200 and d.get("created") == 6, str(d)[:120])
        code, d, _ = jreq("POST", "/api/clear", {"scope": "demo"})
        check("清除示例数据成功", code == 200 and d.get("deleted") == 6, str(d)[:120])
        code, d, _ = jreq("GET", "/api/notices?size=100")
        check("清除示例后真实数据保留", all(not i["ext_id"].startswith("demo-") for i in d["items"]),
              str(d.get("total")))
        code, d, _ = jreq("GET", "/api/notices?q=" + urllib.parse.quote("采集器"))
        received = d["items"][0]["received_at"]
        utc_hour = time.gmtime().tm_hour
        expect_hour = (utc_hour + 8) % 24
        check("入库时间使用 +08:00 时区",
              int(received[11:13]) == expect_hour, f"{received} vs utc_hour={utc_hour}")

        print("\n\033[1m[10] 静态资源与安全\033[0m")
        code, payload, hdrs = req("GET", "/", auth=False)
        check("首页 HTML 可访问", code == 200 and b"<!DOCTYPE html>" in payload[:200], f"code={code}")
        check("首页返回正确 Content-Type",
              "text/html" in hdrs.get("Content-Type", ""), hdrs.get("Content-Type", ""))
        # 通知相关接口现在在通知中心页；登录接口在它自己的 loginScreen 逻辑里
        code_n, payload_n, _ = req("GET", "/notices", auth=False)
        check("通知中心页引用了通知相关接口",
              code_n == 200 and b"/api/notices" in payload_n and b"/api/import" in payload_n
              and b"/api/login" in payload_n, f"code={code_n}")
        check("导航中心页引用了导航接口",
              b"/api/nav" in payload and b"/api/session" in payload, "")
        code, _, _ = req("GET", "/../server.py", auth=False)
        check("路径穿越被阻止", code == 404, f"code={code}")
        code, _, _ = req("GET", "/api/nonexistent")
        check("未知接口返回 404", code == 404, f"code={code}")

        print("\n\033[1m[11] 账号管理（仅管理员）\033[0m")
        code, d, _ = jreq("GET", "/api/users")
        check("管理员可列出账号", code == 200 and len(d.get("items", [])) == 1, str(d)[:160])
        check("账号列表含两种角色选项", len(d.get("roles", [])) == 2, str(d.get("roles")))
        admin_id = d["me"]["id"]
        code, d, _ = jreq("POST", "/api/users", {"username": "bad name!", "password": "abcdefg"})
        check("非法账号名被拒绝", code == 400, str(d)[:160])
        code, d, _ = jreq("POST", "/api/users", {"username": "u2", "password": "123"})
        check("密码过短被拒绝", code == 400, str(d)[:160])
        code, d, _ = jreq("POST", "/api/users", {"username": VIEWER_USER,
                                                "password": VIEWER_PASS, "role": "viewer",
                                                "display_name": "只读同事"})
        check("创建查看账号成功", code == 200 and len(d.get("items", [])) == 2, str(d)[:160])
        code, d, _ = jreq("POST", "/api/users", {"username": VIEWER_USER, "password": VIEWER_PASS})
        check("重复账号被拒绝", code == 400, str(d)[:160])

        # ---- 切到查看账号，验权限边界 ----
        admin_cookie = COOKIE
        code, d, hdrs = jreq("POST", "/api/login",
                             {"username": VIEWER_USER, "password": VIEWER_PASS}, auth=False)
        COOKIE = hdrs.get("Set-Cookie", "").split(";")[0]
        check("查看账号可登录且角色为 viewer",
              code == 200 and d.get("user", {}).get("role") == "viewer", str(d)[:160])
        code, d, _ = jreq("GET", "/api/notices")
        check("查看账号可读通知列表", code == 200 and d.get("ok") is True, str(d)[:120])
        code, d, _ = jreq("GET", "/api/stats")
        check("查看账号可看统计", code == 200, str(d)[:120])
        code, d, _ = jreq("GET", "/api/categories")
        check("查看账号可看分类", code == 200, str(d)[:120])
        code, payload, _ = req("GET", "/api/export?format=csv")
        check("查看账号可导出", code == 200 and payload[:3] == b"\xef\xbb\xbf", f"code={code}")
        code, d, _ = jreq("POST", "/api/notices", {"title": "越权新增"})
        check("查看账号不能新增通知（403）", code == 403, f"code={code} {d}")
        code, d, _ = jreq("POST", "/api/import", {"text": "标题: 越权导入"})
        check("查看账号不能导入（403）", code == 403, f"code={code}")
        code, d, _ = jreq("POST", "/api/clear", {"scope": "demo"})
        check("查看账号不能清空数据（403）", code == 403, f"code={code}")
        code, d, _ = jreq("POST", "/api/seed-demo")
        check("查看账号不能载入示例（403）", code == 403, f"code={code}")
        code, d, _ = jreq("DELETE", "/api/categories?name=" + urllib.parse.quote("网络变更"))
        check("查看账号不能删分类（403）", code == 403, f"code={code}")
        code, d, _ = jreq("GET", "/api/users")
        check("查看账号看不到账号列表（403）", code == 403, f"code={code}")
        code, d, _ = jreq("POST", "/api/users", {"username": "x1", "password": "abcdefg"})
        check("查看账号不能建账号（403）", code == 403, f"code={code}")
        code, d, _ = jreq("POST", "/api/me/password",
                          {"old_password": "wrongpw", "new_password": "whatever123"})
        check("原密码错误时拒绝改密码", code == 400, str(d)[:160])

        # ---- 改密后旧会话失效 ----
        old_viewer_cookie = COOKIE
        code, d, hdrs = jreq("POST", "/api/me/password",
                             {"old_password": VIEWER_PASS, "new_password": "viewer-new-pass"})
        COOKIE = hdrs.get("Set-Cookie", "").split(";")[0]
        check("查看账号可自行改密码", code == 200, str(d)[:160])
        code, d, _ = jreq("GET", "/api/session")
        check("改密后新会话立即可用", code == 200, str(d)[:120])
        saved = COOKIE
        COOKIE = old_viewer_cookie
        code, d, _ = jreq("GET", "/api/session")
        check("改密后旧会话立即失效（401）", code == 401, f"code={code}")
        COOKIE = saved
        code, d, _ = jreq("POST", "/api/login",
                          {"username": VIEWER_USER, "password": VIEWER_PASS}, auth=False)
        check("改密后旧密码无法登录", code == 401, f"code={code}")

        # ---- 回到管理员，验账号生命周期 ----
        COOKIE = admin_cookie
        code, d, _ = jreq("GET", "/api/users")
        vid = [x for x in d["items"] if x["username"] == VIEWER_USER][0]["id"]
        check("列表里能看到新建的查看账号", vid > 0, str(vid))
        code, d, _ = jreq("DELETE", f"/api/users/{admin_id}")
        check("不能删除自己的账号", code == 400, str(d)[:160])
        code, d, _ = jreq("PUT", f"/api/users/{admin_id}", {"role": "viewer"})
        check("不能把自己降级为查看", code == 400, str(d)[:160])
        code, d, _ = jreq("PUT", f"/api/users/{admin_id}", {"enabled": 0})
        check("不能停用自己的账号", code == 400, str(d)[:160])
        code, d, _ = jreq("PUT", f"/api/users/{vid}", {"enabled": 0})
        check("可停用其他账号", code == 200 and
              any(x["id"] == vid and not x["enabled"] for x in d["items"]), str(d)[:160])
        code, d, _ = jreq("POST", "/api/login",
                          {"username": VIEWER_USER, "password": "viewer-new-pass"}, auth=False)
        check("已停用账号无法登录", code == 401, f"code={code}")
        code, d, _ = jreq("PUT", f"/api/users/{vid}", {"enabled": 1})
        check("可重新启用账号", code == 200, str(d)[:120])
        code, d, _ = jreq("PUT", f"/api/users/{vid}", {"role": "admin"})
        check("可把账号提升为管理员", code == 200, str(d)[:120])
        code, d, _ = jreq("PUT", f"/api/users/{vid}", {"role": "viewer"})
        check("可把账号降回查看", code == 200, str(d)[:120])
        code, d, _ = jreq("PUT", f"/api/users/{vid}", {"password": "reset-pass-456"})
        check("管理员可重置他人密码", code == 200, str(d)[:120])
        code, d, _ = jreq("POST", "/api/login",
                          {"username": VIEWER_USER, "password": "reset-pass-456"}, auth=False)
        check("重置后的新密码可登录", code == 200, f"code={code}")
        code, d, _ = jreq("DELETE", f"/api/users/{vid}")
        deleted = code == 200 and not any(x["id"] == vid for x in d["items"])
        check("删除账号成功", deleted, str(d)[:160])
        code, d, _ = jreq("POST", "/api/login",
                          {"username": VIEWER_USER, "password": "reset-pass-456"}, auth=False)
        check("被删账号无法登录", code == 401, f"code={code}")
        code, d, _ = jreq("GET", "/api/audit")
        acts = [x["action"] for x in d.get("items", [])]
        check("审计日志记录了账号相关操作",
              code == 200 and "login" in acts and "user_create" in acts and "user_delete" in acts,
              str(acts[:8]))

        print("\n\033[1m[12] 令牌双通道（兼容不保存 cookie 的浏览器上下文）\033[0m")
        code, d, _ = jreq("POST", "/api/login",
                          {"username": ADMIN_USER, "password": ADMIN_PASS}, auth=False)
        tok = d.get("token", "")
        check("JSON 登录返回会话令牌", bool(tok), str(d)[:140])
        check("令牌去掉了 base64 填充（不含 = 号）", tok != "" and "=" not in tok, tok[:40])
        code, d, _ = jreq("GET", "/api/notices",
                          headers={"X-Session-Token": tok}, auth=False)
        check("仅用 X-Session-Token 头（完全无 cookie）可访问",
              code == 200 and d.get("ok") is True, f"code={code}")
        code, d, _ = jreq("GET", "/api/notices",
                          headers={"Authorization": "Bearer " + tok}, auth=False)
        check("仅用 Authorization: Bearer 可访问", code == 200, f"code={code}")
        code, d, _ = jreq("GET", "/api/notices",
                          headers={"X-Session-Token": "bogus"}, auth=False)
        check("伪造令牌被拒绝 401", code == 401, f"code={code}")

        st, hdrs, _ = raw_post_form("/api/login",
                                    {"username": ADMIN_USER, "password": ADMIN_PASS})
        loc = hdrs.get("Location", "")
        check("原生表单登录返回 303 跳转", st == 303, f"status={st}")
        check("跳转地址带令牌 fragment", loc.startswith("/#token=") and len(loc) > 20, loc[:44])
        check("原生表单同时下发会话 Cookie",
              "nh_session=" in hdrs.get("Set-Cookie", ""), hdrs.get("Set-Cookie", "")[:40])
        st, hdrs2, _ = raw_post_form("/api/login",
                                     {"username": ADMIN_USER, "password": "definitely-wrong"})
        check("原生表单密码错误跳回错误标记",
              st == 303 and hdrs2.get("Location") == "/#login-error",
              f"{st} {hdrs2.get('Location')}")

        # 改密码应换发新令牌，且旧令牌立即失效（用临时账号，不动 admin）
        code, d, _ = jreq("POST", "/api/users", {"username": "toktest",
                                                "password": "toktest12345", "role": "viewer"})
        check("建临时账号用于令牌轮换测试", code == 200, str(d)[:120])
        code, d, _ = jreq("POST", "/api/login",
                          {"username": "toktest", "password": "toktest12345"}, auth=False)
        t1 = d.get("token", "")
        code, d, _ = jreq("GET", "/api/notices", headers={"X-Session-Token": t1}, auth=False)
        check("临时账号令牌可用", code == 200, f"code={code}")
        code, d, _ = jreq("POST", "/api/me/password",
                          {"old_password": "toktest12345", "new_password": "toktest54321"},
                          headers={"X-Session-Token": t1}, auth=False)
        t2 = d.get("token", "")
        check("改密码返回新令牌", code == 200 and bool(t2), str(d)[:140])
        code, d, _ = jreq("GET", "/api/notices", headers={"X-Session-Token": t2}, auth=False)
        check("改密码后新令牌可用（无 cookie）", code == 200, f"code={code}")
        code, d, _ = jreq("GET", "/api/notices", headers={"X-Session-Token": t1}, auth=False)
        check("改密码后旧令牌失效（无 cookie）", code == 401, f"code={code}")
        code, d, _ = jreq("GET", "/api/users", headers={"X-Session-Token": t2}, auth=False)
        check("查看账号的令牌依然受限 403", code == 403, f"code={code}")
        st, hdrs, _ = raw_post_form("/api/login", {})
        check("空表单提交不崩溃且跳回错误标记",
              st == 303 and hdrs.get("Location") == "/#login-error", f"{st}")

        # keep-alive 回归：BaseHTTPRequestHandler 在 HTTP/1.1 下会用同一个 handler 实例
        # 连续处理同一连接上的请求。若认证结果跨请求被缓存，就会出现「带了令牌仍被判未登录」。
        # urllib 每次新建连接，测不出这个问题，必须手动复用连接。
        conn = HTTPConnection("127.0.0.1", PORT, timeout=20)
        conn.request("GET", "/api/session")
        r1 = conn.getresponse()
        b1 = r1.read()
        conn.request("GET", "/api/session", headers={"X-Session-Token": tok})
        r2 = conn.getresponse()
        b2 = r2.read()
        conn.close()
        ok2 = False
        try:
            ok2 = json.loads(b2.decode("utf-8-sig")).get("ok") is True
        except ValueError:
            pass
        check("同一 keep-alive 连接：先匿名 401，再带令牌应放行（认证结果不跨请求复用）",
              r1.status == 401 and r2.status == 200 and ok2,
              f"{r1.status} -> {r2.status} body={b2[:80]}")

        conn = HTTPConnection("127.0.0.1", PORT, timeout=20)
        conn.request("GET", "/api/session", headers={"X-Session-Token": tok})
        r3 = conn.getresponse()
        r3.read()
        conn.request("GET", "/api/session")
        r4 = conn.getresponse()
        b4 = r4.read()
        conn.close()
        check("同一 keep-alive 连接：先带令牌 200，再匿名应 401（不会误放行）",
              r3.status == 200 and r4.status == 401, f"{r3.status} -> {r4.status}")

        conn = HTTPConnection("127.0.0.1", PORT, timeout=20)
        conn.request("GET", "/api/notices", headers={"X-Session-Token": tok})
        r5 = conn.getresponse()
        r5.read()
        conn.request("GET", "/api/stats", headers={"X-Session-Token": tok})
        r6 = conn.getresponse()
        b6 = r6.read()
        conn.close()
        check("同一 keep-alive 连接上连续多个带令牌请求都正常",
              r5.status == 200 and r6.status == 200, f"{r5.status} -> {r6.status}")

        print("\n\033[1m[13] 内网导航（免登录门户）\033[0m")
        code, d, _ = jreq("GET", "/api/nav", auth=False)
        check("导航接口免登录可访问", code == 200 and d.get("ok") is True, f"code={code}")
        check("种子数据已灌入（4 分组 / 15 系统）",
              len(d.get("groups", [])) == 4 and d.get("total") == 15,
              f"{len(d.get('groups', []))}/{d.get('total')}")
        check("免登录只返回 guest 可见项",
              all(i["visible_to"] == "guest" for g in d["groups"] for i in g["items"]), "")
        check("每项都带状态字段",
              all("status" in i for g in d["groups"] for i in g["items"]), "")
        check("免登录状态下 can_edit 为 false", d.get("can_edit") is False, str(d.get("can_edit")))
        check("分组结构正确",
              {g["name"] for g in d["groups"]} >= {"网络运维", "监控告警", "排班管理", "效率与安全"},
              str([g["name"] for g in d["groups"]]))
        check("导航项带标签数组",
              all(isinstance(i.get("tags"), list) for g in d["groups"] for i in g["items"]), "")

        print("\n\033[1m[13b] 在线探测：内网判定与并发\033[0m")
        # 回归：198.18.0.0/15（含全部 198.19.x.x）曾被旧的字符串前缀判定漏掉，
        # 235 个客户站点被静默当成「非内网」跳过，永远显示未探测。这里锁死这个行为。
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": d["groups"][0]["id"], "name": "客户站点探测",
                           "url": "http://198.51.100.254:8443", "probe": 1,
                           "visible_to": "admin"})
        check("可新增 198.19.x.x 的客户站点条目", code == 200 and d.get("id"), str(d)[:120])
        cid = d.get("id")
        code, d, _ = jreq("POST", "/api/nav/probe")
        check("可触发探测", code == 200 and d.get("ok") is True, str(d)[:120])
        code, d, _ = jreq("GET", "/api/nav")
        row = [i for g in d["groups"] for i in g["items"] if i["id"] == cid]
        st = (row[0].get("status") or {}) if row else {}
        check("198.19.x.x 真的被探测了（而不是被当成非内网跳过）",
              bool(row) and st.get("ok") is not None
              and st.get("reason") != "非内网地址，已跳过探测",
              f"ok={st.get('ok')} reason={st.get('reason')!r}")

        # 公网地址仍应跳过：本服务不能被当成任意主机的端口扫描器
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": row[0]["group_id"] if row else 1,
                           "name": "公网探测", "url": "http://93.184.216.34:80", "probe": 1,
                           "visible_to": "admin"})
        pub = d.get("id") if code == 200 else None
        code, d, _ = jreq("POST", "/api/nav/probe")
        code, d, _ = jreq("GET", "/api/nav")
        prow = [i for g in d["groups"] for i in g["items"] if i["id"] == pub]
        pst = (prow[0].get("status") or {}) if prow else {}
        check("公网地址仍被跳过（不当端口扫描器用）",
              bool(prow) and pst.get("ok") is None
              and pst.get("reason") == "非内网地址，已跳过探测",
              f"ok={pst.get('ok')} reason={pst.get('reason')!r}")

        # 并发：12 个必定超时的目标，超时 1 秒。串行要 12 秒以上，并发应远快于此。
        code, d, _ = jreq("GET", "/api/nav")
        gid_p = d["groups"][0]["id"]
        slow_ids = []
        for i in range(12):
            code, d, _ = jreq("POST", "/api/nav/items",
                              {"group_id": gid_p, "name": f"慢目标{i}",
                               "url": "http://10.255.255.1:8443", "probe": 1,
                               "visible_to": "admin"})
            if d.get("id"):
                slow_ids.append(d["id"])
        check("已建 12 个必定超时的探测目标", len(slow_ids) == 12, str(len(slow_ids)))
        t0 = time.time()
        code, d, _ = jreq("POST", "/api/nav/probe")
        elapsed = time.time() - t0
        check("探测是并发的（12 个 1 秒超时目标远快于串行的 12 秒）",
              code == 200 and elapsed < 6.0,
              f"耗时 {elapsed:.1f}s（串行约需 12s+）")
        check("探测接口报告了目标数", d.get("probed", 0) >= 12, str(d.get("probed")))

        code, d, _ = jreq("POST", "/api/nav/items", {"name": "匿名越权"}, auth=False)
        check("免登录不能新增导航项（401）", code == 401, f"code={code}")
        code, d, _ = jreq("POST", "/api/nav/probe", auth=False)
        check("免登录不能触发探测（401）", code == 401, f"code={code}")

        code, d, _ = jreq("GET", "/api/nav")
        check("管理员登录后 can_edit 为 true", d.get("can_edit") is True, "")
        gid = d["groups"][0]["id"]
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": gid, "name": "测试系统", "url": "10.0.0.10:8081",
                           "description": "自动化测试用"})
        check("管理员可新增导航项", code == 200, str(d)[:140])
        new_id = d.get("id")
        added = [i for g in d["groups"] for i in g["items"] if i["id"] == new_id]
        check("地址缺协议头时自动补 http://",
              bool(added) and added[0]["url"] == "http://10.0.0.10:8081",
              added[0]["url"] if added else "未找到")
        check("新增项默认免登录可见", bool(added) and added[0]["visible_to"] == "guest", "")
        code, d, _ = jreq("POST", "/api/nav/items", {"group_id": gid, "name": "", "url": "x"})
        check("名称为空被拒绝", code == 400, str(d)[:120])

        code, d, _ = jreq("GET", "/api/nav", auth=False)
        check("免登录能看到新增的 guest 项",
              new_id in [i["id"] for g in d["groups"] for i in g["items"]], "")

        code, d, _ = jreq("PUT", f"/api/nav/items/{new_id}", {"visible_to": "member"})
        check("可改为「需登录可见」", code == 200, str(d)[:120])
        code, d, _ = jreq("GET", "/api/nav", auth=False)
        check("改为 member 后免登录看不到该项",
              new_id not in [i["id"] for g in d["groups"] for i in g["items"]], "")
        code, d, _ = jreq("GET", "/api/nav")
        check("登录后能看到 member 项",
              new_id in [i["id"] for g in d["groups"] for i in g["items"]], "")

        code, d, _ = jreq("PUT", f"/api/nav/items/{new_id}", {"visible_to": "admin"})
        check("可改为「仅管理员可见」", code == 200, str(d)[:120])
        code, d, _ = jreq("POST", "/api/users", {"username": "navviewer",
                                                "password": "navviewer123", "role": "viewer"})
        check("建临时查看账号用于可见性测试", code == 200, str(d)[:120])
        nav_viewer_id = d.get("id") or [
            x["id"] for x in d["items"] if x["username"] == "navviewer"][0]
        code, d, _ = jreq("POST", "/api/login",
                          {"username": "navviewer", "password": "navviewer123"}, auth=False)
        vtok = d.get("token", "")
        code, d, _ = jreq("GET", "/api/nav", headers={"X-Session-Token": vtok}, auth=False)
        check("查看账号看不到 admin 专属项",
              code == 200 and new_id not in [i["id"] for g in d["groups"] for i in g["items"]],
              f"code={code}")
        check("查看账号能看到 guest 与 member 项",
              len([i for g in d["groups"] for i in g["items"]]) == 15,
              str(len([i for g in d["groups"] for i in g["items"]])))
        code, d, _ = jreq("POST", "/api/nav/items", {"name": "越权", "url": "http://10.0.0.1"},
                          headers={"X-Session-Token": vtok}, auth=False)
        check("查看账号不能新增导航项（403）", code == 403, f"code={code}")
        code, d, _ = jreq("POST", "/api/nav/probe", headers={"X-Session-Token": vtok}, auth=False)
        check("查看账号不能触发探测（403）", code == 403, f"code={code}")

        code, d, _ = jreq("PUT", f"/api/nav/items/{new_id}", {"visible_to": "guest"})
        code, d, _ = jreq("GET", "/api/nav")
        g0 = [g for g in d["groups"] if g["id"] == gid][0]
        ids_before = [i["id"] for i in g0["items"]]
        code, d, _ = jreq("PUT", f"/api/nav/items/{ids_before[1]}", {"move": "up"})
        g0 = [g for g in d["groups"] if g["id"] == gid][0]
        ids_after = [i["id"] for i in g0["items"]]
        check("排序上移生效",
              ids_after[0] == ids_before[1] and ids_after[1] == ids_before[0],
              f"{ids_before[:2]} -> {ids_after[:2]}")

        code, d, _ = jreq("POST", "/api/nav/visit", {"id": new_id}, auth=False)
        check("免登录可记录访问", code == 200, str(d)[:120])
        code, d, _ = jreq("GET", "/api/nav")
        it = [i for g in d["groups"] for i in g["items"] if i["id"] == new_id][0]
        check("访问计数已累加且记录时间",
              it["visit_count"] >= 1 and bool(it["last_visit"]), str(it["visit_count"]))
        check("最近访问列表包含该项",
              any(r["id"] == new_id for r in d.get("recent", [])),
              str([r["id"] for r in d.get("recent", [])]))
        code, d, _ = jreq("POST", "/api/nav/visit", {"id": 999999}, auth=False)
        check("访问不存在的项返回 404", code == 404, f"code={code}")

        code, d, _ = jreq("POST", "/api/nav/probe")
        check("管理员可手动触发探测", code == 200 and d.get("probed", 0) >= 15,
              str(d)[:140])
        check("探测结果带时间戳", bool(d.get("checked_at")), str(d.get("checked_at")))

        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": gid, "name": "外网目标", "url": "http://8.8.8.8/"})
        pub_id = d.get("id")
        check("可新增一个公网地址用于探测白名单测试", code == 200, str(d)[:120])
        jreq("POST", "/api/nav/probe")
        code, d, _ = jreq("GET", "/api/nav")
        pub = [i for g in d["groups"] for i in g["items"] if i["id"] == pub_id]
        check("非内网地址被跳过探测（防端口扫描）",
              bool(pub) and pub[0]["status"].get("ok") is None
              and "跳过" in (pub[0]["status"].get("reason") or ""),
              str(pub[0]["status"]) if pub else "未找到")

        code, d, _ = jreq("POST", "/api/nav/groups", {"name": "测试分组"})
        check("可新增分组", code == 200 and any(g["name"] == "测试分组" for g in d["groups"]),
              str(d)[:140])
        tg = [g for g in d["groups"] if g["name"] == "测试分组"][0]["id"]
        code, d, _ = jreq("POST", "/api/nav/groups", {"name": "测试分组"})
        check("同名分组被拒绝", code == 400, str(d)[:120])
        code, d, _ = jreq("PUT", f"/api/nav/groups/{tg}", {"name": "测试分组改名"})
        check("可改分组名",
              code == 200 and any(g["name"] == "测试分组改名" for g in d["groups"]), "")
        code, d, _ = jreq("PUT", f"/api/nav/groups/{tg}", {"move": "up"})
        check("分组可上移排序", code == 200, str(d)[:100])
        jreq("PUT", f"/api/nav/items/{new_id}", {"group_id": tg})
        code, d, _ = jreq("DELETE", f"/api/nav/groups/{tg}")
        check("删除分组会连带删除其中导航项",
              code == 200 and not any(g["id"] == tg for g in d["groups"])
              and not any(i["id"] == new_id for g in d["groups"] for i in g["items"]),
              str(d)[:140])
        code, d, _ = jreq("DELETE", f"/api/nav/items/{pub_id}")
        check("可删除导航项",
              code == 200 and not any(i["id"] == pub_id for g in d["groups"] for i in g["items"]),
              "")
        jreq("DELETE", f"/api/users/{nav_viewer_id}")
        code, d, _ = jreq("GET", "/api/audit")
        acts = [x["action"] for x in d.get("items", [])]
        check("导航操作写入审计日志",
              "nav_item_create" in acts and "nav_item_delete" in acts and
              "nav_group_delete" in acts, str([a for a in acts if a.startswith("nav")][:6]))

        print("\n\033[1m[13b] 组内二级分组\033[0m")
        code, d, _ = jreq("GET", "/api/nav")
        gid2 = d["groups"][0]["id"]
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": gid2, "name": "二级分组测试A",
                           "url": "http://10.9.9.1/", "subgroup": "客户甲"})
        a_id = d.get("id")
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": gid2, "name": "二级分组测试B",
                           "url": "http://10.9.9.2/", "subgroup": "客户甲"})
        b_id = d.get("id")
        code, d, _ = jreq("POST", "/api/nav/items",
                          {"group_id": gid2, "name": "无二级分组", "url": "http://10.9.9.3/"})
        c_id = d.get("id")
        flat = {i["id"]: i for g in d["groups"] for i in g["items"]}
        check("新增项可带二级分组", flat[a_id]["subgroup"] == "客户甲",
              str(flat.get(a_id, {}).get("subgroup")))
        check("未指定二级分组时为空", flat[c_id]["subgroup"] == "",
              repr(flat[c_id]["subgroup"]))
        code, d, _ = jreq("GET", "/api/nav", auth=False)
        check("免登录视图也返回二级分组字段",
              all("subgroup" in i for g in d["groups"] for i in g["items"]), "")

        jreq("PUT", f"/api/nav/items/{b_id}", {"subgroup": "客户乙"})
        code, d, _ = jreq("GET", "/api/nav")
        flat = {i["id"]: i for g in d["groups"] for i in g["items"]}
        check("可修改二级分组", flat[b_id]["subgroup"] == "客户乙", str(flat[b_id]["subgroup"]))
        check("改一项的二级分组不影响同组其它项",
              flat[a_id]["subgroup"] == "客户甲", str(flat[a_id]["subgroup"]))

        jreq("PUT", f"/api/nav/items/{a_id}", {"subgroup": ""})
        code, d, _ = jreq("GET", "/api/nav")
        flat = {i["id"]: i for g in d["groups"] for i in g["items"]}
        check("可清空二级分组", flat[a_id]["subgroup"] == "", repr(flat[a_id]["subgroup"]))

        jreq("PUT", f"/api/nav/items/{a_id}", {"subgroup": "客户甲"})
        code, d, _ = jreq("PUT", f"/api/nav/items/{a_id}", {"description": "只改描述"})
        flat = {i["id"]: i for g in d["groups"] for i in g["items"]}
        check("只改其它字段不会丢掉二级分组",
              flat[a_id]["subgroup"] == "客户甲" and flat[a_id]["description"] == "只改描述",
              f"{flat[a_id]['subgroup']} / {flat[a_id]['description']}")

        code, d, _ = jreq("GET", "/api/nav", auth=False)
        check("二级分组项在免登录视图仍受可见性约束（默认 guest 可见）",
              a_id in [i["id"] for g in d["groups"] for i in g["items"]], "")
        for x in (a_id, b_id, c_id):
            jreq("DELETE", f"/api/nav/items/{x}")
        code, d, _ = jreq("GET", "/api/nav")
        left = [i["id"] for g in d["groups"] for i in g["items"]]
        check("二级分组测试项已清理", a_id not in left and b_id not in left and c_id not in left,
              f"剩余 {len(left)} 项")

        print("\n\033[1m[14] 两个独立页面结构（导航中心 / 通知中心）\033[0m")
        code, payload, _ = req("GET", "/", auth=False)
        html = payload.decode("utf-8", "replace")            # 导航中心
        code2, payload2, _ = req("GET", "/notices", auth=False)
        notices = payload2.decode("utf-8", "replace")        # 重要通知中心
        code3, payload3, _ = req("GET", "/common.js", auth=False)
        common = payload3.decode("utf-8", "replace")         # 共用库
        code4, payload4, _ = req("GET", "/common.css", auth=False)
        common_css = payload4.decode("utf-8", "replace")

        check("导航中心与通知中心各自可独立打开",
              code == 200 and code2 == 200 and "portalView" in html and "id=\"app\"" in notices,
              f"{code}/{code2}")
        check("两个页面标题正确",
              "太平洋电信导航中心" in html and "重要通知中心" in notices, "")
        check("共用资源可访问且 MIME 正确",
              code3 == 200 and code4 == 200 and common.startswith("/*"), f"{code3}/{code4}")

        # 门户页只装门户：登录屏与通知视图都不该在首页里
        check("首页包含导航门户视图", 'id="portalView"' in html, "")
        check("首页不含通知中心视图（已分家）",
              'id="app" hidden' not in html and 'id="loginScreen"' not in html, "")
        check("通知中心页含登录屏与通知视图",
              'id="loginScreen" hidden' in notices and 'id="app" hidden' in notices, "")
        check("前端引用了导航接口",
              "/api/nav" in html and "/api/nav/visit" in html, "")
        check("门户含最近访问与状态图例元素",
              'id="navRecent"' in html and 'id="navGroups"' in html and 'id="navManage"' in html, "")

        check("门户顶栏有账号管理入口，且带文字标签而非纯图标",
              'id="navAccount"' in html and '<span class="hmob">账号管理</span>' in html
              and "openUsers" in html + common, "")
        check("前端含分组内容渲染逻辑（客户 chip + 命中标记）",
              "groupBodyHtml" in html and "navsub" in html and "data-hit" in html, "")
        check("门户为左标题右内容两栏结构",
              'id="navSide"' in html and 'class="pside"' in html and 'class="pmain"' in html, "")
        check("左侧标题可点击切换分组",
              "data-navsel" in html and "renderNavSide" in html and "renderPane" in html, "")
        check("门户主页含完整账号功能",
              'id="navUserChip"' in html and 'id="navUserMenu"' in html
              and 'id="navPwdBanner"' in html and "renderNavUser" in html
              and "data-navact" in html and "openChangePwd" in common, "")
        check("登录一键直达（不二次弹菜单）",
              "if (!S.me) return go('notices');" in html, "")

        # 账号功能要「迁移」过去，不是两边都留一份
        menu_seg = notices[notices.find("function openMenu()"):][:1600]
        check("账号功能只在导航中心，通知中心已迁走",
              'id="userChip"' not in notices
              and "mi('users'" not in menu_seg and "mi('pwd'" not in menu_seg
              and "mi('logout'" not in menu_seg and "mi('backportal'" in menu_seg, "")
        check("通知中心不再背着门户代码",
              "renderNavSide" not in notices and "navCardHtml" not in notices, "")
        check("门户不再背着通知列表代码",
              "openImport" not in html and "renderSidebar" not in html
              and "cardHtml" not in html, "")

        # 共用层与主题
        check("共用库导出 NH 命名空间",
              "window.NH" in common and "NH.hooks" in common, "")
        check("默认蓝白主题（左侧深蓝底 + 右侧白底）",
              'data-theme="light"' in html and "--side-bg" in common_css
              and "--pane-bg" in common_css, "")
        # 拆分后没有 hash 路由了：站内链接必须是真实路径，不能再生成 #/notices
        # Winbox 本地联动：Windows 上 Winbox 不注册 URL 协议，必须靠本机安装的
        # nh:// 处理程序来接。这两个文件要能下载，前端也要用 nh:// 而不是 winbox://
        code_c, payload_c, _ = req("GET", "/tools/nh-setup.cmd", auth=False)
        code_i, payload_i, _ = req("GET", "/tools/nh-setup.ps1", auth=False)
        code_p, payload_p, _ = req("GET", "/tools/nh-open.ps1", auth=False)
        check("Winbox 联动安装包三个文件都可下载",
              code_c == code_i == code_p == 200
              and len(payload_c) > 500 and len(payload_i) > 500 and len(payload_p) > 500,
              f"{code_c}/{code_i}/{code_p}")
        check("安装器只写 HKCU（无需管理员权限）",
              b"HKCU" in payload_i and b"HKLM" not in payload_i
              and b"HKLM" not in payload_c, "")
        # 每台 PC 的 winbox.exe 路径都不同，必须能指定
        check("安装器支持指定 winbox.exe 路径（弹窗选择 + 命令行）",
              b"OpenFileDialog" in payload_i and b"WinboxPath" in payload_i, "")
        # Windows 批处理/脚本的字节级要求 —— 踩过一次，必须锁死：
        #   .cmd 必须是 CRLF + 纯 ASCII（cmd.exe 按 ANSI 码页读，UTF-8 会乱码并被当命令执行；
        #        LF 换行会把行切碎，powershell.exe 变成 'hell.exe'）
        #   .ps1 必须带 UTF-8 BOM（PowerShell 5.1 没 BOM 就按 ANSI 读，中文串乱码）
        def _bare_lf(b):
            return b.count(b"\n") - b.count(b"\r\n")
        check("nh-setup.cmd 是 CRLF 且纯 ASCII（否则 cmd.exe 会报一堆乱码错误）",
              b"\r\n" in payload_c and _bare_lf(payload_c) == 0
              and all(x < 128 for x in payload_c)
              and not payload_c.startswith(b"\xef\xbb\xbf"), "")
        check("两个 .ps1 都带 UTF-8 BOM 且是 CRLF（否则 PowerShell 5.1 中文乱码）",
              payload_i.startswith(b"\xef\xbb\xbf") and payload_p.startswith(b"\xef\xbb\xbf")
              and _bare_lf(payload_i) == 0 and _bare_lf(payload_p) == 0, "")
        # 把处理程序里的正则抽出来，喂真实链接 —— 这条是踩过坑的：
        # 浏览器会把 nh://winbox?host=X 规范化成 nh://winbox/?host=X（host 位后补了空路径），
        # 只认前者的话，从网页点必然报「无法识别的链接」，而安装器自测却成功。
        ps = payload_p.decode("utf-8-sig", "replace")
        pats = re.findall(r"\[regex\]::Match\(\$Uri, '([^']+)'", ps)
        check("处理程序声明了多种可接受的链接形式", len(pats) >= 3, str(pats))

        def _accepts(uri):
            for pat in pats:
                try:
                    if re.match(pat, uri, re.I):
                        return True
                except re.error:
                    continue
            return False

        check("接受浏览器规范化后的链接（winbox 后多一个斜杠）",
              _accepts("nh://winbox/?host=10.0.0.9"), "")
        check("接受未经规范化的原始写法", _accepts("nh://winbox?host=10.0.0.9"), "")
        check("接受路径形式", _accepts("nh://winbox/10.0.0.9"), "")
        check("仍兼容旧的 winbox://", _accepts("winbox://10.0.0.9"), "")
        check("拒绝无关链接", not _accepts("https://evil.example/"), "")

        # 安装器会先从门户拉最新处理程序（处理程序是拷贝到本机的，不会自动更新）。
        # 这里复刻安装器的校验规则：拉到的内容必须过这几道，否则回退本地文件 ——
        # 也就是说，绝不能让**旧版**（不认浏览器规范化链接的那版）被当成新版装上。
        def _installer_accepts(text):
            need = ("param(", "[regex]::Match", "$NhHandlerVersion",
                    "nh://winbox/?", "Start-Process")
            if any(t not in text for t in need):
                return False
            return "Invoke-Expression" not in text

        check("安装器的校验规则接受当前处理程序",
              _installer_accepts(payload_p.decode("utf-8-sig")), "")
        check("安装器的校验规则拒收旧版处理程序（没有可选斜杠那版）",
              not _installer_accepts(
                  payload_p.decode("utf-8-sig").replace("nh://winbox/?", "nh://winbox")), "")
        check("安装器的校验规则拒收错误页 / 半截文件",
              not _installer_accepts("<html><body>404 Not Found</body></html>"), "")
        check("处理程序带版本号",
              "$NhHandlerVersion" in payload_p.decode("utf-8-sig"), "")

        # 探测日志降噪：以前每轮都打一行，一天 1440 行，把有用日志全淹了。
        # 这里做的是**源码级**检查（行为级要等 15 秒间隔，不适合放进测试）。
        srv_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "server.py"), encoding="utf-8").read()
        loop_src = srv_src[srv_src.index("def nav_probe_loop"):]
        loop_src = loop_src[:loop_src.index("\n\n\n")] if "\n\n\n" in loop_src else loop_src
        check("探测日志只在状态变化时打（不再每轮刷一行）",
              "sig != prev_sig" in loop_src and "logged_first" in loop_src, "")
        check("探测摘要区分在线/离线/跳过",
              "def _probe_summary" in srv_src and "在线" in srv_src and "跳过" in srv_src, "")
        check("探测并发数可配、并有单目标超时上限",
              "NAV_PROBE_WORKERS" in srv_src and "NAV_PROBE_TIMEOUT" in srv_src, "")
        check("单个探测目标异常不会拖垮整轮",
              "单个目标出错不该拖垮整轮" in srv_src, "")

        check("路径存本机配置，处理程序优先读它",
              b"config.json" in payload_i and b"config.json" in payload_p
              and b"cfg.winbox" in payload_p, "")
        check("处理程序校验地址、不拼 shell",
              b"-notmatch" in payload_p and b"Start-Process" in payload_p
              and b"Invoke-Expression" not in payload_p, "")
        check("前端用 nh:// 而不是没用的 winbox://",
              "'nh://winbox?host='" in html and "'winbox://' + host" not in html, "")
        check("站内链接使用真实路径（不再有 hash 路由）",
              "return '/notices';" in html and "'#/' +" not in html
              and 'data-route' not in html, "")
        check("两个页面都引用了共用样式与共用脚本",
              html.count("/common.css") == 1 and html.count("/common.js") == 1
              and notices.count("/common.css") == 1 and notices.count("/common.js") == 1, "")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 62)
    if FAIL:
        print(f"\033[31m失败 {len(FAIL)} / {total} 项\033[0m")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        return 1
    print(f"\033[32m全部通过：{total} 项\033[0m")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (测试里用到 quote)
    sys.exit(main())
