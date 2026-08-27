#!/usr/bin/env python3
"""Pull a spreadsheet on a timer, validate rows, and upsert into a database — idempotently.

    sheet_sync.py data.csv --db metrics.sqlite --table campaigns --key campaign_id
    sheet_sync.py --demo

A spreadsheet that people edit by hand is an unreliable data source: blank
required fields, a date typed as text, a duplicate row from a copy-paste. This
validates every row against a schema before touching the database at all —
one bad row shouldn't corrupt a sync that would otherwise succeed — and upserts
by a business key using SQLite's ON CONFLICT, so running the same sync twice
in a row is a no-op rather than a second copy of every row.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ColumnSpec:
    name: str
    type: str  # "text" | "integer" | "real" | "date"
    required: bool = True


@dataclass
class ValidationError:
    row_number: int
    column: str
    message: str


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_value(value: str, spec: ColumnSpec) -> str | None:
    """Return an error message, or None if the value is fine."""
    stripped = value.strip()
    if not stripped:
        return "required field is empty" if spec.required else None
    if spec.type == "integer":
        if not re.fullmatch(r"-?\d+", stripped):
            return f"{stripped!r} is not an integer"
    elif spec.type == "real":
        try:
            float(stripped)
        except ValueError:
            return f"{stripped!r} is not a number"
    elif spec.type == "date":
        if not DATE_RE.match(stripped):
            return f"{stripped!r} is not YYYY-MM-DD"
        try:
            datetime.strptime(stripped, "%Y-%m-%d")
        except ValueError:
            return f"{stripped!r} is not a real calendar date"
    return None


def coerce_value(value: str, spec: ColumnSpec):
    stripped = value.strip()
    if not stripped:
        return None
    if spec.type == "integer":
        return int(stripped)
    if spec.type == "real":
        return float(stripped)
    return stripped


@dataclass
class ValidationResult:
    valid_rows: list[dict]
    errors: list[ValidationError]


def validate_rows(rows: list[dict], schema: list[ColumnSpec]) -> ValidationResult:
    valid_rows = []
    errors = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        row_errors = []
        for spec in schema:
            raw = row.get(spec.name, "")
            error = validate_value(raw, spec)
            if error:
                row_errors.append(ValidationError(i, spec.name, error))
        if row_errors:
            errors.extend(row_errors)
        else:
            valid_rows.append({spec.name: coerce_value(row.get(spec.name, ""), spec) for spec in schema})
    return ValidationResult(valid_rows, errors)


def ensure_table(conn: sqlite3.Connection, table: str, schema: list[ColumnSpec], key_column: str) -> None:
    type_map = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "date": "TEXT"}
    columns_sql = ", ".join(f'"{s.name}" {type_map[s.type]}' + (" PRIMARY KEY" if s.name == key_column else "") for s in schema)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns_sql})')


@dataclass
class SyncResult:
    inserted: int
    updated: int
    unchanged: int
    rejected: int
    errors: list[ValidationError]


def sync_rows(conn: sqlite3.Connection, table: str, schema: list[ColumnSpec], key_column: str, rows: list[dict]) -> SyncResult:
    ensure_table(conn, table, schema, key_column)
    validation = validate_rows(rows, schema)

    columns = [s.name for s in schema]
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(f'"{c}"' for c in columns)
    update_clause = ", ".join(f'"{c}"=excluded."{c}"' for c in columns if c != key_column)

    inserted = updated = unchanged = 0
    for row in validation.valid_rows:
        key_value = row[key_column]
        existing = conn.execute(f'SELECT * FROM "{table}" WHERE "{key_column}"=?', (key_value,)).fetchone()
        conn.execute(
            f'INSERT INTO "{table}" ({column_list}) VALUES ({placeholders}) '
            f'ON CONFLICT("{key_column}") DO UPDATE SET {update_clause}',
            [row[c] for c in columns],
        )
        if existing is None:
            inserted += 1
        elif tuple(existing) != tuple(row[c] for c in columns):
            updated += 1
        else:
            unchanged += 1
    conn.commit()

    return SyncResult(inserted, updated, unchanged, len(validation.errors) and len({e.row_number for e in validation.errors}), validation.errors)


def format_sync_result(result: SyncResult) -> str:
    lines = [
        f"{result.inserted} inserted, {result.updated} updated, {result.unchanged} unchanged, "
        f"{result.rejected} row(s) rejected"
    ]
    if result.errors:
        lines.append("\nvalidation errors:")
        for err in result.errors:
            lines.append(f"  row {err.row_number}, column '{err.column}': {err.message}")
    return "\n".join(lines)


# ------------------------------------------------------------ demo

CAMPAIGN_SCHEMA = [
    ColumnSpec("campaign_id", "text", required=True),
    ColumnSpec("name", "text", required=True),
    ColumnSpec("budget", "real", required=True),
    ColumnSpec("clicks", "integer", required=True),
    ColumnSpec("launched_on", "date", required=True),
]


def demo() -> int:
    conn = sqlite3.connect(":memory:")

    print("sync 1: initial load from the spreadsheet\n")
    csv_v1 = """campaign_id,name,budget,clicks,launched_on
