#!/usr/bin/env python3
"""A todo list you drive by typing fragments instead of ids.

    todo.py add "renew the domain" --tag admin --due friday
    todo.py done rnw dmn        # subsequence match, best score wins
    todo.py ls --tag admin
    todo.py find dom            # ranked matches with scores

Matching is a subsequence scorer in the spirit of fzf: characters must appear in
order, consecutive runs and word-boundary hits score higher, and a shorter
haystack breaks ties. Tasks live in ~/.todo.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

STORE = os.path.expanduser(os.environ.get("TODO_FILE", "~/.todo.json"))
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def load() -> list[dict]:
    try:
        with open(STORE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(tasks: list[dict]) -> None:
    tmp = STORE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(tasks, fh, indent=1)
    os.replace(tmp, STORE)  # atomic: a crash mid-write cannot truncate the list


def score(needle: str, haystack: str) -> int | None:
    """Subsequence score, or None if `needle` is not a subsequence of `haystack`."""
    n, h = needle.lower(), haystack.lower()
    if not n:
        return 0
    total, prev, hi = 0, -2, 0
    for ch in n:
        hi = h.find(ch, prev + 1 if prev >= 0 else 0)
        if hi < 0:
            return None
        if hi == prev + 1:
            total += 8                      # consecutive run
        elif hi == 0 or h[hi - 1] in " -_/":
            total += 6                      # start of a word
        else:
            total += 1
        prev = hi
    return total * 100 - len(h)


def rank(tasks: list[dict], query: str) -> list[tuple[int, dict]]:
    q = query.replace(" ", "")
    hits = []
    for t in tasks:
        s = score(q, t["text"] + " " + " ".join(t.get("tags", [])))
        if s is not None:
            hits.append((s, t))
    return sorted(hits, key=lambda pair: -pair[0])


def parse_due(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip().lower()
    now = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)
    if text == "today":
        return now.timestamp()
    if text == "tomorrow":
        return (now + timedelta(days=1)).timestamp()
    if text in WEEKDAYS:
        delta = (WEEKDAYS.index(text) - now.weekday()) % 7 or 7
        return (now + timedelta(days=delta)).timestamp()
    if text.endswith("d") and text[:-1].isdigit():
        return (now + timedelta(days=int(text[:-1]))).timestamp()
    return datetime.fromisoformat(text).timestamp()


def fmt(task: dict, width: int = 0) -> str:
    mark = "x" if task.get("done") else " "
    tags = " ".join(f"#{t}" for t in task.get("tags", []))
    due = ""
    if task.get("due"):
        days = (datetime.fromtimestamp(task["due"]).date() - datetime.now().date()).days
        due = "overdue" if days < 0 else "today" if days == 0 else f"{days}d"
        due = f"  ({due})"
    return f"[{mark}] {task['text'].ljust(width)}{due}  {tags}".rstrip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a task")
    a.add_argument("text", nargs="+")
    a.add_argument("--tag", action="append", default=[])
    a.add_argument("--due", default=None, help="today | tomorrow | friday | 3d | 2026-09-01")
    for name, help_text in (("done", "complete the best match"), ("rm", "delete the best match")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", nargs="+")
    f = sub.add_parser("find", help="rank tasks against a query")
    f.add_argument("query", nargs="+")
    l = sub.add_parser("ls", help="list tasks")
    l.add_argument("--all", action="store_true", help="include completed")
    l.add_argument("--tag", default=None)
    args = ap.parse_args()

    tasks = load()

    if args.cmd == "add":
        task = {"text": " ".join(args.text), "tags": args.tag, "done": False,
                "created": time.time(), "due": parse_due(args.due)}
        tasks.append(task)
        save(tasks)
        print(fmt(task))
        return 0

    if args.cmd in ("done", "rm", "find"):
        query = " ".join(args.query)
        hits = rank([t for t in tasks if not t.get("done") or args.cmd == "find"], query)
        if not hits:
            print(f"no task matches {query!r}", file=sys.stderr)
            return 1
        if args.cmd == "find":
            width = max(len(t["text"]) for _, t in hits)
            for s, t in hits[:10]:
                print(f"{s:6d}  {fmt(t, width)}")
            return 0
        best = hits[0][1]
        if len(hits) > 1 and hits[0][0] == hits[1][0]:
            print(f"ambiguous: {best['text']!r} and {hits[1][1]['text']!r} tie — type more", file=sys.stderr)
            return 1
        if args.cmd == "done":
            best["done"] = True
            best["completed"] = time.time()
            print(fmt(best))
        else:
            tasks.remove(best)
            print(f"deleted {best['text']!r}")
        save(tasks)
        return 0

    pending = [t for t in tasks if args.all or not t.get("done")]
    if args.tag:
        pending = [t for t in pending if args.tag in t.get("tags", [])]
    if not pending:
        print("nothing to do")
        return 0
    pending.sort(key=lambda t: (t.get("due") or 9e18, t["created"]))
    width = max(len(t["text"]) for t in pending)
    for t in pending:
        print(fmt(t, width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
