#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则版「一键整理重点」（不联网、不调模型）—— Python 移植
==========================================================

**来源**：从页面 `public/notices.html` 的「一键整理」一节逐行移植而来
（原实现：POLISH_HDR / POLISH_BULLET / POLISH_ACTION / POLISH_STOP /
POLISH_LEXICON / polishKeywords / isTitleJunk / pickTitleLine / polishRecord）。

为什么要移植而不是重新写：那套规则是调过参数的（词表权重、去重、行分类），
重写一遍必然对不上，页面点「一键整理」和企微自动整理出来的东西会长得不一样。
移植后有一致性对拍测试（`test_polish_parity.js`）盯着两边输出必须逐字节相同。

⚠️ 改这里的规则时，请同步改 `public/notices.html`，否则对拍测试会失败。

能做到：认头部字段 / 摘出要点行并编号 / 行动句加粗 / 顶部补「关键字」。
做不到：真正的语义归纳（那需要模型）。所以正文原样保留，只把重点标出来。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- 正则与词表

POLISH_HDR = re.compile(
    r"^\s*(来源|群名称|群|发布人|发件人|发布时间|时间|日期|分类|类型|级别|重要级别|标签|外部ID|消息ID|群/发件人)\s*[:：]\s*\S"
)

POLISH_BULLET = re.compile(
    r"^\s*(?:[-*·•—]|\(?\d{1,2}[.)、]|[①②③④⑤⑥⑦⑧⑨⑩]|第[一二三四五六七八九十]+[、.)])\s*"
)

POLISH_ACTION = re.compile(
    r"(请|需|务必|必须|立即|尽快|及时|截止|前完成|禁止|注意|告警|异常|超过|不低于|联系|反馈|确认|安排|升级|割接|回滚|中断|停机|排查)"
)

POLISH_STOP = ['重要通知', '重要', '通知', '关于', '如下', '以下', '各位', '同事', '我们', '可以',
               '进行', '相关', '后续', '目前', '已经', '问题', '情况', '大家好', '谢谢']

# 业务词表：命中就优先当关键字（比按字频瞎切准得多，也避免切出「口监控与告警」这种碎片）。
# 维护方式：往里面加词即可，越贴近你们日常的叫法越好用。
# 业务词表：**高区分度**的词，命中就优先当关键字。
# 维护方式：往里面加词即可，越贴近你们日常的叫法越好用。
POLISH_LEXICON = ['SLA', 'SASE', 'ZTNA', 'SDWAN', 'IPVPN', 'MPLS', 'VPN', 'DIA', 'BGP', 'POP', 'CPE',
                  'IPsec', 'FortiGate', 'Fortinet', 'SNOW', 'NAT', 'DNS', 'DHCP', 'QoS', 'MTU',
                  '监控', '告警', '割接', '故障', '中断', '回滚', '排查', '巡检',
                  '线路', '专线', '带宽', '流量', '延时', '丢包', '可用性', '路由', '拓扑',
                  '扩容', '迁移', '证书', '域名', '端口', '防火墙', '交换机', '路由器', '链路',
                  '隧道', '建连', '光猫', '会话表', '公网IP', '回收', '重启', '号码认证', '认证',
                  '呼叫', '热线', '审批', '权限', '配置调整', '版本升级', '变更窗口',
                  '钻石客户', '铂金客户', '订单', '验收', '交接', '工单号', '备件', '扩容窗口']

# 低区分度词：出现频率很高但说明不了「这条通知讲什么」。
# 降权而不是删除 —— 当一条通知确实只有这些词可用时，至少不至于空标签。
POLISH_GENERIC = ['客户', '供应商', '邮件', '工单', '变更', '设备', '版本', '升级', '值班',
                  '操作', '培训', '流程', '规范', '维护', '通知', '系统', '订单']

# 纯噪音：任何情况下都不该当标签（「目前」「后续」「公司」当标签毫无意义）
POLISH_NOISE = ['目前', '后续', '相关', '注意', '注意事项', '事项', '要求', '同事', '公司',
                '情况', '问题', '大家', '谢谢', '如下', '以下', '关于', '重要通知', '重要']

