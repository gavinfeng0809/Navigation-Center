#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 mock：假的「企微智能机器人长连接服务端」
================================================

用途：在拿到真实 BotID/Secret 之前，把采集器整条链路先验证通——
      订阅 → 收消息回调 → 解析 → 入库 notice-hub → 群内回执。

它按官方文档的帧格式跟采集器对话：
    收到  {"cmd":"aibot_subscribe", headers:{req_id}, body:{bot_id,secret}}
    回执  {"headers":{req_id}, "errcode":0, "errmsg":"ok"}
    推送  {"cmd":"aibot_msg_callback", headers:{req_id}, body:{msgid,chatid,...}}
    收到  {"cmd":"aibot_respond_msg", headers:{req_id}, body:{...}}   ← 采集器的回执
    收到  {"cmd":"ping", ...}  →  回 ack

用法：
    ./venv/bin/python mock_ws_server.py                      # 监听 127.0.0.1:8765
    ./venv/bin/python mock_ws_server.py --scenario notice    # 推一条【重要通知】
    ./venv/bin/python mock_ws_server.py --scenario mixed     # 推一条图文混排
    ./venv/bin/python mock_ws_server.py --scenario quote     # 推一条「引用通知 + @机器人」
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from websockets.asyncio.server import serve

RECEIVED = []


def frame(cmd: str, body: dict) -> str:
    return json.dumps({
        "cmd": cmd,
        "headers": {"req_id": f"mock-{cmd}-{int(time.time() * 1000)}"},
        "body": body,
    }, ensure_ascii=False)


def build_scenarios() -> dict:
    notice_text = (
        "@通知归档助手\n"
        "【重要通知】\n"
        "华东区新铂金客户 - 星海科技（XHKE）线路割接通知\n"
        "\n"
        "客户：星海科技（XHKE）\n"
        "线路：WALT-A0231 / WALT-A0232\n"
        "窗口：2026-10-12 01:00 - 05:00\n"
        "影响：割接期间双线各中断约 15 分钟，业务侧已确认可接受\n"
        "动作：请值班同事在窗口前 30 分钟完成告警屏蔽，结束后恢复\n"
        "\n"
        "发布人：Alex Chen"
    )

    def body(msgid: str, msgtype: str, **extra) -> dict:
        b = {
            "msgid": msgid,
            "aibotid": "MOCKBOTID",
            "chatid": "MOCKCHATID001",
            "chattype": "group",
            "from": {"userid": "alex.chen"},
            "msgtype": msgtype,
        }
        b.update(extra)
        return b

    return {
        "notice": body("mock-msgid-notice-0001", "text", text={"content": notice_text}),
        "mixed": body(
            "mock-msgid-mixed-0002", "mixed",
            mixed={"msg_item": [
                {"msgtype": "text", "text": {"content": "@通知归档助手 【重要通知】\n\r\n割接后现场照片如下，线路已恢复正常。"}},
                {"msgtype": "image", "image": {"url": "http://127.0.0.1:9/not-a-real-image.jpg",
                                               "aeskey": "deadbeef" * 8}},
            ]},
        ),
        "quote": body(
            "mock-msgid-quote-0003", "text",
            text={"content": "@通知归档助手 这条补归档一下"},
            quote={"msgtype": "text", "text": {"content": "【重要通知】钻石客户央视国际YSGJ 新增SLA级监控通知：自 2026-10-15 起对 IPVPN 链路启用 85% 流量阈值告警。"}},
        ),
        # 提问：同事会把机器人当搜索用（上线第一天就真发生了）。
        # 这条**不该**被归档 —— 否则通知中心里会多出一条「帮我查询…」的垃圾。
        "query": body(
            "mock-msgid-query-0005", "text",
            text={"content": "@通知归档助手 帮我查询以下近期的重要通知内容"},
        ),
        # 语音：企微在服务端做完识别，只下发**文字转写**，没有音频也没有 aeskey。
        # 采集器必须把 voice.content 当正文，否则「语音 @机器人」会被当空消息丢掉。
        "voice": body(
            "mock-msgid-voice-0004", "voice",
            voice={"content": "【重要通知】语音转写：钻石客户星海科技线路割接窗口改为今晚十一点开始，请值班同事提前屏蔽告警。"},
        ),
    }


