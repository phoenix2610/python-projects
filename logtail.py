#!/usr/bin/env python3
"""Follow a log file and colour it by level, with your own highlight rules.

    logtail.py /var/log/app.log
    logtail.py app.log --hi 'user=(\\w+)' --hi ERROR:red --only WARN,ERROR
    cat app.log | logtail.py -

Survives log rotation: when the inode under the path changes, it reopens the new
file and keeps following instead of tailing a deleted handle forever.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

RESET = "\033[0m"
COLORS = {
    "grey": "\033[90m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "white": "\033[97m",
    "bold-red": "\033[1;31m",
}
LEVELS = [
    (re.compile(r"\b(FATAL|CRITICAL|PANIC)\b"), "bold-red"),
    (re.compile(r"\b(ERROR|ERR|SEVERE)\b"), "red"),
    (re.compile(r"\b(WARN(?:ING)?)\b"), "yellow"),
    (re.compile(r"\b(INFO|NOTICE)\b"), "green"),
    (re.compile(r"\b(DEBUG|TRACE)\b"), "grey"),
]
TIMESTAMP = re.compile(r"^\S*\d{2}:\d{2}:\d{2}\S*")


def paint(text: str, color: str, enabled: bool) -> str:
    return f"{COLORS[color]}{text}{RESET}" if enabled and color in COLORS else text


def level_of(line: str) -> tuple[str | None, str]:
    for pattern, color in LEVELS:
        if m := pattern.search(line):
            return m.group(1).upper(), color
    return None, "white"


def render(line: str, rules: list[tuple[re.Pattern, str]], color: bool) -> str:
    level, level_color = level_of(line)
    out = line
    if m := TIMESTAMP.match(out):
        out = paint(m.group(0), "grey", color) + out[m.end():]
    if level:
        out = re.sub(rf"\b{level}\b", paint(level, level_color, color), out, count=1, flags=re.I)
    for pattern, hl in rules:
        out = pattern.sub(lambda m: paint(m.group(0), hl, color), out)
    return out


def follow(path: str, from_start: bool):
    """Yield lines forever, reopening the file if it is rotated out from under us."""
    fh = open(path, "r", errors="replace")
    if not from_start:
        fh.seek(0, os.SEEK_END)
    inode = os.fstat(fh.fileno()).st_ino
    while True:
        line = fh.readline()
        if line:
            yield line.rstrip("\n")
            continue
        time.sleep(0.15)
        try:
            if os.stat(path).st_ino != inode:
                fh.close()
                fh = open(path, "r", errors="replace")
                inode = os.fstat(fh.fileno()).st_ino
        except FileNotFoundError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="log file, or - for stdin")
    ap.add_argument("--hi", action="append", default=[], metavar="RE[:COLOR]", help="highlight a pattern")
    ap.add_argument("--only", default="", help="comma-separated levels to keep, e.g. WARN,ERROR")
    ap.add_argument("--grep", default=None, help="drop lines that do not match this regex")
    ap.add_argument("--from-start", action="store_true", help="print the whole file before following")
    ap.add_argument("--no-follow", action="store_true", help="print and exit instead of tailing")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    color = not args.no_color and sys.stdout.isatty()
    rules = []
    for spec in args.hi:
        pattern, _, name = spec.rpartition(":")
        if not pattern or name not in COLORS:
            pattern, name = spec, "cyan"
        rules.append((re.compile(pattern), name))
    only = {lvl.strip().upper() for lvl in args.only.split(",") if lvl.strip()}
    grep = re.compile(args.grep) if args.grep else None

    if args.path == "-":
        lines = (ln.rstrip("\n") for ln in sys.stdin)
    elif args.no_follow:
        with open(args.path, errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    else:
        lines = follow(args.path, args.from_start)

    try:
        for line in lines:
            if only and (level_of(line)[0] or "") not in only:
                continue
            if grep and not grep.search(line):
                continue
            print(render(line, rules, color), flush=True)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
