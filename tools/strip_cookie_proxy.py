#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时验证工具：本地反向代理，转发到 8901，但**剥掉 Cookie 请求头与所有 Set-Cookie 响应头**。

用途：在真实浏览器里重现「浏览器上下文不保存 cookie」的环境
（内嵌 webview / 预览面板 / 被代理剥离 cookie），用来验证令牌兜底通道是否真的生效。

    python tools/strip_cookie_proxy.py          # 监听 127.0.0.1:8902 -> 127.0.0.1:8901
"""

from __future__ import annotations

import http.server
import sys
import urllib.error
import urllib.request

UPSTREAM = "http://127.0.0.1:8901"
PORT = 8902

SKIP_REQ = {"cookie", "host", "connection", "content-length", "accept-encoding"}
SKIP_RESP = {"set-cookie", "content-length", "connection", "transfer-encoding",
             "date", "server"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 302/303，把跳转原样交给浏览器（否则测不到登录后的 303）。"""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class StripCookieProxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _proxy(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in SKIP_REQ
        }
        req = urllib.request.Request(UPSTREAM + self.path, data=body,
                                     method=method, headers=headers)
        try:
            resp = _OPENER.open(req, timeout=30)
            code, payload, resp_headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            code, payload, resp_headers = e.code, e.read(), e.headers
        except Exception as e:  # noqa: BLE001
            payload = f"proxy error: {e}".encode()
            code, resp_headers = 502, {}

        self.send_response(code)
        for k, v in (resp_headers.items() if resp_headers else []):
            if k.lower() in SKIP_RESP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_HEAD(self):
        self._proxy("HEAD")


def main() -> None:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), StripCookieProxy)
    srv.daemon_threads = True
    print(f"[strip-cookie-proxy] 127.0.0.1:{PORT} -> {UPSTREAM}  "
          f"（Cookie 请求头与 Set-Cookie 响应头都会被剥掉）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
