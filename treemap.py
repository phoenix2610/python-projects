#!/usr/bin/env python3
"""A squarified treemap of disk usage, drawn in the terminal with block characters.

    treemap.py ~/Downloads
    treemap.py / --depth 2 --width 100 --min-share 0.01

The layout is the squarified algorithm from Bruls et al: lay children into the
short side of the remaining rectangle while the worst aspect ratio keeps
improving, then start a new row. That's what keeps cells readable rather than
letting one file become a 1-cell-tall sliver across the screen.
"""
from __future__ import annotations

import argparse
import os
import sys

BLOCKS = "░▒▓█"
PALETTE = [f"\033[38;5;{n}m" for n in (67, 108, 144, 180, 174, 139, 103, 110)]
RESET = "\033[0m"


class Node:
    __slots__ = ("name", "size", "children", "is_dir")

    def __init__(self, name: str, size: int = 0, is_dir: bool = False):
        self.name, self.size, self.is_dir = name, size, is_dir
        self.children: list[Node] = []


def scan(path: str, depth: int) -> Node:
    node = Node(os.path.basename(path.rstrip("/")) or path, is_dir=True)
    try:
        entries = list(os.scandir(path))
    except OSError:
        return node
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                child = scan(entry.path, depth - 1) if depth > 1 else Node(entry.name, dir_size(entry.path), True)
                node.children.append(child)
                node.size += child.size
            else:
                child = Node(entry.name, entry.stat().st_size)
                node.children.append(child)
                node.size += child.size
        except OSError:
            continue
    return node


def dir_size(path: str) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


def worst(row: list[float], side: float) -> float:
    """Worst aspect ratio in a row laid along `side` — the squarify quality metric."""
    total = sum(row)
    if total == 0 or side == 0:
        return float("inf")
    return max(max((side * side * r) / (total * total), (total * total) / (side * side * r))
               for r in row if r > 0)


def squarify(items: list[tuple[str, float]], x: float, y: float, w: float, h: float) -> list[tuple]:
    """Return [(name, x, y, w, h)] cells filling the rectangle. Values must sum to w*h."""
    out: list[tuple] = []
    items = [i for i in items if i[1] > 0]
    while items:
        side = min(w, h)
        row: list[tuple[str, float]] = []
        while items:
            trial = row + [items[0]]
            if row and worst([v for _, v in trial], side) > worst([v for _, v in row], side):
                break
            row.append(items.pop(0))
        total = sum(v for _, v in row)
        if w >= h:
            band = total / h if h else 0
            offset = y
            for name, value in row:
                cell_h = (value / total) * h if total else 0
                out.append((name, x, offset, band, cell_h))
                offset += cell_h
            x, w = x + band, w - band
        else:
            band = total / w if w else 0
            offset = x
            for name, value in row:
                cell_w = (value / total) * w if total else 0
                out.append((name, offset, y, cell_w, band))
                offset += cell_w
            y, h = y + band, h - band
    return out


def draw(cells: list[tuple], width: int, height: int, color: bool) -> None:
    grid = [[" "] * width for _ in range(height)]
    tint = [[""] * width for _ in range(height)]
    for i, (name, x, y, w, h) in enumerate(cells):
        x0, y0 = int(round(x)), int(round(y))
        x1, y1 = min(width, int(round(x + w))), min(height, int(round(y + h)))
        shade = BLOCKS[i % len(BLOCKS)]
        for row in range(y0, max(y1, y0 + 1)):
            for col in range(x0, max(x1, x0 + 1)):
                if 0 <= row < height and 0 <= col < width:
                    grid[row][col] = shade
                    tint[row][col] = PALETTE[i % len(PALETTE)] if color else ""
        label = f" {name}"[: max(0, x1 - x0 - 1)]
        if label.strip() and y1 > y0 and 0 <= y0 < height:
            for j, ch in enumerate(label):
                if x0 + j < width:
                    grid[y0][x0 + j] = ch
    for row, tints in zip(grid, tint):
        line = "".join((t + c + (RESET if t else "")) for c, t in zip(row, tints))
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--depth", type=int, default=1, help="directory levels to recurse before summarising")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=20)
    ap.add_argument("--min-share", type=float, default=0.005, help="fold cells below this fraction into 'other'")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    width = args.width or min(100, os.get_terminal_size().columns if sys.stdout.isatty() else 80)
    root = scan(os.path.abspath(args.path), args.depth)
    if not root.size:
        print("nothing to show (empty or unreadable)", file=sys.stderr)
        return 1

    kids = sorted(root.children, key=lambda n: -n.size)
    keep = [k for k in kids if k.size / root.size >= args.min_share]
    folded = sum(k.size for k in kids if k not in keep)
    items = [(f"{k.name}{'/' if k.is_dir else ''} {human(k.size)}", float(k.size)) for k in keep]
    if folded:
        items.append((f"other {human(folded)}", float(folded)))
    # squarify works in area units: scale byte counts so they sum to the canvas area
    scale = (width * args.height) / sum(v for _, v in items)
    items = [(name, value * scale) for name, value in items]

    print(f"{root.name}  {human(root.size)}  ({len(kids)} entries)\n")
    cells = squarify(items, 0, 0, width, args.height)
    draw(cells, width, args.height, not args.no_color and sys.stdout.isatty())
    print()
    for k in keep[:10]:
        bar = "█" * max(1, round(30 * k.size / root.size))
        print(f"{human(k.size):>7}  {k.size * 100 / root.size:5.1f}%  {bar} {k.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
