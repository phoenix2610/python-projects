#!/usr/bin/env python3
"""Import a CSV into SQLite, inferring column types from a sample of rows.

    csv2sqlite.py data.csv out.db --table sales
    csv2sqlite.py data.csv out.db --sample 5000 --if-exists replace

Types are inferred by scanning the first N rows: a column is INTEGER only if
every non-empty value parses as one, REAL if every value is numeric, and TEXT
otherwise. Rows stream in batches, so a 2GB CSV never lands in memory.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from itertools import islice
from typing import Iterator

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
BLANK = {"", "na", "n/a", "null", "none", "-"}


def infer_type(values: list[str]) -> str:
    seen = [v.strip() for v in values if v.strip().lower() not in BLANK]
    if not seen:
        return "TEXT"
    if all(INT_RE.match(v) for v in seen):
        # a leading zero is an identifier (zip code, part number), not a number
        if any(len(v) > 1 and v.lstrip("+-").startswith("0") for v in seen):
            return "TEXT"
        return "INTEGER"
    if all(FLOAT_RE.match(v) for v in seen):
        return "REAL"
    return "TEXT"


def safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip()).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{cleaned}" if cleaned else fallback
    return cleaned.lower()


def coerce(value: str, decl: str):
    v = value.strip()
    if v.lower() in BLANK:
        return None
    if decl == "INTEGER":
        return int(v)
    if decl == "REAL":
        return float(v)
    return value


def batched(rows: Iterator[list[str]], size: int) -> Iterator[list[list[str]]]:
    while chunk := list(islice(rows, size)):
        yield chunk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("db_path")
    ap.add_argument("--table", help="table name (default: the CSV's file stem)")
    ap.add_argument("--sample", type=int, default=1000, help="rows to scan for type inference")
    ap.add_argument("--batch", type=int, default=5000, help="rows per insert transaction")
    ap.add_argument("--delimiter", default=None, help="override the sniffed delimiter")
    ap.add_argument("--if-exists", choices=["fail", "replace", "append"], default="fail")
    args = ap.parse_args()

    with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
        head = fh.read(8192)
        fh.seek(0)
        delim = args.delimiter or csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
        reader = csv.reader(fh, delimiter=delim)
        try:
            header = next(reader)
        except StopIteration:
            print("empty file", file=sys.stderr)
            return 1

        cols = [safe_name(h, f"col_{i}") for i, h in enumerate(header)]
        sample = list(islice(reader, args.sample))
        types = [infer_type([r[i] for r in sample if i < len(r)]) for i in range(len(cols))]

        table = args.table or re.sub(r"\W+", "_", args.csv_path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        conn = sqlite3.connect(args.db_path)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            if args.if_exists == "fail":
                print(f"table {table!r} already exists (use --if-exists replace|append)", file=sys.stderr)
                return 1
            if args.if_exists == "replace":
                conn.execute(f'DROP TABLE "{table}"')
                exists = None
        if not exists:
            ddl = ", ".join(f'"{c}" {t}' for c, t in zip(cols, types))
            conn.execute(f'CREATE TABLE "{table}" ({ddl})')

        placeholders = ", ".join("?" * len(cols))
        insert = f'INSERT INTO "{table}" VALUES ({placeholders})'
        total = 0
        rows: Iterator[list[str]] = iter(sample + list(reader))
        for chunk in batched(rows, args.batch):
            payload = []
            for row in chunk:
                row = (row + [""] * len(cols))[: len(cols)]
                payload.append(tuple(coerce(v, t) for v, t in zip(row, types)))
            conn.executemany(insert, payload)
            conn.commit()
            total += len(payload)

    width = max(len(c) for c in cols)
    for c, t in zip(cols, types):
        print(f"  {c.ljust(width)}  {t}")
    print(f"{total} rows -> {args.db_path}:{table}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
