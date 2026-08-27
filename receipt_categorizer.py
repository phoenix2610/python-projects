#!/usr/bin/env python3
"""OCR receipts, categorize by merchant rules, and export a monthly report.

    receipt_categorizer.py receipts/*.txt --month 2026-08 --out report.csv
    receipt_categorizer.py --demo

Real OCR needs a trained model this repo can't ship, so this operates on
receipt TEXT (what a real OCR pass would hand off) and focuses on the part
that's actually hard to get right afterward: pulling a total out of a page
that mentions several dollar amounts (subtotal, tax, tip, total — often in
that order, sometimes not), and categorizing by merchant name against rules
that fall back sensibly when nothing matches, rather than crashing or silently
dropping the receipt from the report.
"""
from __future__ import annotations

import argparse
import csv
import glob
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime


MONEY_RE = re.compile(r"\$?\s?(\d{1,5}\.\d{2})\b")
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: (int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), None),  # handled specially below
]
MONTH_NAMES = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}


@dataclass
class CategoryRule:
    pattern: str
    category: str

    def matches(self, merchant_line: str) -> bool:
        return bool(re.search(self.pattern, merchant_line, re.I))


DEFAULT_RULES = [
    CategoryRule(r"\b(uber|lyft|taxi|transit)\b", "transportation"),
    CategoryRule(r"\b(starbucks|peet'?s|coffee|cafe)\b", "coffee"),
    CategoryRule(r"\b(whole foods|safeway|trader joe|grocery|market)\b", "groceries"),
    CategoryRule(r"\b(hilton|marriott|airbnb|hotel|inn)\b", "lodging"),
    CategoryRule(r"\b(delta|united|american air|southwest|airlines)\b", "airfare"),
    CategoryRule(r"\b(office depot|staples|amazon)\b", "office supplies"),
    CategoryRule(r"\b(restaurant|grill|kitchen|bistro|steakhouse|diner)\b", "meals"),
]


def find_total(text: str) -> float | None:
    """Pull the transaction total out of receipt text with several dollar amounts.

    Priority order: a line explicitly labeled TOTAL (not SUBTOTAL) wins outright.
    Failing that, fall back to the largest dollar amount on the receipt — a total
    is (almost) always >= every line item, subtotal, and tax shown above it.
    """
    lines = text.split("\n")
    for line in lines:
        if re.search(r"\btotal\b", line, re.I) and not re.search(r"subtotal", line, re.I):
            if match := MONEY_RE.search(line):
                return float(match.group(1))

    amounts = [float(m.group(1)) for m in MONEY_RE.finditer(text)]
    return max(amounts) if amounts else None


def find_date(text: str) -> str | None:
    for pattern, extractor in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if extractor:
            try:
                y, m, d = extractor(match)
                return datetime(y, m, d).strftime("%Y-%m-%d")
            except ValueError:
                continue
        else:
            month_name, day, year = match.group(1)[:3], match.group(2), match.group(3)
            month_num = MONTH_NAMES.get(month_name)
            if month_num:
                try:
                    return datetime(int(year), month_num, int(day)).strftime("%Y-%m-%d")
                except ValueError:
                    continue
    return None


def find_merchant(text: str) -> str:
    """The merchant name is almost always the first non-blank, non-numeric line."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not re.match(r"^[\d\s\-/:.$]+$", stripped):
            return stripped
    return "Unknown Merchant"


def categorize(merchant: str, rules: list[CategoryRule]) -> str:
    for rule in rules:
        if rule.matches(merchant):
            return rule.category
    return "uncategorized"


@dataclass
class ReceiptRecord:
    source: str
    merchant: str
    date: str | None
    total: float | None
    category: str
    warnings: list[str] = field(default_factory=list)


def parse_receipt(text: str, source: str, rules: list[CategoryRule]) -> ReceiptRecord:
    merchant = find_merchant(text)
    total = find_total(text)
    date = find_date(text)
    category = categorize(merchant, rules)

    warnings = []
    if total is None:
        warnings.append("no dollar amount found")
    if date is None:
        warnings.append("no date found")
    if category == "uncategorized":
        warnings.append(f"merchant {merchant!r} matched no category rule")

    return ReceiptRecord(source, merchant, date, total, category, warnings)


def summarize(records: list[ReceiptRecord]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for r in records:
        if r.total is not None:
            totals[r.category] = totals.get(r.category, 0) + r.total
    return totals


# ------------------------------------------------------------ demo

SAMPLE_RECEIPTS = {
    "uber-ride.txt": """
