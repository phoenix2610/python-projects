#!/usr/bin/env python3
"""Fix subtitle timing: shift, stretch, or resync from two known anchor points.

    subshift.py movie.srt --shift -2.5              # everything 2.5s earlier
    subshift.py movie.srt --anchor 00:00:12,000=00:00:14,500 \\
                          --anchor 01:42:03,000=01:42:11,200
    subshift.py movie.srt --fps 23.976:25           # PAL speed-up conversion

Two anchors solve for a linear fit (t' = a*t + b), which is what you need when
the subtitle drifts instead of being uniformly late — the usual cause is a file
authored at a different frame rate.
"""
from __future__ import annotations

import argparse
import re
import sys

TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
ARROW = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})(.*)")


def to_seconds(text: str) -> float:
    h, m, s, ms = TIME.match(text.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def to_srt(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def solve(anchors: list[tuple[float, float]]) -> tuple[float, float]:
    """Return (scale, offset) mapping source time to target time."""
    if len(anchors) == 1:
        return 1.0, anchors[0][1] - anchors[0][0]
    (x1, y1), (x2, y2) = anchors[0], anchors[-1]
    if abs(x2 - x1) < 1e-6:
        raise SystemExit("anchors are at the same source time — pick two far apart")
    scale = (y2 - y1) / (x2 - x1)
    return scale, y1 - scale * x1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("srt")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--shift", type=float, default=None, help="seconds to add (negative = earlier)")
    ap.add_argument("--scale", type=float, default=None, help="multiply every timestamp")
    ap.add_argument("--fps", default=None, help="SRC:DST frame rates, e.g. 23.976:25")
    ap.add_argument("--anchor", action="append", default=[], metavar="SRC=DST",
                    help="a timestamp and where it should land; pass twice for a linear fit")
    ap.add_argument("--trim-negative", action="store_true", help="drop cues that end before 0")
    args = ap.parse_args()

    if args.anchor:
        pairs = []
        for spec in args.anchor:
            src, _, dst = spec.partition("=")
            pairs.append((to_seconds(src), to_seconds(dst)))
        scale, offset = solve(sorted(pairs))
    elif args.fps:
        src_fps, _, dst_fps = args.fps.partition(":")
        scale, offset = float(src_fps) / float(dst_fps), 0.0
    else:
        scale, offset = args.scale or 1.0, args.shift or 0.0

    lines = open(args.srt, encoding="utf-8-sig").read().splitlines()
    out: list[str] = []
    dropped = moved = 0
    skip_block = False
    for line in lines:
        m = ARROW.match(line.strip())
        if not m:
            if not skip_block:
                out.append(line)
            elif not line.strip():
                skip_block = False
            continue
        start = to_seconds(m.group(1)) * scale + offset
        end = to_seconds(m.group(2)) * scale + offset
        if args.trim_negative and end < 0:
            dropped += 1
            skip_block = True
            while out and out[-1].strip():   # also drop the cue number above the arrow
                out.pop()
            continue
        out.append(f"{to_srt(start)} --> {to_srt(end)}{m.group(3)}")
        moved += 1

    text = "\n".join(out).rstrip() + "\n"
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    else:
        sys.stdout.write(text)
    print(f"{moved} cues retimed  (scale {scale:.6f}, offset {offset:+.3f}s)"
          + (f", {dropped} dropped" if dropped else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
