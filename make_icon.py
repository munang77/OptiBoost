# -*- coding: utf-8 -*-
"""외부 라이브러리 없이 순수 파이썬으로 앱 아이콘(icon.ico) 생성."""
import os
import zlib
import struct

W = H = 256
TOP = (124, 92, 255)     # 보라
BOT = (0, 200, 150)      # 청록
R = 54                    # 라운드 코너 반지름


def lerp(a, b, t):
    return int(round(a + (b - a) * t))


def inside_round(x, y):
    rx = min(max(x, R), W - 1 - R)
    ry = min(max(y, R), H - 1 - R)
    dx, dy = x - rx, y - ry
    return dx * dx + dy * dy <= R * R


# 번개 모양 다각형
BOLT = [(150, 34), (92, 150), (130, 150), (104, 224),
        (182, 104), (140, 104), (170, 34)]


def in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def build_png():
    raw = bytearray()
    for y in range(H):
        raw.append(0)  # filter type 0
        t = y / (H - 1)
        base = (lerp(TOP[0], BOT[0], t), lerp(TOP[1], BOT[1], t),
                lerp(TOP[2], BOT[2], t))
        for x in range(W):
            if not inside_round(x, y):
                raw += b"\x00\x00\x00\x00"
            elif in_poly(x, y, BOLT):
                raw += b"\xff\xff\xff\xff"
            else:
                raw += bytes(base) + b"\xff"

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        return c

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    return png


def build_ico(png):
    # ICO 헤더 + 디렉터리 1개 + PNG(256은 width/height 바이트=0)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    png = build_png()
    with open(out, "wb") as f:
        f.write(build_ico(png))
    # 미리보기용 PNG도 저장
    with open(out.replace(".ico", ".png"), "wb") as f:
        f.write(png)
    print("saved:", out, "(", len(png), "bytes png )")
