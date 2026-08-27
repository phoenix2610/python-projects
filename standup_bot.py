#!/usr/bin/env python3
"""Collect async standup updates on a schedule and post one formatted digest.

    standup_bot.py collect --hours 24
    standup_bot.py --demo

A synchronous "everyone talks in order" standup doesn't survive a distributed
team across time zones. This collects whatever updates people already posted
(to a channel, a form, wherever `--source` points) within the window, flags
whoever hasn't posted yet, and separately surfaces anyone who reported being
blocked — because "I'm blocked on X" buried in message 6 of 9 is the one line
that actually needed someone's attention today.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class RawUpdate:
    author: str
    text: str
    posted_at: datetime


@dataclass
class ParsedUpdate:
    author: str
    posted_at: datetime
    yesterday: str | None
    today: str | None
    blockers: list[str]
    raw_text: str


# Loosely structured standup format: "Yesterday: ... Today: ... Blockers: ..."
# on separate lines, in any order, any of the three optional. This is generous
# on purpose — people don't format consistently, and a bot that rejects
# malformed updates just trains people to stop posting them.
SECTION_RE = re.compile(
    r"^(yesterday|today|blocker[s]?|blocked on)\s*:?\s*(.*)$", re.I | re.M
)

# "Blockers: none" is someone answering the prompt, not reporting a blocker —
# without this, every all-clear update gets misfiled as an active blocker.
NO_BLOCKER_RE = re.compile(r"^(none|no|n/a|nothing|nope|all clear)[.!]?$", re.I)


def parse_update(raw: RawUpdate) -> ParsedUpdate:
    sections: dict[str, list[str]] = {"yesterday": [], "today": [], "blockers": []}
    current_key: str | None = None

    for line in raw.text.split("\n"):
        stripped = line.strip("-* \t")
        match = SECTION_RE.match(stripped)
        if match:
            label = match.group(1).lower()
            current_key = "blockers" if label.startswith("block") else label
            rest = match.group(2).strip()
            if rest:
                sections[current_key].append(rest)
        elif current_key and stripped:
            sections[current_key].append(stripped)

    # a message with no recognisable sections at all is treated as a single "today" note —
    # better than silently discarding someone's whole update because they didn't use headers
    if not any(sections.values()) and raw.text.strip():
        sections["today"].append(raw.text.strip())

    return ParsedUpdate(
        author=raw.author,
        posted_at=raw.posted_at,
        yesterday=" ".join(sections["yesterday"]) or None,
        today=" ".join(sections["today"]) or None,
        blockers=[b for b in sections["blockers"] if b and not NO_BLOCKER_RE.match(b.strip())],
        raw_text=raw.text,
    )


@dataclass
class StandupDigest:
    window_start: datetime
    window_end: datetime
    updates: list[ParsedUpdate]
    missing: list[str]

    @property
    def blocked_people(self) -> list[ParsedUpdate]:
        return [u for u in self.updates if u.blockers]


def build_digest(team: list[str], raw_updates: list[RawUpdate], window_hours: int, now: datetime | None = None) -> StandupDigest:
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    in_window = [u for u in raw_updates if window_start <= u.posted_at <= now]
    # keep only the LATEST update per author within the window — someone posting
    # a correction ("actually, also blocked on Y") shouldn't produce two entries
    latest_by_author: dict[str, RawUpdate] = {}
    for update in sorted(in_window, key=lambda u: u.posted_at):
        latest_by_author[update.author] = update

    parsed = [parse_update(u) for u in latest_by_author.values()]
    posted_authors = {u.author for u in parsed}
    missing = [person for person in team if person not in posted_authors]

    return StandupDigest(window_start, now, parsed, missing)


def format_digest(digest: StandupDigest) -> str:
    lines = [f"Standup Digest — {digest.window_end.strftime('%A, %B %d')}", ""]

    if digest.blocked_people:
        lines.append(f"BLOCKED ({len(digest.blocked_people)}):")
        for update in digest.blocked_people:
            lines.append(f"  {update.author}:")
            for blocker in update.blockers:
                lines.append(f"    - {blocker}")
        lines.append("")

    lines.append(f"Updates ({len(digest.updates)}):")
    for update in sorted(digest.updates, key=lambda u: u.author):
        lines.append(f"\n  {update.author}  ({update.posted_at.strftime('%H:%M UTC')})")
        if update.yesterday:
            lines.append(f"    yesterday: {update.yesterday}")
        if update.today:
            lines.append(f"    today: {update.today}")
        if not update.yesterday and not update.today and not update.blockers:
            lines.append(f"    (unstructured update, posted as-is)")

    if digest.missing:
        lines.append(f"\nNo update yet ({len(digest.missing)}): {', '.join(digest.missing)}")

    return "\n".join(lines)


def load_updates_from_json(path: str) -> list[RawUpdate]:
    with open(path) as fh:
        raw = json.load(fh)
    return [RawUpdate(author=e["author"], text=e["text"], posted_at=datetime.fromisoformat(e["posted_at"])) for e in raw]


# ------------------------------------------------------------ demo

def demo() -> int:
    team = ["ana", "bo", "cy", "dee", "eli"]
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)

    updates = [
        RawUpdate("ana", "Yesterday: shipped the retry logic\nToday: writing tests for it\nBlockers: none", now - timedelta(hours=20)),
        RawUpdate("bo", "yesterday: reviewed PRs\ntoday: starting the migration script\nblocked on: need prod DB read access from Dee", now - timedelta(hours=18)),
        RawUpdate("cy", "just finishing up the onboarding doc, no blockers", now - timedelta(hours=15)),  # unstructured
        RawUpdate("dee", "Yesterday: on call, handled 2 incidents\nToday: catching up on the backlog", now - timedelta(hours=10)),
        # bo posts a correction later — should REPLACE the earlier update, not duplicate it
        RawUpdate("bo", "Yesterday: reviewed PRs\nToday: migration script AND fixing the flaky test\nBlockers: still need prod DB access from Dee, and CI is red on main", now - timedelta(hours=3)),
        # a stale update from 2 days ago — outside the 24h window, should not appear
        RawUpdate("eli", "Yesterday: old update from before vacation", now - timedelta(hours=50)),
    ]

    digest = build_digest(team, updates, window_hours=24, now=now)
    print(format_digest(digest))

    print("\n\nnote: bo posted twice — the digest shows only the LATER update (with the added")
    print("CI blocker), not both. eli's team member has no update in the last 24h (their only")
    print("message was 50h old) and correctly appears under 'No update yet', distinct from cy")
    print("who posted something unstructured and still got included rather than dropped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    collect_p = sub.add_parser("collect")
    collect_p.add_argument("--source", help="JSON file of {author, text, posted_at} entries")
    collect_p.add_argument("--team", help="comma-separated team member names")
    collect_p.add_argument("--hours", type=int, default=24)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()

    if args.cmd == "collect":
        if not args.source or not args.team:
            print("--source and --team are required for `collect`", flush=True)
            return 1
        team = args.team.split(",")
        raw_updates = load_updates_from_json(args.source)
        digest = build_digest(team, raw_updates, args.hours)
        print(format_digest(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