c-001,Summer Sale,5000.00,1204,2026-06-01
c-002,New Product Launch,12000.50,3891,2026-07-15
c-003,Retargeting,800,412,2026-08-01
c-004,Broken Row,not-a-number,99,2026-08-10
c-005,Missing Date,1500,220,
"""
    rows_v1 = list(csv.DictReader(csv_v1.splitlines()))
    result1 = sync_rows(conn, "campaigns", CAMPAIGN_SCHEMA, "campaign_id", rows_v1)
    print(format_sync_result(result1))

    print("\n\nsync 2: running the EXACT SAME file again — should be a no-op\n")
    result2 = sync_rows(conn, "campaigns", CAMPAIGN_SCHEMA, "campaign_id", rows_v1)
    print(format_sync_result(result2))

    print("\n\nsync 3: the spreadsheet was edited — one budget changed, one new row, one row unchanged\n")
    csv_v3 = """campaign_id,name,budget,clicks,launched_on
c-001,Summer Sale,7500.00,1204,2026-06-01
c-002,New Product Launch,12000.50,3891,2026-07-15
c-003,Retargeting,800,412,2026-08-01
c-006,Fall Preview,2200,88,2026-08-25
"""
    rows_v3 = list(csv.DictReader(csv_v3.splitlines()))
    result3 = sync_rows(conn, "campaigns", CAMPAIGN_SCHEMA, "campaign_id", rows_v3)
    print(format_sync_result(result3))

    print("\n\nfinal table contents:")
    for row in conn.execute("SELECT * FROM campaigns ORDER BY campaign_id"):
        print(f"  {row}")

    print(f"\n\nnote: sync 2 (identical data) reported {result2.unchanged} unchanged and 0 inserted/updated —")
    print(f"an idempotent re-run doesn't create duplicate rows or spuriously touch anything.")
    print(f"c-004 and c-005 were rejected in sync 1 (bad number, blank date) and never made it")
    print(f"into the table, but c-001/c-002/c-003 loaded fine in the SAME run — one bad row")
    print(f"doesn't block the good ones.")

    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_file", nargs="?")
    ap.add_argument("--db", default="sync.sqlite")
    ap.add_argument("--table", default="data")
    ap.add_argument("--key", default=None, help="column to use as the upsert key (default: first column)")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.csv_file:
        return demo()

    with open(args.csv_file, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    key_column = args.key or fieldnames[0]
    schema = [ColumnSpec(name, "text", required=(name == key_column)) for name in fieldnames]

    conn = sqlite3.connect(args.db)
    result = sync_rows(conn, args.table, schema, key_column, rows)
    print(format_sync_result(result))
    conn.close()
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
