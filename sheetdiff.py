#!/usr/bin/env python3
"""Diff two spreadsheets cell by cell and print what actually changed.

    sheetdiff.py q1-old.xlsx q1-new.xlsx --key "Order ID"
    sheetdiff.py before.csv after.csv --sheet Sheet1

Reads .xlsx with nothing but zipfile and ElementTree (a workbook is a zip of XML;
strings live in a shared table and cells reference them by index). With --key,
rows are matched by that column so an insert at the top reports one added row
instead of shifting every row below it.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
Grid = list[list[str]]


def col_index(ref: str) -> int:
    """A1 -> 0, B7 -> 1, AA3 -> 26."""
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path: str, sheet: str | None) -> Grid:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        target = "xl/worksheets/sheet1.xml"
        if sheet:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            names = [s.get("name") for s in wb.iter(f"{{{NS['m']}}}sheet")]
            if sheet not in names:
                raise SystemExit(f"{path}: no sheet named {sheet!r} (have {', '.join(names)})")
            target = f"xl/worksheets/sheet{names.index(sheet) + 1}.xml"
        rows: Grid = []
        root = ET.fromstring(zf.read(target))
        for row in root.iter(f"{{{NS['m']}}}row"):
            cells: list[str] = []
            for cell in row.findall("m:c", NS):
                idx = col_index(cell.get("r", "A1"))
                while len(cells) < idx:
                    cells.append("")
                value = cell.findtext("m:v", default="", namespaces=NS)
                if cell.get("t") == "s" and value.isdigit():
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(f"{{{NS['m']}}}t"))
                cells.append(value)
            rows.append(cells)
        return rows


def read_any(path: str, sheet: str | None) -> Grid:
    if path.lower().endswith((".xlsx", ".xlsm")):
        return read_xlsx(path, sheet)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [row for row in csv.reader(fh)]


def pad(grid: Grid) -> Grid:
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def index_by_key(grid: Grid, key: str) -> tuple[list[str], dict[str, list[str]]]:
    header = grid[0]
    if key not in header:
        raise SystemExit(f"key column {key!r} not found (have {', '.join(header)})")
    col = header.index(key)
    return header, {row[col]: row for row in grid[1:] if col < len(row)}


def cell_name(col: int) -> str:
    name, col = "", col + 1
    while col:
        col, rem = divmod(col - 1, 26)
        name = chr(65 + rem) + name
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before"); ap.add_argument("after")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--key", default=None, help="column that identifies a row")
    ap.add_argument("--ignore-case", action="store_true")
    ap.add_argument("--max", type=int, default=200, help="stop after this many differences")
    args = ap.parse_args()

    a, b = pad(read_any(args.before, args.sheet)), pad(read_any(args.after, args.sheet))
    norm = (lambda v: v.strip().lower()) if args.ignore_case else (lambda v: v.strip())
    added = removed = changed = 0

    if args.key:
        header_a, rows_a = index_by_key(a, args.key)
        header_b, rows_b = index_by_key(b, args.key)
        if header_a != header_b:
            print(f"columns changed: {header_a} -> {header_b}\n")
        for k in rows_b.keys() - rows_a.keys():
            print(f"+ row {args.key}={k}: {', '.join(rows_b[k])}")
            added += 1
        for k in rows_a.keys() - rows_b.keys():
            print(f"- row {args.key}={k}: {', '.join(rows_a[k])}")
            removed += 1
        for k in sorted(rows_a.keys() & rows_b.keys()):
            for i, (before, after) in enumerate(zip(rows_a[k], rows_b[k])):
                if norm(before) != norm(after):
                    col = header_b[i] if i < len(header_b) else cell_name(i)
                    print(f"~ {args.key}={k}  {col}: {before!r} -> {after!r}")
                    changed += 1
                    if changed >= args.max:
                        print("… truncated")
                        break
    else:
        for r in range(max(len(a), len(b))):
            row_a = a[r] if r < len(a) else []
            row_b = b[r] if r < len(b) else []
            if not row_a:
                print(f"+ row {r + 1}: {', '.join(row_b)}"); added += 1; continue
            if not row_b:
                print(f"- row {r + 1}: {', '.join(row_a)}"); removed += 1; continue
            for c in range(max(len(row_a), len(row_b))):
                before = row_a[c] if c < len(row_a) else ""
                after = row_b[c] if c < len(row_b) else ""
                if norm(before) != norm(after):
                    print(f"~ {cell_name(c)}{r + 1}: {before!r} -> {after!r}")
                    changed += 1
                    if changed >= args.max:
                        print("… truncated")
                        break

    print(f"\n{changed} cells changed, {added} rows added, {removed} rows removed", file=sys.stderr)
    return 1 if (changed or added or removed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
