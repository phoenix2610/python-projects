#!/usr/bin/env python3
"""Create accounts and group memberships from a CSV, idempotently.

    bulk_provision.py users.csv --db users.sqlite
    bulk_provision.py --demo

The property that makes bulk provisioning safe to re-run is idempotency: the
same CSV run twice should create zero duplicate accounts the second time, and
a CSV with three new rows added to an otherwise-unchanged file should only
create those three. This diffs the CSV against existing accounts by a stable
key (email), only inserts what's missing, and treats group membership changes
(someone added to a new group in the sheet) as a separate, additive operation
from account creation — removing someone from a CSV row does NOT remove their
account or memberships, because a spreadsheet has no reliable way to express
deliberate deletion versus "this row just wasn't re-exported this time."
"""
from __future__ import annotations

import argparse
import csv
import re
import secrets
import sqlite3
from dataclasses import dataclass, field


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass
class UserRow:
    email: str
    full_name: str
    groups: list[str]
    row_number: int


@dataclass
class RowError:
    row_number: int
    message: str


def parse_csv_rows(rows: list[dict], row_offset: int = 2) -> tuple[list[UserRow], list[RowError]]:
    parsed = []
    errors = []
    seen_emails: dict[str, int] = {}

    for i, row in enumerate(rows, start=row_offset):
        email = (row.get("email") or "").strip().lower()
        full_name = (row.get("full_name") or "").strip()
        groups_raw = (row.get("groups") or "").strip()
        groups = [g.strip() for g in groups_raw.split(";") if g.strip()]

        if not email:
            errors.append(RowError(i, "missing email"))
            continue
        if not EMAIL_RE.match(email):
            errors.append(RowError(i, f"invalid email format: {email!r}"))
            continue
        if not full_name:
            errors.append(RowError(i, "missing full_name"))
            continue
        if email in seen_emails:
            errors.append(RowError(i, f"duplicate email in this CSV (also on row {seen_emails[email]})"))
            continue

        seen_emails[email] = i
        parsed.append(UserRow(email, full_name, groups, i))

    return parsed, errors


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            temp_password TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memberships (
            email TEXT NOT NULL,
            group_name TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (email, group_name)
        )
    """)


def generate_temp_password() -> str:
    return secrets.token_urlsafe(9)


@dataclass
class ProvisionResult:
    created_accounts: list[str]
    memberships_added: list[tuple[str, str]]
    already_existed: list[str]
    row_errors: list[RowError]


def provision(conn: sqlite3.Connection, rows: list[UserRow]) -> ProvisionResult:
    ensure_schema(conn)
    created = []
    already_existed = []
    memberships_added = []

    for row in rows:
        existing = conn.execute("SELECT 1 FROM users WHERE email = ?", (row.email,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (email, full_name, temp_password) VALUES (?, ?, ?)",
                (row.email, row.full_name, generate_temp_password()),
            )
            created.append(row.email)
        else:
            already_existed.append(row.email)

        for group in row.groups:
            existing_membership = conn.execute(
                "SELECT 1 FROM memberships WHERE email = ? AND group_name = ?", (row.email, group)
            ).fetchone()
            if existing_membership is None:
                conn.execute("INSERT INTO memberships (email, group_name) VALUES (?, ?)", (row.email, group))
                memberships_added.append((row.email, group))

    conn.commit()
    return ProvisionResult(created, memberships_added, already_existed, [])


def format_result(result: ProvisionResult) -> str:
    lines = [
        f"{len(result.created_accounts)} accounts created, "
        f"{len(result.already_existed)} already existed, "
        f"{len(result.memberships_added)} group memberships added"
    ]
    if result.created_accounts:
        lines.append("\nnew accounts:")
        for email in result.created_accounts:
            lines.append(f"  {email}")
    if result.memberships_added:
        lines.append("\nnew memberships:")
        for email, group in result.memberships_added:
            lines.append(f"  {email} -> {group}")
    if result.row_errors:
        lines.append("\nrejected rows:")
        for err in result.row_errors:
            lines.append(f"  row {err.row_number}: {err.message}")
    return "\n".join(lines)


# ------------------------------------------------------------ demo

def demo() -> int:
    conn = sqlite3.connect(":memory:")

    print("run 1: initial provisioning from HR's export\n")
    csv_v1 = """email,full_name,groups
ana@example.com,Ana Rivera,engineering;on-call
bo@example.com,Bo Chen,engineering
cy@example.com,Cy Patel,design
,Missing Email,marketing
dee@example.com,,sales
ana@example.com,Ana Rivera Duplicate,engineering
"""
    rows_v1, errors_v1 = parse_csv_rows(list(csv.DictReader(csv_v1.splitlines())))
    print(f"parsed {len(rows_v1)} valid rows, {len(errors_v1)} rejected:")
    for err in errors_v1:
        print(f"  row {err.row_number}: {err.message}")

    result1 = provision(conn, rows_v1)
    print(f"\n{format_result(result1)}")

    print("\n\nrun 2: running the EXACT SAME CSV again — should create nothing new\n")
    result2 = provision(conn, rows_v1)
    print(format_result(result2))

    print("\n\nrun 3: the sheet was updated — dee was fixed, eli is new, ana joined a new group,")
    print("        and bo's row was simply not re-exported this time (should NOT delete bo)\n")
    csv_v3 = """email,full_name,groups
ana@example.com,Ana Rivera,engineering;on-call;security-review
cy@example.com,Cy Patel,design
dee@example.com,Dee Okafor,sales
eli@example.com,Eli Nakamura,engineering
"""
    rows_v3, _ = parse_csv_rows(list(csv.DictReader(csv_v3.splitlines())))
    result3 = provision(conn, rows_v3)
    print(format_result(result3))

    print("\n\nfinal state:")
    for row in conn.execute("SELECT email, full_name FROM users ORDER BY email"):
        groups = [g[0] for g in conn.execute("SELECT group_name FROM memberships WHERE email = ? ORDER BY group_name", (row[0],))]
        print(f"  {row[0]:<20} {row[1]:<20} groups: {', '.join(groups) or '(none)'}")

    print(f"\n\nnote: bo@example.com still has an account with 'engineering' membership even though")
    print(f"bo's row was absent from the sheet in run 3 — the CSV is a source of NEW accounts and")
    print(f"NEW group memberships, never an implicit deletion list. dee's full_name was blank in")
    print(f"run 1 (rejected) and got created fresh in run 3 once it was fixed. ana's account already")
    print(f"existed by run 3, so only her new 'security-review' membership was added, not a duplicate")
    print(f"account.")

    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_file", nargs="?")
    ap.add_argument("--db", default="users.sqlite")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.csv_file:
        return demo()

    with open(args.csv_file, newline="") as fh:
        rows, errors = parse_csv_rows(list(csv.DictReader(fh)))

    conn = sqlite3.connect(args.db)
    result = provision(conn, rows)
    result.row_errors = errors
    print(format_result(result))
    conn.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
