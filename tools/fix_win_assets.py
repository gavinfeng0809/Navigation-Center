#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正三个 Windows 安装文件的字节级问题：

1. 换行符必须是 CRLF
   cmd.exe 用 LF 会把行切碎（powershell.exe 会变成 hell.exe 这种碎片报错）。
2. .cmd 必须是**纯 ASCII**
   cmd.exe 按系统 ANSI 码页读批处理，UTF-8 中文会变乱码并被当成命令执行。
3. .ps1 必须带 UTF-8 BOM
   Windows PowerShell 5.1 没有 BOM 时按 ANSI 读，中文串会乱码。

    python3 tools/fix_win_assets.py            # 检查
    python3 tools/fix_win_assets.py --write    # 修正
"""

from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "public", "tools")

CMD = "nh-setup.cmd"
PS1S = ("nh-setup.ps1", "nh-open.ps1")


def report(name: str, raw: bytes) -> list:
    problems = []
    if b"\r\n" not in raw:
        problems.append("没有 CRLF 换行（cmd.exe 会把行切碎）")
    bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
    if bare_lf:
        problems.append(f"存在裸 LF（{bare_lf} 处）")
    if name.endswith(".cmd"):
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as e:
            problems.append(f"含非 ASCII 字节（偏移 {e.start}）：cmd 会按 ANSI 读成乱码")
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append("带 UTF-8 BOM（cmd.exe 会把 BOM 当成命令的一部分）")
        if b"chcp" in raw:
            problems.append("有 chcp（改码页对已读入的批处理行不可靠，建议去掉）")
    else:
        if not raw.startswith(b"\xef\xbb\xbf"):
            problems.append("没有 UTF-8 BOM（PowerShell 5.1 会按 ANSI 读，中文乱码）")
    return problems


def main() -> int:
    write = "--write" in sys.argv
    bad = 0
    for name in (CMD,) + PS1S:
        p = os.path.join(TOOLS, name)
        raw = open(p, "rb").read()
        probs = report(name, raw)
        status = "✓ 正常" if not probs else "✗ " + "；".join(probs)
        print(f"  {name:<16} {len(raw):>6}B  {status}")
        if probs:
            bad += 1
        if write:
            text = raw.decode("utf-8-sig")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if name.endswith(".cmd"):
                # .cmd 去中文：注释与提示一律英文 ASCII
                text = text.replace("chcp 65001 >nul\n", "")
                text = "".join(ch if ord(ch) < 128 else "?" for ch in text)
            out = text.replace("\n", "\r\n").encode("utf-8")
            if not name.endswith(".cmd"):
                out = b"\xef\xbb\xbf" + out      # 加 UTF-8 BOM
            open(p, "wb").write(out)
    if write:
        print("\n已按 CRLF / ASCII(cmd) / UTF-8-BOM(ps1) 重写。")
        print("重新检查：")
        for name in (CMD,) + PS1S:
            raw = open(os.path.join(TOOLS, name), "rb").read()
            probs = report(name, raw)
            print(f"  {name:<16} {len(raw):>6}B  " + ("✓ 正常" if not probs else "✗ " + "；".join(probs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