# 标题行不能拿这些行来充当（和服务端 is_title_junk 保持一致）
POLISH_TITLE_LABEL = re.compile(
    r"^\s*(?:【(?:重要|紧急)?通知】|(?:重要|紧急)?(?:通知|公告|提醒|说明|注意事项|温馨提示))\s*[:：]?\s*$"
)
POLISH_TITLE_GREET = re.compile(
    r"^\s*(?:Dear\b[^,，]{0,30}[,，]?|Hi\b[^,，]{0,20}[,，]?|Hello\b|各位(?:同事|领导|老师)?[好，,:：]*|大家好[，。!！:：]*|您好[，。!！:：]*|早上好|下午好)\s*$",
    re.I,
)
POLISH_TITLE_CLOSING = re.compile(
    r"^\s*(?:Best\s+Regards|Regards|Thanks|Thank\s+you|BR|Sincerely|此致|敬礼|谢谢|多谢)\s*[,，。.!！]?\s*$",
    re.I,
)
POLISH_TITLE_SIGN = re.compile(r"(This communication may contain|保密声明|版权声明|Confidential)", re.I)
POLISH_TITLE_SIGN_SHORT = re.compile(r"(@example-corp|400 700 2800)", re.I)   # 只在短行里算签名
POLISH_MAILHDR = re.compile(
    r"^\s*(?:From|Sent|To|Cc|Bcc|Subject|发件人|发送时间|收件人|抄送|主题)\s*[:：]\s*\S", re.I
)

# 英文/数字代号里的噪音词
_TOKEN_NOISE = re.compile(
    r"^(Dear|Hi|Hello|Thanks|Best|Regards|Team|Please|From|Sent|Subject|To|Cc|OK|PS)$", re.I
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,20}")
_UPPER2 = re.compile(r"[A-Z]{2,}")


# ---------------------------------------------------------------- 关键字

def polish_keywords(text: str, title: str) -> list:
    """抽出最多 5 个关键字。与 JS 版逐行对应（含 -1 排序这种怪癖，为的是输出一致）。"""
    text = text or ""
    title_line = (title or "").split("\n")[0]
    score = {}

    for w in POLISH_LEXICON:
        n = len(re.findall(re.escape(w), text, re.I))
        if not n:
            continue
        # 词越长越具体，给长度加分；出现在标题里额外加权（标题是人写的摘要）
        score[w] = n * 4 + (6 if w in title_line else 0) + 3 + min(len(w), 10) * 2

    # 泛词：同样的匹配，但权重低一个量级，避免「客户/供应商/邮件」挤掉具体词
    for w in POLISH_GENERIC:
        n = len(re.findall(re.escape(w), text, re.I))
        if not n:
            continue
        s_gen = n + (3 if w in title_line else 0)
        prev = score.get(w)
        if prev is None or s_gen > prev:
            score[w] = s_gen

    # 纯噪音词直接剔除，不参与排序
    for w in POLISH_NOISE:
        score.pop(w, None)

    # 英文/数字代号（YSGJ、HAHY、EMS7.4、85% 之类）也算关键字
    for w in _TOKEN_RE.findall(text):
        if _TOKEN_NOISE.match(w):
            continue
        score[w] = score.get(w, 0) + 1 + (2 if w in title_line else 0)

    for k in list(score.keys()):
        if score[k] <= 1 and not _UPPER2.search(k):
            del score[k]

    # 排序：分数降序，同分按在正文里出现的先后。
    # 注意 JS 用的是大小写敏感的 text.indexOf，命中不到时返回 -1；
    # 这里照搬，保证同分时的次序和页面完全一致。
    def _order(ws):
        return sorted(ws, key=lambda w: (-score[w], text.find(w)))

    # 具体词优先；泛词（客户/邮件/工单…）只在前者不足 3 个时用来补位，
    # 否则「客户 / 供应商 / 邮件」这种会永远占满 5 个位置。
    _spec = [w for w in score if w not in POLISH_GENERIC]
    _gen = [w for w in score if w in POLISH_GENERIC]
    ordered = _order(_spec)
    if len(ordered) < 3:
        ordered += _order(_gen)

    out, seen = [], []
    for w in ordered:
        if len(out) >= 5:
            break
        if any((w in s) or (s in w) for s in seen):
            continue
        seen.append(w)
        out.append(w)
    return out


# ---------------------------------------------------------------- 行分类

