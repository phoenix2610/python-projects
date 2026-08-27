#!/usr/bin/env python3
"""Show every local branch with the facts you need to decide, then delete the ones you tick.

    gitclean.py                      # interactive: pick branches to delete
    gitclean.py --merged --yes       # non-interactive: drop everything already merged
    gitclean.py --stale 90 --dry-run

"Merged" here means merged into the default branch *or* squash-merged: a squashed
branch has no merge commit, so it is detected by diffing its tree against the
merge base — the case `git branch --merged` misses and everyone hand-deletes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

SEP = "\x1f"


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise SystemExit(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def default_branch() -> str:
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if head:
        return head.rsplit("/", 1)[-1]
    for name in ("main", "master", "trunk"):
        if git("rev-parse", "--verify", "--quiet", name, check=False):
            return name
    raise SystemExit("cannot find a default branch — pass --base")


def squash_merged(branch: str, base: str) -> bool:
    """True if the branch's changes are already in base, even without a merge commit."""
    merge_base = git("merge-base", base, branch, check=False)
    if not merge_base:
        return False
    tree = git("rev-parse", f"{branch}^{{tree}}", check=False)
    if not tree:
        return False
    commit = git("commit-tree", tree, "-p", merge_base, "-m", "probe", check=False)
    return commit and not git("cherry", base, commit, check=False).startswith("+")


def collect(base: str) -> list[dict]:
    fmt = SEP.join(["%(refname:short)", "%(committerdate:unix)", "%(committername)",
                    "%(upstream:short)", "%(upstream:track)", "%(contents:subject)"])
    merged = set(git("branch", "--merged", base, "--format=%(refname:short)").splitlines())
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    rows = []
    for line in git("for-each-ref", "refs/heads", f"--format={fmt}").splitlines():
        name, ts, author, upstream, track, subject = line.split(SEP)
        if name == base:
            continue
        rows.append({
            "name": name, "age": (time.time() - int(ts)) / 86400, "author": author,
            "upstream": upstream, "gone": "gone" in track, "subject": subject,
            "merged": name in merged, "current": name == current,
        })
    return rows


def fmt_row(i: int, b: dict, width: int) -> str:
    flags = []
    if b["current"]:
        flags.append("current")
    if b["merged"]:
        flags.append("merged")
    elif b.get("squashed"):
        flags.append("squash-merged")
    if b["gone"]:
        flags.append("remote gone")
    if not b["upstream"]:
        flags.append("never pushed")
    tail = ", ".join(flags)
    return f"{i:>3}. {b['name'].ljust(width)}  {b['age']:>4.0f}d  {b['author'][:14].ljust(14)}  {tail}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=None, help="branch to compare against")
    ap.add_argument("--merged", action="store_true", help="preselect merged branches")
    ap.add_argument("--stale", type=int, default=0, help="preselect branches older than N days")
    ap.add_argument("--gone", action="store_true", help="preselect branches whose remote is deleted")
    ap.add_argument("--check-squash", action="store_true", help="also detect squash-merged branches")
    ap.add_argument("--yes", action="store_true", help="delete the preselection without prompting")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    git("rev-parse", "--git-dir")
    base = args.base or default_branch()
    branches = collect(base)
    if not branches:
        print(f"no branches besides {base}")
        return 0
    if args.check_squash:
        for b in branches:
            b["squashed"] = not b["merged"] and squash_merged(b["name"], base)

    width = max(len(b["name"]) for b in branches)
    preselected = [
        b for b in branches
        if not b["current"] and (
            (args.merged and (b["merged"] or b.get("squashed")))
            or (args.stale and b["age"] >= args.stale)
            or (args.gone and b["gone"]))
    ]

    print(f"{len(branches)} branches, comparing against {base}\n")
    for i, b in enumerate(branches, 1):
        marker = "*" if b in preselected else " "
        print(f"{marker}{fmt_row(i, b, width)}")

    if args.yes:
        chosen = preselected
    else:
        if not sys.stdin.isatty():
            print("\n(no tty — rerun with --yes to delete the starred branches)")
            return 0
        raw = input("\ndelete which? numbers, ranges (2-5), 'starred', or blank to cancel: ").strip()
        if not raw:
            print("cancelled")
            return 0
        if raw == "starred":
            chosen = preselected
        else:
            picked: set[int] = set()
            for part in raw.replace(",", " ").split():
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    picked.update(range(int(lo), int(hi) + 1))
                else:
                    picked.add(int(part))
            chosen = [b for i, b in enumerate(branches, 1) if i in picked]

    if not chosen:
        print("nothing selected")
        return 0
    for b in chosen:
        if b["current"]:
            print(f"  skip   {b['name']} (checked out)")
            continue
        safe = b["merged"] or b.get("squashed")
        if args.dry_run:
            print(f"  would delete {b['name']}" + ("" if safe else "  (UNMERGED — needs -D)"))
            continue
        flag = "-d" if safe else "-D"
        proc = subprocess.run(["git", "branch", flag, b["name"]], capture_output=True, text=True)
        print(f"  {'deleted' if not proc.returncode else 'failed '} {b['name']}"
              + ("" if not proc.returncode else f"  {proc.stderr.strip()}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
