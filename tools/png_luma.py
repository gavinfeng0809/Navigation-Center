#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 PNG 亮度采样器（标准库 zlib），用于核验截图是深色还是浅色主题。"""
import glob
import os
import struct
import sys
import zlib


def decode_rows(path, max_rows):
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not png")
    pos, idat = 8, b""
    w = h = bd = ct = None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct, _, _, inter = struct.unpack(">IIBBBBB", chunk)
            if bd != 8 or inter != 0:
                raise ValueError(f"unsupported bd={bd} interlace={inter}")
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(idat)
    stride = w * ch
    rows = min(h, max_rows)
    prev = bytearray(stride)
    p = 0
    lines = []
    for _ in range(rows):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        lines.append(bytes(line))
        prev = line
    return w, h, ch, lines


def brightness(path, max_rows=420):
    w, h, ch, lines = decode_rows(path, max_rows)
    total = n = 0
    for li, line in enumerate(lines):
        if li % 4:
            continue
        for x in range(0, w, 7):
            o = x * ch
            total += (line[o] * 299 + line[o + 1] * 587 + line[o + 2] * 114) // 1000
            n += 1
    return w, h, total / max(1, n)


if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "*.png")
    for f in sorted(glob.glob(pattern)):
        w, h, b = brightness(f)
        label = "浅色" if b > 128 else "深色"
        print(f"{os.path.basename(f):28s} {w}x{h}  平均亮度 {b:6.1f}  -> {label}")
