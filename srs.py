#!/usr/bin/env python3
"""A spaced-repetition deck in the terminal, scheduled with SM-2.

    srs.py import notes/*.md          # first line is the question, rest the answer
    srs.py review --limit 20
    srs.py stats

SM-2 keeps a per-card ease factor: rate a card 0-5 and the interval multiplies by
that ease, which drops on a lapse and creeps up on easy recalls. That is why a
card you keep failing comes back tomorrow while one you know slides to next
month, without you tuning anything.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta

STORE = os.path.expanduser(os.environ.get("SRS_DECK", "~/.srs-deck.json"))
DAY = 86400


def load() -> dict:
    try:
        return json.load(open(STORE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cards": {}}


def save(deck: dict) -> None:
    tmp = STORE + ".tmp"
    json.dump(deck, open(tmp, "w"), indent=1)
    os.replace(tmp, STORE)


def new_card(question: str, answer: str, source: str) -> dict:
    return {"q": question, "a": answer, "source": source, "ease": 2.5,
            "interval": 0, "reps": 0, "lapses": 0, "due": time.time(), "history": []}


def schedule(card: dict, grade: int) -> dict:
    """SM-2: grade 0-5, where <3 is a lapse that restarts the interval."""
    if grade < 3:
        card["lapses"] += 1
        card["reps"] = 0
        card["interval"] = 1
        card["ease"] = max(1.3, card["ease"] - 0.2)
    else:
        card["reps"] += 1
        if card["reps"] == 1:
            card["interval"] = 1
        elif card["reps"] == 2:
            card["interval"] = 6
        else:
            card["interval"] = round(card["interval"] * card["ease"])
        card["ease"] = max(1.3, card["ease"] + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    card["due"] = time.time() + card["interval"] * DAY
    card["history"].append({"at": time.time(), "grade": grade})
    return card


def due_cards(deck: dict, limit: int, include_new: bool) -> list[tuple[str, dict]]:
    now = time.time()
    ready = [(cid, c) for cid, c in deck["cards"].items()
             if c["due"] <= now and (include_new or c["reps"] > 0)]
    ready.sort(key=lambda pair: (pair[1]["reps"] > 0, pair[1]["due"]))
    random.shuffle(ready[: max(len(ready) // 2, 1)])
    return ready[:limit]


def wrap(text: str, width: int = 76, indent: str = "  ") -> str:
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(indent + line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(indent + line)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import"); i.add_argument("paths", nargs="+")
    a = sub.add_parser("add"); a.add_argument("question"); a.add_argument("answer")
    r = sub.add_parser("review"); r.add_argument("--limit", type=int, default=20)
    r.add_argument("--no-new", action="store_true"); r.add_argument("--auto", type=int, default=None,
                   help="answer every card with this grade (for scripted runs)")
    sub.add_parser("stats")
    f = sub.add_parser("forecast"); f.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    deck = load()

    if args.cmd == "import":
        added = skipped = 0
        for pattern in args.paths:
            for path in sorted(glob.glob(pattern)):
                text = open(path, encoding="utf-8").read().strip()
                if not text:
                    continue
                question, _, answer = text.partition("\n")
                cid = os.path.relpath(path)
                if cid in deck["cards"]:
                    deck["cards"][cid]["q"] = question.lstrip("# ").strip()
                    deck["cards"][cid]["a"] = answer.strip()
                    skipped += 1
                    continue
                deck["cards"][cid] = new_card(question.lstrip("# ").strip(), answer.strip(), path)
                added += 1
        save(deck)
        print(f"{added} new cards, {skipped} updated in place (progress kept)")
        return 0

    if args.cmd == "add":
        cid = f"manual:{int(time.time() * 1000)}"
        deck["cards"][cid] = new_card(args.question, args.answer, "manual")
        save(deck)
        print(f"added {cid}")
        return 0

    if args.cmd == "stats":
        cards = list(deck["cards"].values())
        if not cards:
            print("empty deck")
            return 0
        now = time.time()
        buckets = {"new": 0, "learning": 0, "young": 0, "mature": 0}
        for c in cards:
            if c["reps"] == 0:
                buckets["new"] += 1
            elif c["interval"] < 7:
                buckets["learning"] += 1
            elif c["interval"] < 30:
                buckets["young"] += 1
            else:
                buckets["mature"] += 1
        reviews = sum(len(c["history"]) for c in cards)
        good = sum(1 for c in cards for h in c["history"] if h["grade"] >= 3)
        print(f"{len(cards)} cards, {sum(1 for c in cards if c['due'] <= now)} due now")
        for name, n in buckets.items():
            print(f"  {name.ljust(9)} {n:>4}  {'█' * round(30 * n / len(cards))}")
        if reviews:
            print(f"\n{reviews} reviews, {good * 100 // reviews}% recalled, "
                  f"average ease {sum(c['ease'] for c in cards) / len(cards):.2f}")
        return 0

    if args.cmd == "forecast":
        counts = [0] * args.days
        for c in deck["cards"].values():
            day = int((c["due"] - time.time()) // DAY)
            if 0 <= day < args.days:
                counts[day] += 1
        peak = max(counts) or 1
        for i, n in enumerate(counts):
            label = (date.today() + timedelta(days=i)).strftime("%a %d %b")
            print(f"  {label}  {n:>3}  {'█' * round(28 * n / peak)}")
        return 0

    batch = due_cards(deck, args.limit, not args.no_new)
    if not batch:
        upcoming = min((c["due"] for c in deck["cards"].values()), default=None)
        when = datetime.fromtimestamp(upcoming).strftime("%a %d %b") if upcoming else "never"
        print(f"nothing due. next card: {when}")
        return 0

    correct = 0
    for n, (cid, card) in enumerate(batch, 1):
        state = "new" if card["reps"] == 0 else f"interval {card['interval']}d, ease {card['ease']:.2f}"
        print(f"\n[{n}/{len(batch)}]  {state}")
        print(wrap(card["q"]))
        if args.auto is None:
            input("\n  ... press enter for the answer")
        print("\n" + wrap(card["a"]))
        if args.auto is not None:
            grade = args.auto
        else:
            raw = input("\n  grade 0-5 (q to stop): ").strip()
            if raw.lower().startswith("q"):
                break
            grade = int(raw) if raw.isdigit() else 3
        schedule(card, max(0, min(5, grade)))
        correct += grade >= 3
        print(f"  -> next in {card['interval']}d (ease {card['ease']:.2f})")
    save(deck)
    print(f"\n{correct}/{len(batch)} recalled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