async def handler(websocket):
    peer = getattr(websocket, "remote_address", "?")
    print(f"[mock] 连接建立 {peer}", flush=True)
    scenarios = build_scenarios()
    pushed = False

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                print(f"[mock] 收到非 JSON：{raw[:200]}", flush=True)
                continue

            cmd = msg.get("cmd", "")
            req_id = (msg.get("headers") or {}).get("req_id", "")

            if cmd == "aibot_subscribe":
                b = msg.get("body", {})
                print(f"[mock] ← aibot_subscribe bot_id={b.get('bot_id')} "
                      f"secret={'*' * len(b.get('secret') or '')}", flush=True)
                await websocket.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
                if not pushed:
                    pushed = True
                    await asyncio.sleep(0.5)
                    for name, bd in scenarios.items():
                        print(f"[mock] → 推送回调场景 {name}（msgid={bd['msgid']}）", flush=True)
                        await websocket.send(frame("aibot_msg_callback", bd))
                        await asyncio.sleep(1.5)

            elif cmd == "ping":
                await websocket.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))

            elif cmd == "aibot_respond_msg":
                b = msg.get("body", {})
                RECEIVED.append(b)
                mt = b.get("msgtype")
                print(f"[mock] ← 采集器回执 req_id={req_id} msgtype={mt}", flush=True)

                # 复刻企微对被动回复的类型校验。
                # 被动回复（aibot_respond_msg）只认 stream / text / template_card / 媒体；
                # markdown 是**主动发送**（aibot_send_msg）专用的，用在这里会被拒收。
                # 以前 mock 什么都不校验，于是「回执用了 markdown」这个真 bug
                # 在联调阶段完全看不出来 —— 现在它会在这里直接失败。
                if mt not in ("stream", "text", "template_card",
                              "image", "file", "voice", "video"):
                    print(f"[mock] ✗ 拒绝被动回复 msgtype={mt}"
                          f"（企微不接受这个类型，回执不会出现在群里）", flush=True)
                    await websocket.send(json.dumps(
                        {"headers": {"req_id": req_id}, "errcode": 40008,
                         "errmsg": f"unsupported msgtype for aibot_respond_msg: {mt}"}))
                    continue

                if mt == "stream":
                    st = b.get("stream") or {}
                    if not st.get("id"):
                        print("[mock] ✗ stream 帧缺少 id", flush=True)
                    if st.get("finish") and not (st.get("content") or "").strip():
                        print("[mock] ✗ 封口帧内容不可见（会被忽略，群里留一个转圈的空泡）",
                              flush=True)

                content = ((b.get("stream") or {}).get("content")
                           or (b.get("text") or {}).get("content")
                           or (b.get("markdown") or {}).get("content") or "")
                print(f"[mock]    内容：{content!r}", flush=True)
                if len(content.encode("utf-8")) > 20480:
                    print(f"[mock] ✗ stream.content 超过 20480 字节上限", flush=True)
                await websocket.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))

            else:
                print(f"[mock] ← 未处理的帧 cmd={cmd}", flush=True)
                if req_id:
                    await websocket.send(json.dumps(
                        {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))

    except Exception as e:
        print(f"[mock] 连接结束：{type(e).__name__}: {e}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario", default="all",
                    choices=["all", "notice", "mixed", "quote", "voice", "query"],
                    help="只推指定场景（默认 all）")
    args = ap.parse_args()

    print(f"[mock] 假的企微长连接服务端已启动：ws://{args.host}:{args.port}", flush=True)
    print("[mock] 采集器这样连它：", flush=True)
    print(f"[mock]   ./venv/bin/python wecom_bot.py --ws-url ws://{args.host}:{args.port} "
          f"--bot-id testbot --bot-secret testsecret", flush=True)
    async with serve(handler, args.host, args.port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[mock] 退出")
