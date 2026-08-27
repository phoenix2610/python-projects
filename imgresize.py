#!/usr/bin/env python3
"""Resize a folder of PNGs in parallel — decoder, resampler and encoder written here.

    imgresize.py ~/screenshots --width 800 --out ~/small
    imgresize.py ~/photos --max 1200 --filter box --workers 8

A PNG is a header plus zlib-compressed scanlines, each prefixed with a filter
byte that predicts pixels from the ones left of and above them. This decodes all
five filter types, resamples, then re-encodes with a filter choice per scanline
(minimum sum of absolute differences, the heuristic libpng uses).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def decode(data: bytes) -> tuple[int, int, int, bytearray]:
    """Return (width, height, channels, RGBA-ish pixel bytes) for a non-interlaced PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width = 8, bytearray(), 0
    height = depth = color = interlace = 0
    palette = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError(f"only 8-bit non-interlaced PNGs supported (got depth {depth})")

    channels = CHANNELS[color]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filter_type == 4:
                line[i] = (line[i] + paeth(a, b, c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line

    if color == 3:  # expand the palette so resampling averages real colours
        rgb = bytearray(width * height * 3)
        for i, idx in enumerate(out):
            rgb[i * 3:i * 3 + 3] = palette[idx * 3:idx * 3 + 3]
        return width, height, 3, rgb
    return width, height, channels, out


def resample(src: bytearray, sw: int, sh: int, ch: int, dw: int, dh: int, mode: str) -> bytearray:
    dst = bytearray(dw * dh * ch)
    x_ratio, y_ratio = sw / dw, sh / dh
    if mode == "box":  # average the source pixels each destination pixel covers
        for dy in range(dh):
            y0, y1 = int(dy * y_ratio), max(int((dy + 1) * y_ratio), int(dy * y_ratio) + 1)
            for dx in range(dw):
                x0, x1 = int(dx * x_ratio), max(int((dx + 1) * x_ratio), int(dx * x_ratio) + 1)
                n = (y1 - y0) * (x1 - x0)
                base = (dy * dw + dx) * ch
                for c in range(ch):
                    total = 0
                    for sy in range(y0, y1):
                        row = sy * sw * ch
                        for sx in range(x0, x1):
                            total += src[row + sx * ch + c]
                    dst[base + c] = total // n
    else:  # nearest
        for dy in range(dh):
            sy = min(int(dy * y_ratio), sh - 1)
            for dx in range(dw):
                sx = min(int(dx * x_ratio), sw - 1)
                s = (sy * sw + sx) * ch
                d = (dy * dw + dx) * ch
                dst[d:d + ch] = src[s:s + ch]
    return dst


def encode(pixels: bytearray, width: int, height: int, ch: int, level: int) -> bytes:
    color = {1: 0, 2: 4, 3: 2, 4: 6}[ch]
    stride = width * ch
    raw = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        line = pixels[y * stride:(y + 1) * stride]
        candidates = []
        for ftype in (0, 1, 2):
            if ftype == 0:
                enc = bytes(line)
            elif ftype == 1:
                enc = bytes((line[i] - (line[i - ch] if i >= ch else 0)) & 0xFF for i in range(stride))
            else:
                enc = bytes((line[i] - prev[i]) & 0xFF for i in range(stride))
            # minimum sum of absolute differences: cheap proxy for what compresses best
            candidates.append((sum(b if b < 128 else 256 - b for b in enc), ftype, enc))
        _, ftype, enc = min(candidates)
        raw.append(ftype)
        raw += enc
        prev = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), level)) + chunk(b"IEND", b""))


def process(job: tuple) -> str:
    path, out_path, target_w, target_h, max_side, mode, level = job
    data = open(path, "rb").read()
    try:
        sw, sh, ch, pixels = decode(data)
    except ValueError as exc:
        return f"  skip   {os.path.basename(path)} ({exc})"
    if max_side:
        scale = min(1.0, max_side / max(sw, sh))
        dw, dh = max(1, round(sw * scale)), max(1, round(sh * scale))
    elif target_w and target_h:
        dw, dh = target_w, target_h
    elif target_w:
        dw, dh = target_w, max(1, round(sh * target_w / sw))
    else:
        dh, dw = target_h, max(1, round(sw * target_h / sh))
    out = encode(resample(pixels, sw, sh, ch, dw, dh, mode), dw, dh, ch, level)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "wb").write(out)
    saved = (1 - len(out) / len(data)) * 100
    return (f"  ok     {os.path.basename(path)}  {sw}x{sh} -> {dw}x{dh}  "
            f"{len(data) // 1024}KB -> {len(out) // 1024}KB ({saved:+.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--out", default=None, help="output folder (default: <folder>/resized)")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--max", type=int, default=0, help="longest side, preserving aspect")
    ap.add_argument("--filter", choices=["box", "nearest"], default="box")
    ap.add_argument("--level", type=int, default=9, help="zlib compression level")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    if not (args.width or args.height or args.max):
        ap.error("pass --width, --height or --max")
    out_dir = args.out or os.path.join(args.folder, "resized")
    jobs = []
    for name in sorted(os.listdir(args.folder)):
        if name.lower().endswith(".png") and os.path.isfile(os.path.join(args.folder, name)):
            jobs.append((os.path.join(args.folder, name), os.path.join(out_dir, name),
                         args.width, args.height, args.max, args.filter, args.level))
    if not jobs:
        print("no PNGs found", file=sys.stderr)
        return 1

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for line in pool.map(process, jobs):
            print(line)
    print(f"\n{len(jobs)} images -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
