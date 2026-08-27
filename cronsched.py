#!/usr/bin/env python3
"""Parse cron expressions, compute the next runs, and fire jobs on time.

    cronsched.py next "*/15 9-17 * * mon-fri" --count 5
    cronsched.py explain "0 3 1,15 * *"
    cronsched.py run jobs.txt          # lines of: <expr> <shell command>
    cronsched.py --demo

Supports steps (*/5), ranges, lists, names (mon, jan), @hourly-style shorthands,
and last-day-of-month (L). Next-run search walks forward field by field rather
than ticking a minute at a time, so "29 Feb" resolves instantly instead of after
1.4 million iterations.
"""
from __future__ import annotations

import argparse
import calendar
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta

FIELDS = ("minute", "hour", "day", "month", "weekday")
RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31), "month": (1, 12), "weekday": (0, 6)}
MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
SHORTHAND = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
             "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
             "@hourly": "0 * * * *"}


class CronExpr:
    def __init__(self, expression: str):
        self.raw = expression.strip()
        text = SHORTHAND.get(self.raw.lower(), self.raw)
        parts = text.split()
        if len(parts) != 5:
            raise ValueError(f"expected 5 fields, got {len(parts)}: {expression!r}")
        self.fields = {name: self._parse(part, name) for name, part in zip(FIELDS, parts)}
        self.last_day = "l" in parts[2].lower()
        # a cron with both day-of-month and weekday restricted matches EITHER (the Vixie rule)
        self.day_or = parts[2] != "*" and parts[4] != "*"

    def _parse(self, part: str, field: str) -> set[int]:
        low, high = RANGES[field]
        values: set[int] = set()
        for chunk in part.split(","):
            chunk = chunk.strip().lower()
            step = 1
            if "/" in chunk:
                chunk, _, raw_step = chunk.partition("/")
                step = int(raw_step)
            if chunk == "l":
                continue          # handled by the last_day flag, not by a value set
            if chunk in ("*", "?"):
                start, end = low, high
            elif "-" in chunk[1:]:
                start_raw, _, end_raw = chunk.partition("-")
                start, end = self._value(start_raw, field), self._value(end_raw, field)
            else:
                start = end = self._value(chunk, field)
            if start > end:                       # wrap-around, e.g. fri-mon
                values.update(range(start, high + 1, step))
                values.update(range(low, end + 1, step))
            else:
                values.update(range(start, end + 1, step))
        if field == "weekday" and 7 in values:
            values.add(0)
            values.discard(7)
        for v in values:
            if not low <= v <= high:
                raise ValueError(f"{field} value {v} outside {low}-{high}")
        return values

    @staticmethod
    def _value(token: str, field: str) -> int:
        token = token.strip().lower()
        names = DAY_NAMES if field == "weekday" else MONTH_NAMES if field == "month" else {}
        if token in names:
            return names[token]
        if token == "l":
            return RANGES[field][1]
        try:
            return int(token)
        except ValueError:
            known = ", ".join(names) or f"{RANGES[field][0]}-{RANGES[field][1]}"
            raise ValueError(f"{field}: {token!r} is not valid here (expected {known})") from None

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.fields["minute"] or when.hour not in self.fields["hour"]:
            return False
        if when.month not in self.fields["month"]:
            return False
        weekday = (when.weekday() + 1) % 7          # python: Monday=0; cron: Sunday=0
        day_ok = when.day in self.fields["day"]
        if self.last_day and when.day == calendar.monthrange(when.year, when.month)[1]:
            day_ok = True
        weekday_ok = weekday in self.fields["weekday"]
        return (day_ok or weekday_ok) if self.day_or else (day_ok and weekday_ok)

    def next(self, after: datetime | None = None) -> datetime:
        """Skip whole months/days instead of ticking minutes — bounded work, always."""
        cursor = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = cursor + timedelta(days=366 * 5)
        while cursor < limit:
            if cursor.month not in self.fields["month"]:
                cursor = (cursor.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cursor.hour not in self.fields["hour"]:
                cursor = (cursor + timedelta(hours=1)).replace(minute=0)
                continue
            if cursor.minute not in self.fields["minute"]:
                cursor += timedelta(minutes=1)
                continue
            return cursor
        raise ValueError(f"{self.raw!r} has no next run within 5 years")

    def _day_matches(self, when: datetime) -> bool:
        weekday = (when.weekday() + 1) % 7
        day_ok = when.day in self.fields["day"]
        if self.last_day and when.day == calendar.monthrange(when.year, when.month)[1]:
            day_ok = True
        weekday_ok = weekday in self.fields["weekday"]
        return (day_ok or weekday_ok) if self.day_or else (day_ok and weekday_ok)

    def describe(self) -> str:
        def phrase(field: str, plural: str) -> str:
            values = sorted(self.fields[field])
            low, high = RANGES[field]
            if not values:
                return f"no fixed {plural}"
            if len(values) == high - low + 1:
                return f"every {plural}"
            gaps = {b - a for a, b in zip(values, values[1:])}
            if len(values) > 2 and gaps == {1}:
                return f"{plural}s {values[0]} to {values[-1]}"
            if len(values) > 2 and len(gaps) == 1:
                return f"every {gaps.pop()} {plural}s from {values[0]}"
            return f"{plural} " + ", ".join(str(v) for v in values)
        days = [calendar.day_abbr[(d - 1) % 7] for d in sorted(self.fields["weekday"])]
        return (f"{phrase('minute', 'minute')}, {phrase('hour', 'hour')}, "
                f"{phrase('day', 'day')} of {phrase('month', 'month')}"
                + (f", on {', '.join(days)}" if len(days) < 7 else "")
                + (" (or the last day of the month)" if self.last_day else ""))


def run_jobs(path: str, once: bool = False) -> int:
    jobs = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        expr, _, command = line.partition(" ")
        if expr.startswith("@"):
            pass
        else:
            parts = line.split(None, 5)
            expr, command = " ".join(parts[:5]), parts[5] if len(parts) > 5 else ""
        jobs.append((CronExpr(expr), command.strip()))
    if not jobs:
        print("no jobs", file=sys.stderr)
        return 1
    print(f"{len(jobs)} jobs loaded")
    for cron, command in jobs:
        print(f"  {cron.raw:<20} next {cron.next():%Y-%m-%d %H:%M}  {command}")
    if once:
        return 0
    upcoming = {i: cron.next() for i, (cron, _) in enumerate(jobs)}
    try:
        while True:
            now = datetime.now().replace(second=0, microsecond=0)
            for i, (cron, command) in enumerate(jobs):
                if upcoming[i] <= now:
                    print(f"[{now:%H:%M}] running {command}")
                    subprocess.Popen(shlex.split(command))
                    upcoming[i] = cron.next(now)
            time.sleep(max(1.0, 60 - datetime.now().second))
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


def demo() -> int:
    now = datetime(2026, 2, 27, 23, 50)
    print(f"assuming now = {now:%Y-%m-%d %H:%M} (a Friday)\n")
    cases = [
        ("*/15 9-17 * * mon-fri", "quarter-hourly during office hours"),
        ("0 3 1,15 * *", "3am on the 1st and 15th"),
        ("30 2 * * sun", "weekly maintenance window"),
        ("0 0 29 2 *", "leap day only"),
        ("@hourly", "shorthand"),
        ("0 12 L * *", "last day of the month"),
        ("5 0 * * 1-5", "weekday mornings"),
    ]
    for expr, label in cases:
        cron = CronExpr(expr)
        runs = []
        cursor = now
        for _ in range(3):
            cursor = cron.next(cursor)
            runs.append(f"{cursor:%a %d %b %Y %H:%M}")
        print(f"  {expr:<24} {label}")
        print(f"  {'':<24} -> {', '.join(runs)}")
    print("\ndescribed in words")
    for expr in ("*/15 9-17 * * mon-fri", "0 3 1,15 * *", "0 12 L * *"):
        print(f"  {expr:<24} {CronExpr(expr).describe()}")
    print("\nsearch cost: next leap-day run from 1 Mar 2026")
    start = time.perf_counter()
    result = CronExpr("0 0 29 2 *").next(datetime(2026, 3, 1))
    print(f"  {result:%Y-%m-%d %H:%M} found in {(time.perf_counter() - start) * 1000:.2f}ms "
          f"(a minute-ticking loop would take ~1.6M iterations)")
    print("\nerrors are specific")
    for bad in ("* * * *", "99 * * * *", "0 0 * * funday"):
        try:
            CronExpr(bad)
        except ValueError as exc:
            print(f"  {bad!r}: {exc}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    n = sub.add_parser("next"); n.add_argument("expr"); n.add_argument("--count", type=int, default=5)
    e = sub.add_parser("explain"); e.add_argument("expr")
    r = sub.add_parser("run"); r.add_argument("file"); r.add_argument("--once", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    if args.cmd == "run":
        return run_jobs(args.file, args.once)
    cron = CronExpr(args.expr)
    if args.cmd == "explain":
        print(f"  {cron.raw}: {cron.describe()}")
        print(f"  next: {cron.next():%Y-%m-%d %H:%M}")
        return 0
    cursor = datetime.now()
    for _ in range(args.count):
        cursor = cron.next(cursor)
        delta = cursor - datetime.now()
        hours = delta.total_seconds() / 3600
        print(f"  {cursor:%a %Y-%m-%d %H:%M}  (in {hours:.1f}h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
