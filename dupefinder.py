#!/usr/bin/env python3
"""Find duplicate files the cheap way first: size, then head hash, then full hash.

    dupefinder.py ~/Downloads
    dupefinder.py ~/Photos --min-size 1M --delete --keep oldest

Most candidate pairs die at the size check, which costs one stat() call. Only
same-size files get a 64KB head hash, and only matching head hashes get read in
full — so a folder of 50k photos does a few hundred full reads, not 50k.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict

HEAD_BYTES = 64 * 1024


def parse_size(text: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    text = text.strip().lower().rstrip("b")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text or 0)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def digest(path: str, limit: int | None = None) -> str:
    h = hashlib.blake2b(digest_size=16)
    read = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            if limit is not None and read + len(chunk) > limit:
                h.update(chunk[: limit - read])
                break
            h.update(chunk)
            read += len(chunk)
    return h.hexdigest()


def walk(roots: list[str], min_size: int, follow: bool) -> dict[int, list[str]]:
    by_size: dict[int, list[str]] = defaultdict(list)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                if st.st_size >= min_size:
                    by_size[st.st_size].append(path)
    return by_size


def group(paths: list[str], size: int) -> list[list[str]]:
    """Split a same-size bucket into groups of byte-identical files."""
    if len(paths) < 2:
        return []
    stage: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        try:
            stage[digest(p, HEAD_BYTES)].append(p)
        except OSError:
            continue
    out = []
    for candidates in stage.values():
        if len(candidates) < 2:
            continue
        if size <= HEAD_BYTES:  # head hash already covered the whole file
            out.append(candidates)
            continue
        full: dict[str, list[str]] = defaultdict(list)
        for p in candidates:
            try:
                full[digest(p)].append(p)
            except OSError:
                continue
        out.extend(g for g in full.values() if len(g) > 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--min-size", default="1", help="ignore files below this size (e.g. 1M)")
    ap.add_argument("--delete", action="store_true", help="delete all but one file per group")
    ap.add_argument("--keep", choices=["oldest", "newest", "shortest-path"], default="oldest")
    ap.add_argument("--follow-symlinks", action="store_true")
    args = ap.parse_args()

    min_size = parse_size(args.min_size)
    by_size = walk(args.roots, min_size, args.follow_symlinks)
    scanned = sum(len(v) for v in by_size.values())

    groups: list[list[str]] = []
    for size, paths in sorted(by_size.items(), reverse=True):
        groups.extend(group(paths, size))

    reclaim = 0
    for g in groups:
        size = os.path.getsize(g[0])
        reclaim += size * (len(g) - 1)
        if args.keep == "oldest":
            g.sort(key=lambda p: os.path.getmtime(p))
        elif args.keep == "newest":
            g.sort(key=lambda p: -os.path.getmtime(p))
        else:
            g.sort(key=lambda p: (len(p), p))
        print(f"\n{human(size)} x{len(g)}")
        print(f"  keep   {g[0]}")
        for dup in g[1:]:
            print(f"  {'delete' if args.delete else 'dup   '} {dup}")
            if args.delete:
                os.remove(dup)

    verb = "freed" if args.delete else "reclaimable"
    print(f"\n{scanned} files scanned, {len(groups)} duplicate groups, {human(reclaim)} {verb}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