def is_title_junk(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return True
    if (POLISH_TITLE_LABEL.match(t) or POLISH_TITLE_GREET.match(t)
            or POLISH_TITLE_CLOSING.match(t) or POLISH_TITLE_SIGN.search(t)
            or (len(t) <= 40 and POLISH_TITLE_SIGN_SHORT.search(t))
            or POLISH_MAILHDR.match(t)):
        return True
    if re.match(r"^[\s\-–—_=*#·•.，。；;、!！?？:：()（）\[\]【】]+$", t):
        return True
    if re.match(r"^(https?://\S+|!\[[^\]]*\]\(\S+\))$", t):
        return True
    if re.match(r"^[A-Za-z][A-Za-z.\-]*(?:\s+[A-Za-z][A-Za-z.\-]*){1,3}$", t):
        return True
    if POLISH_HDR.match(t):
        return True
    return False


def pick_title_line(lines: list) -> dict:
    """从正文里挑出当标题的那一行：跳过标签/问候/落款，遇到「重要通知：」这类标签就拼进去"""
    label, label_idx, title_idx = "", -1, -1
    for i, raw in enumerate(lines):
        t = (raw or "").strip()
        if not t:
            continue
        if POLISH_TITLE_LABEL.match(t):
            if title_idx < 0:
                label = re.sub(r"[:：]\s*$", "", t)
                label_idx = i
            continue
        if is_title_junk(t):
            continue
        title_idx = i
        break
    if title_idx < 0:
        return {"title": "", "drops": []}
    sep = "" if re.search(r"[】\])）]$", label) else "："
    title = (label + sep if (label and label_idx < title_idx) else "") + lines[title_idx].strip()
    title = re.sub(r"\s+", " ", title)[:200]   # 不截短句；正文必须保留全文
    drops = [title_idx]
    if 0 <= label_idx < title_idx:
        drops.append(label_idx)
    return {"title": title, "drops": drops}


# ---------------------------------------------------------------- 主入口

def polish_record(block: str) -> str:
    """与 JS 版 polishRecord 对应：头部字段归位 → 标题 → 关键字 → 【要点】 → 【正文】

    只返回正文。要同时拿关键字和标题（写 tags / title 字段用）请用
    `polish_record_ex()` —— 关键字**必须**用正文里那一份：
    正文里的「关键字：…」是带标题参与打分算出来的，另算一遍次序可能不同，
    会和正文自相矛盾。
    """
    return polish_record_ex(block)["content"]


def polish_record_ex(block: str) -> dict:
    """返回 {"content": 整理后正文, "keywords": [...], "title": 标题行}

    content 与 JS 版 polishRecord 逐字节一致（有 test_polish_parity.js 盯着）。
    keywords 就是 content 里「关键字：」那一行所用的列表，供 tags 字段复用。
    """
    lines = (block or "").split("\n")

    # 头部区 = 第一个空行之前；整篇没有空行就看前 12 行。
    blank_idx = -1
    for i, ln in enumerate(lines):
        if not ln.strip():
            blank_idx = i
            break
    head_end = blank_idx if blank_idx >= 0 else min(len(lines), 12)

    head, rest = [], []
    for i, ln in enumerate(lines):
        t = ln.strip()
        if i < head_end and t and POLISH_HDR.match(ln):
            head.append(t)
            continue
        rest.append(ln)

    picked = pick_title_line(rest)
    title_line = picked["title"]
    if picked["drops"]:
        rest = [ln for i, ln in enumerate(rest) if i not in picked["drops"]]

    bullets, body = [], []
    for ln in rest:
        t = ln.strip()
        if not t:
            body.append(ln)
            continue
        if POLISH_BULLET.match(t):
            bullets.append(POLISH_BULLET.sub("", t, count=1))
            continue
        if len(t) <= 70 and POLISH_ACTION.search(t) and not re.search(r"[。；;]$", t):
            bullets.append(t)
            continue
        body.append(ln)

    out = list(head)
    if title_line:
        if out:
            out.append("")
        out.append(title_line)

    kws = polish_keywords(block, title_line)
    if kws:
        out.append("")
        out.append("关键字：" + " / ".join(kws))

    if bullets:
        out.append("")
        out.append("【要点】")
        for i, b in enumerate(bullets, 1):
            out.append(f"{i}. {b}")

    if "".join(body).strip():
        out.append("")
        out.append("【正文】")
        started = False
        for ln in body:
            t = ln.strip()
            if not started:
                if not t:
                    continue          # 去掉正文开头多余的空行
                started = True
            if t and len(t) <= 160 and POLISH_ACTION.search(t):
                out.append("**" + t + "**")
            else:
                out.append(ln)

    return {
        "content": "\n".join(out).strip(),
        "keywords": kws,
        "title": title_line,
    }

