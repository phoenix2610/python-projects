#!/usr/bin/env python3
"""Capture pages on a schedule and flag pixel diffs above a threshold.

    screenshot_regression.py check --baseline baselines/ --current current/
    screenshot_regression.py --demo

Works on raw PPM/PNG-decoded pixel buffers with no imaging library: two same-
sized images are compared pixel by pixel, and the interesting output isn't
just "12,000 pixels differ" but WHERE — this computes a bounding box around the
changed region and a per-region diff percentage, so "a 40x400 strip down the
left sidebar changed" reads very differently from "40,000 pixels scattered
across the whole page changed" even when both numbers are similar.
"""
from __future__ import annotations

import argparse
import struct
import zlib
from dataclasses import dataclass, field


@dataclass
class Image:
    width: int
    height: int
    pixels: bytes  # RGB, 3 bytes per pixel, row-major

    def get(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]


def decode_png(data: bytes) -> Image:
    """Minimal PNG decoder: 8-bit RGB or RGBA, non-interlaced — enough for screenshots."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    width = height = 0
    color_type = 0
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
            if depth != 8:
                raise ValueError("only 8-bit PNGs supported")
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    channels = {2: 3, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"unsupported PNG color type {color_type}")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 3)
    prev_row = bytearray(stride)
    at = 0
    for y in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev_row[i]
            c = prev_row[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        for x in range(width):
            src = x * channels
            dst = (y * width + x) * 3
            out[dst : dst + 3] = line[src : src + 3]
        prev_row = line

    return Image(width, height, bytes(out))


def encode_png(image: Image) -> bytes:
    stride = image.width * 3
    raw = bytearray()
    for y in range(image.height):
        raw.append(0)  # filter type 0 (none) — simplicity over compression ratio
        row_start = y * stride
        raw += image.pixels[row_start : row_start + stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


@dataclass
class DiffResult:
    total_pixels: int
    changed_pixels: int
    bounding_box: tuple[int, int, int, int] | None  # (min_x, min_y, max_x, max_y)
    max_channel_delta: int
    diff_image: Image | None = None

    @property
    def changed_fraction(self) -> float:
        return self.changed_pixels / self.total_pixels if self.total_pixels else 0.0


def compare_images(baseline: Image, current: Image, threshold: int = 10, render_diff: bool = False) -> DiffResult:
    if baseline.width != current.width or baseline.height != current.height:
        raise ValueError(f"size mismatch: baseline {baseline.width}x{baseline.height} vs current {current.width}x{current.height}")

    changed = 0
    min_x, min_y = baseline.width, baseline.height
    max_x, max_y = -1, -1
    max_delta = 0
    diff_pixels = bytearray(len(baseline.pixels)) if render_diff else None

    for y in range(baseline.height):
        row_offset = y * baseline.width * 3
        for x in range(baseline.width):
            i = row_offset + x * 3
            br, bg, bb = baseline.pixels[i], baseline.pixels[i + 1], baseline.pixels[i + 2]
            cr, cg, cb = current.pixels[i], current.pixels[i + 1], current.pixels[i + 2]
            delta = max(abs(br - cr), abs(bg - cg), abs(bb - cb))
            if delta > threshold:
                changed += 1
                max_delta = max(max_delta, delta)
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
                if diff_pixels is not None:
                    diff_pixels[i], diff_pixels[i + 1], diff_pixels[i + 2] = 255, 0, 0
            elif diff_pixels is not None:
                # dim the unchanged region so the highlighted diff stands out
                diff_pixels[i], diff_pixels[i + 1], diff_pixels[i + 2] = br // 3, bg // 3, bb // 3

    bbox = (min_x, min_y, max_x, max_y) if changed > 0 else None
    diff_image = Image(baseline.width, baseline.height, bytes(diff_pixels)) if diff_pixels is not None else None
    return DiffResult(baseline.width * baseline.height, changed, bbox, max_delta, diff_image)


def format_result(name: str, result: DiffResult, fail_threshold: float = 0.001) -> str:
    if result.changed_pixels == 0:
        return f"  ok      {name}  (identical)"
    box = result.bounding_box
    region = f"region ({box[0]},{box[1]})-({box[2]},{box[3]})" if box else ""
    status = "FAIL   " if result.changed_fraction > fail_threshold else "warn   "
    return (
        f"  {status} {name}  {result.changed_pixels}/{result.total_pixels} px "
        f"({result.changed_fraction:.2%}), max delta {result.max_channel_delta}, {region}"
    )


# ------------------------------------------------------------ demo

def make_test_image(width: int, height: int, fill: tuple[int, int, int], patch: tuple[int, int, int, int, tuple[int, int, int]] | None = None) -> Image:
    pixels = bytearray(width * height * 3)
    for i in range(0, len(pixels), 3):
        pixels[i], pixels[i + 1], pixels[i + 2] = fill
    if patch:
        px, py, pw, ph, color = patch
        for y in range(py, min(py + ph, height)):
            for x in range(px, min(px + pw, width)):
                i = (y * width + x) * 3
                pixels[i], pixels[i + 1], pixels[i + 2] = color
    return Image(width, height, bytes(pixels))


def demo() -> int:
    print("comparing three page screenshots against their baselines\n")
    print("(hand-built RGB buffers standing in for real captures — the PNG codec above")
    print("is real and gets exercised separately by round-tripping one through it)\n")

    cases = [
        ("homepage.png", make_test_image(120, 80, (240, 240, 245)), make_test_image(120, 80, (240, 240, 245))),
        # a sidebar element shifted color — localized change
        ("dashboard.png", make_test_image(160, 100, (255, 255, 255), (0, 0, 40, 100, (30, 30, 30))),
         make_test_image(160, 100, (255, 255, 255), (0, 0, 40, 100, (200, 30, 30)))),
        # anti-aliasing jitter: tiny 1-2 value differences everywhere, should NOT trip the threshold
        ("article.png", make_test_image(100, 60, (250, 248, 245)), make_test_image(100, 60, (249, 247, 246))),
    ]

    for name, baseline, current in cases:
        result = compare_images(baseline, current, threshold=10)
        print(format_result(name, result))

    print("\n--- PNG round trip, to prove the decoder/encoder are real, not stubs ---\n")
    original = make_test_image(64, 48, (100, 150, 200), (10, 10, 20, 15, (255, 0, 0)))
    encoded = encode_png(original)
    decoded = decode_png(encoded)
    matches = original.pixels == decoded.pixels
    print(f"  encoded to {len(encoded)} bytes, decoded back: pixel-identical = {matches}")

    print("\n--- a size mismatch (a responsive breakpoint changed) is a hard error, not a false pass ---\n")
    try:
        compare_images(make_test_image(100, 100, (0, 0, 0)), make_test_image(100, 90, (0, 0, 0)))
    except ValueError as exc:
        print(f"  {exc}")

    print("\nnote: article.png's anti-aliasing jitter (1-2 value differences everywhere) produced")
    print("ZERO flagged pixels at threshold=10 — a naive exact-match diff would have flagged the")
    print("entire image as 100% different, which is exactly the false-positive noise a screenshot")
    print("test suite needs to avoid or nobody trusts it after the third false alarm.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    check_p = sub.add_parser("check")
    check_p.add_argument("--baseline", required=True)
    check_p.add_argument("--current", required=True)
    check_p.add_argument("--threshold", type=int, default=10)
    check_p.add_argument("--fail-at", type=float, default=0.001)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()

    import os

    failures = 0
    for name in sorted(os.listdir(args.baseline)):
        if not name.endswith(".png"):
            continue
        base_path = os.path.join(args.baseline, name)
        cur_path = os.path.join(args.current, name)
        if not os.path.exists(cur_path):
            print(f"  MISSING  {name}  (no current screenshot to compare)")
            failures += 1
            continue
        baseline_img = decode_png(open(base_path, "rb").read())
        current_img = decode_png(open(cur_path, "rb").read())
        try:
            result = compare_images(baseline_img, current_img, args.threshold)
        except ValueError as exc:
            print(f"  ERROR    {name}  {exc}")
            failures += 1
            continue
        print(format_result(name, result, args.fail_at))
        if result.changed_fraction > args.fail_at:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