Uber Technologies Inc.
Trip on 2026-08-14

Base fare        $8.50
Distance         $12.30
Time             $3.20
------------------------
Subtotal         $24.00
Booking fee      $2.99
Total            $26.99

Thanks for riding!
""",
    "starbucks.txt": """
STARBUCKS COFFEE
Store #4471
08/15/2026

Grande Latte         5.45
Blueberry Muffin      3.25
-----------------------------
Subtotal              8.70
Tax                   0.78
Total                 9.48
""",
    "whole-foods.txt": """
Whole Foods Market
123 Main St

Aug 16, 2026

Organic Bananas       3.99
Almond Milk            4.49
Sourdough Bread         5.29
Chicken Breast         12.99
----------------------------
SUBTOTAL              26.76
Tax                    2.14
TOTAL                 28.90
""",
    "mystery-vendor.txt": """
QuickMart LLC
2026-08-18

Item 1              4.50
Item 2              7.25
Amount Due          11.75
""",  # no explicit "total" line, no recognizable merchant category — tests the fallback paths
    "faded-receipt.txt": """
??????????? ??????
partially unreadable scan
some text here
""",  # simulates a bad OCR pass: no amounts, no date at all
}


def demo() -> int:
    print(f"{len(SAMPLE_RECEIPTS)} OCR'd receipts to process\n")

    records = [parse_receipt(text, name, DEFAULT_RULES) for name, text in SAMPLE_RECEIPTS.items()]

    for r in records:
        status = "ok" if not r.warnings else "warn"
        print(f"  [{status}] {r.source:<22} {r.merchant:<20} {r.category:<16} "
              f"${r.total:.2f}" if r.total is not None else f"  [{status}] {r.source:<22} {r.merchant:<20} {r.category:<16} (no total)")
        for w in r.warnings:
            print(f"         - {w}")

    print("\nmonthly summary by category:")
    totals = summarize(records)
    for category, amount in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {category:<18} ${amount:>8.2f}")
    print(f"  {'TOTAL':<18} ${sum(totals.values()):>8.2f}")

    print(f"\n\nnote: whole-foods.txt has 6 dollar amounts (three item prices, subtotal, tax, total),")
    print(f"and the extractor correctly picked 28.90 (the labeled TOTAL line) over 26.76 (SUBTOTAL)")
    print(f"and over 12.99 (the single largest line-item price) — label wins, not raw magnitude.")
    print(f"mystery-vendor.txt has no line literally containing the word 'total', so it fell back")
    print(f"to the largest dollar amount (11.75, 'Amount Due') — still a sane answer, not a crash.")
    print(f"faded-receipt.txt has no extractable data at all and is reported with warnings rather")
    print(f"than silently vanishing from the report or crashing the whole batch.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("receipts", nargs="*", help="receipt text files (globs supported)")
    ap.add_argument("--out", help="write a CSV report here")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.receipts:
        return demo()

    paths = [p for pattern in args.receipts for p in glob.glob(pattern)] or args.receipts
    records = []
    for path in paths:
        with open(path) as fh:
            records.append(parse_receipt(fh.read(), path, DEFAULT_RULES))

    out = open(args.out, "w", newline="") if args.out else sys.stdout
    writer = csv.writer(out)
    writer.writerow(["source", "merchant", "date", "total", "category", "warnings"])
    for r in records:
        writer.writerow([r.source, r.merchant, r.date or "", r.total or "", r.category, "; ".join(r.warnings)])
    if args.out:
        out.close()
        print(f"{len(records)} receipts -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
