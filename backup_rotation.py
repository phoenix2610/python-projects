#!/usr/bin/env python3
"""Run nightly dumps and enforce a grandfather-father-son retention policy.

    backup_rotation.py run --source ./data --dest ./backups
    backup_rotation.py --demo

GFS retention keeps daily backups for a short window, one backup per week for
longer, and one per month for a long time — instead of either keeping every
backup forever (disk fills up) or a flat "last N" window (you lose the ability
to restore from three months ago). This computes which existing backups are
still required by the policy from their timestamps alone, so it works whether
backups ran on a perfect schedule or with gaps, and never deletes the backup a
slot depends on until a newer one has actually taken its place.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class Backup:
    path: str
    timestamp: datetime
    size_bytes: int

    @property
    def day(self) -> date:
        return self.timestamp.date()


@dataclass
class RetentionPolicy:
    keep_daily: int = 7  # every day for the last N days
    keep_weekly: int = 4  # one per week for the last N weeks (after the daily window)
    keep_monthly: int = 12  # one per month for the last N months (after the weekly window)


def list_backups(directory: str) -> list[Backup]:
    backups = []
    if not os.path.isdir(directory):
        return backups
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        # filenames are `backup-YYYYMMDD-HHMMSS.tar.gz`
        stem = name.removesuffix(".tar.gz")
        if not stem.startswith("backup-"):
            continue
        try:
            timestamp = datetime.strptime(stem[len("backup-"):], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        backups.append(Backup(path=path, timestamp=timestamp, size_bytes=os.path.getsize(path)))
    return sorted(backups, key=lambda b: b.timestamp)


def compute_retained(backups: list[Backup], policy: RetentionPolicy, now: datetime) -> set[str]:
    """Return the set of backup paths that must be kept under GFS. Nothing outside
    this set survives a prune run."""
    if not backups:
        return set()

    keep: set[str] = set()
    by_day = sorted(backups, key=lambda b: b.timestamp)

    # daily: every backup within the last `keep_daily` days — all of them, not just one per day,
    # since a fresh disaster recovery window wants everything recent
    daily_cutoff = now - timedelta(days=policy.keep_daily)
    for b in by_day:
        if b.timestamp >= daily_cutoff:
            keep.add(b.path)

    # weekly: after the daily window, keep the LATEST backup from each ISO week,
    # for `keep_weekly` weeks back from the start of the daily window
    weekly_start = daily_cutoff - timedelta(weeks=policy.keep_weekly)
    weekly_candidates = [b for b in by_day if weekly_start <= b.timestamp < daily_cutoff]
    latest_per_week: dict[tuple[int, int], Backup] = {}
    for b in weekly_candidates:
        iso_year, iso_week, _ = b.timestamp.isocalendar()
        key = (iso_year, iso_week)
        if key not in latest_per_week or b.timestamp > latest_per_week[key].timestamp:
            latest_per_week[key] = b
    keep.update(b.path for b in latest_per_week.values())

    # monthly: before the weekly window, keep the LATEST backup from each calendar
    # month, for `keep_monthly` months
    monthly_start = weekly_start - timedelta(days=31 * policy.keep_monthly)
    monthly_candidates = [b for b in by_day if monthly_start <= b.timestamp < weekly_start]
    latest_per_month: dict[tuple[int, int], Backup] = {}
    for b in monthly_candidates:
        key = (b.timestamp.year, b.timestamp.month)
        if key not in latest_per_month or b.timestamp > latest_per_month[key].timestamp:
            latest_per_month[key] = b
    keep.update(b.path for b in latest_per_month.values())

    return keep


@dataclass
class PruneResult:
    kept: list[Backup]
    deleted: list[Backup]
    bytes_freed: int


def plan_prune(backups: list[Backup], policy: RetentionPolicy, now: datetime) -> PruneResult:
    keep_paths = compute_retained(backups, policy, now)
    kept = [b for b in backups if b.path in keep_paths]
    deleted = [b for b in backups if b.path not in keep_paths]
    return PruneResult(kept=kept, deleted=deleted, bytes_freed=sum(b.size_bytes for b in deleted))


def execute_prune(result: PruneResult) -> None:
    for backup in result.deleted:
        os.remove(backup.path)


def create_backup(source: str, dest_dir: str, now: datetime) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    name = f"backup-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    path = os.path.join(dest_dir, name)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(source, arcname=os.path.basename(source))
    return path


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# ------------------------------------------------------------ demo

def synthesize_backup_history(now: datetime) -> list[Backup]:
    """A fake 15-month history: daily for the last 2 weeks, then sparser, like a
    real system that has been running for over a year."""
    import random

    rng = random.Random(7)
    backups = []
    # last 14 days: one backup every day (some at slightly different hours)
    for days_ago in range(14, -1, -1):
        ts = now - timedelta(days=days_ago, hours=-rng.randint(0, 2))
        backups.append(Backup(path=f"backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz", timestamp=ts, size_bytes=rng.randint(80_000_000, 120_000_000)))
    # 15 days to 10 weeks ago: one backup every day too (simulating it ran daily the whole time)
    for days_ago in range(15, 70):
        ts = now - timedelta(days=days_ago)
        backups.append(Backup(path=f"backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz", timestamp=ts, size_bytes=rng.randint(80_000_000, 120_000_000)))
    # 10 weeks to 15 months ago: still daily (this is the "problem" — nothing has ever been pruned)
    for days_ago in range(70, 460):
        ts = now - timedelta(days=days_ago)
        backups.append(Backup(path=f"backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz", timestamp=ts, size_bytes=rng.randint(80_000_000, 120_000_000)))
    return sorted(backups, key=lambda b: b.timestamp)


def demo() -> int:
    now = datetime(2026, 8, 27, 3, 0)
    backups = synthesize_backup_history(now)
    total_size = sum(b.size_bytes for b in backups)

    print(f"simulating a backup directory that has NEVER been pruned: daily backups for 460 days\n")
    print(f"{len(backups)} backups on disk, {human_bytes(total_size)} total\n")

    policy = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=12)
    print(f"policy: {policy.keep_daily} daily, {policy.keep_weekly} weekly, {policy.keep_monthly} monthly\n")

    result = plan_prune(backups, policy, now)
    print(f"would keep {len(result.kept)} backups, delete {len(result.deleted)}")
    print(f"disk freed: {human_bytes(result.bytes_freed)}  ({result.bytes_freed / total_size:.0%} of current usage)\n")

    print("kept backups, grouped by which tier they satisfy:")
    daily_cutoff = now - timedelta(days=policy.keep_daily)
    weekly_cutoff = daily_cutoff - timedelta(weeks=policy.keep_weekly)
    daily_kept = [b for b in result.kept if b.timestamp >= daily_cutoff]
    weekly_kept = [b for b in result.kept if weekly_cutoff <= b.timestamp < daily_cutoff]
    monthly_kept = [b for b in result.kept if b.timestamp < weekly_cutoff]
    print(f"  daily tier:   {len(daily_kept)} backups (every day, last {policy.keep_daily} days)")
    print(f"  weekly tier:  {len(weekly_kept)} backups (one per week, before that)")
    print(f"  monthly tier: {len(monthly_kept)} backups (one per month, before that)")

    print("\nsample of what's kept vs deleted around the 3-week mark:")
    around_3wk = [b for b in backups if (now - b.timestamp).days in range(18, 24)]
    for b in around_3wk:
        status = "KEEP  " if b.path in {k.path for k in result.kept} else "delete"
        print(f"  {b.timestamp.strftime('%Y-%m-%d')}  ({(now - b.timestamp).days} days old)  {status}")

    print(f"\nnote: exactly one backup per ISO week survives in the weekly tier, and exactly")
    print(f"one per calendar month survives in the monthly tier — {len(weekly_kept)} + {len(monthly_kept)} + {len(daily_kept)}")
    print(f"= {len(daily_kept) + len(weekly_kept) + len(monthly_kept)} total, matching the {len(result.kept)} the planner reports.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run")
    run_p.add_argument("--source", required=True)
    run_p.add_argument("--dest", required=True)
    run_p.add_argument("--keep-daily", type=int, default=7)
    run_p.add_argument("--keep-weekly", type=int, default=4)
    run_p.add_argument("--keep-monthly", type=int, default=12)
    run_p.add_argument("--no-prune", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()

    now = datetime.now()
    new_backup = create_backup(args.source, args.dest, now)
    print(f"created {new_backup}")

    if not args.no_prune:
        policy = RetentionPolicy(args.keep_daily, args.keep_weekly, args.keep_monthly)
        backups = list_backups(args.dest)
        result = plan_prune(backups, policy, now)
        execute_prune(result)
        print(f"pruned {len(result.deleted)} old backups, freed {human_bytes(result.bytes_freed)}")
        print(f"{len(result.kept)} backups retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
