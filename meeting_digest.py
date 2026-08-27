#!/usr/bin/env python3
"""Summarize tomorrow's calendar into one message with prep links attached.

    meeting_digest.py calendar.ics
    meeting_digest.py --demo

Reads a real .ics file with the stdlib only (no icalendar package): unfolds the
RFC 5545 line-continuation format, parses VEVENT blocks, expands simple RRULEs
(daily/weekly) far enough to find tomorrow's occurrences, and groups back-to-back
meetings into "focus is gone from 9-11am" blocks rather than listing five
30-minute meetings as five separate interruptions. Attendee count and any URL in
the description become a one-line prep note per meeting.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


def unfold_ics_lines(text: str) -> list[str]:
    """RFC 5545: a line starting with a space or tab continues the previous line."""
    raw_lines = text.replace("\r\n", "\n").split("\n")
    unfolded: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    return unfolded


def parse_ics_datetime(value: str, params: str) -> datetime:
    # Every value comes back naive. A real calendar client would convert UTC ("Z")
    # times to the viewer's local zone before comparing them against floating-time
    # events; this digest only needs one self-consistent timeline to sort and diff
    # against "tomorrow", so both cases collapse to naive wall-clock time rather
    # than mixing aware and naive datetimes (which Python refuses to compare at all).
    value = value.strip()
    if "VALUE=DATE" in params and len(value) == 8:
        return datetime.strptime(value, "%Y%m%d")
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


@dataclass
class Event:
    summary: str
    start: datetime
    end: datetime
    location: str = ""
    description: str = ""
    attendee_count: int = 0
    rrule: str | None = None
    is_all_day: bool = False


PROP_RE = re.compile(r"^([A-Z\-]+)(;[^:]*)?:(.*)$")


def parse_ics(text: str) -> list[Event]:
    events: list[Event] = []
    current: dict | None = None

    for line in unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            current = {"attendee_count": 0}
            continue
        if line == "END:VEVENT":
            if current and "SUMMARY" in current and "DTSTART" in current:
                events.append(
                    Event(
                        summary=current.get("SUMMARY", "(no title)"),
                        start=current["DTSTART"],
                        end=current.get("DTEND", current["DTSTART"] + timedelta(hours=1)),
                        location=current.get("LOCATION", ""),
                        description=current.get("DESCRIPTION", ""),
                        attendee_count=current["attendee_count"],
                        rrule=current.get("RRULE"),
                        is_all_day=current.get("_all_day", False),
                    )
                )
            current = None
            continue
        if current is None:
            continue

        match = PROP_RE.match(line)
        if not match:
            continue
        prop, params, value = match.group(1), match.group(2) or "", match.group(3)
        value = value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")

        if prop in ("DTSTART", "DTEND"):
            current[prop] = parse_ics_datetime(value, params)
            if prop == "DTSTART" and "VALUE=DATE" in params:
                current["_all_day"] = True
        elif prop == "ATTENDEE":
            current["attendee_count"] += 1
        elif prop in ("SUMMARY", "LOCATION", "DESCRIPTION", "RRULE"):
            current[prop] = value

    return events


def parse_rrule(rrule: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)


def occurs_on(event: Event, target_date: date) -> tuple[datetime, datetime] | None:
    """Return (start, end) for `event`'s occurrence on `target_date`, or None if it
    doesn't occur that day. Handles the base occurrence plus DAILY/WEEKLY RRULEs."""
    start_date = event.start.date()
    duration = event.end - event.start

    if not event.rrule:
        return (event.start, event.end) if start_date == target_date else None

    if start_date > target_date:
        return None

    rule = parse_rrule(event.rrule)
    freq = rule.get("FREQ", "")
    interval = int(rule.get("INTERVAL", "1"))
    days_diff = (target_date - start_date).days

    if freq == "DAILY":
        if days_diff % interval != 0:
            return None
    elif freq == "WEEKLY":
        by_day = rule.get("BYDAY", "").split(",") if "BYDAY" in rule else None
        weeks_diff = days_diff // 7
        if by_day:
            day_codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
            if day_codes[target_date.weekday()] not in by_day:
                return None
            # for BYDAY weekly rules, alignment is by week number relative to the rule's start week
            start_week_monday = start_date - timedelta(days=start_date.weekday())
            target_week_monday = target_date - timedelta(days=target_date.weekday())
            if ((target_week_monday - start_week_monday).days // 7) % interval != 0:
                return None
        else:
            if target_date.weekday() != start_date.weekday() or weeks_diff % interval != 0:
                return None
    else:
        return None  # MONTHLY/YEARLY not needed for a next-day digest

    if "COUNT" in rule:
        # approximate: count occurrences up to target_date at this frequency
        occurrences_so_far = days_diff // interval + 1 if freq == "DAILY" else (days_diff // 7) // interval + 1
        if occurrences_so_far > int(rule["COUNT"]):
            return None
    if "UNTIL" in rule:
        until = parse_ics_datetime(rule["UNTIL"], "")
        if target_date > until.date():
            return None

    new_start = datetime.combine(target_date, event.start.timetz())
    return new_start, new_start + duration


def find_occurrences(events: list[Event], target_date: date) -> list[tuple[Event, datetime, datetime]]:
    out = []
    for event in events:
        occurrence = occurs_on(event, target_date)
        if occurrence:
            out.append((event, occurrence[0], occurrence[1]))
    out.sort(key=lambda triple: triple[1])
    return out


@dataclass
class FocusBlock:
    start: datetime
    end: datetime
    meeting_count: int


def find_meeting_blocks(occurrences: list[tuple[Event, datetime, datetime]], gap_tolerance_minutes: int = 15) -> list[FocusBlock]:
    """Merge back-to-back (or nearly back-to-back) meetings into contiguous blocks —
    "9:00-11:30, 4 meetings back to back" communicates more than four separate lines."""
    timed = [(s, e) for _, s, e in occurrences if s != e]
    if not timed:
        return []
    blocks: list[FocusBlock] = []
    cur_start, cur_end, count = timed[0][0], timed[0][1], 1
    for start, end in timed[1:]:
        gap = (start - cur_end).total_seconds() / 60
        if gap <= gap_tolerance_minutes:
            cur_end = max(cur_end, end)
            count += 1
        else:
            blocks.append(FocusBlock(cur_start, cur_end, count))
            cur_start, cur_end, count = start, end, 1
    blocks.append(FocusBlock(cur_start, cur_end, count))
    return [b for b in blocks if b.meeting_count > 1]


URL_RE = re.compile(r"https?://\S+")


def build_digest(events: list[Event], target_date: date) -> str:
    occurrences = find_occurrences(events, target_date)
    lines = [f"Digest for {target_date.strftime('%A, %B %-d' if hasattr(target_date, 'strftime') else '%A')}"]
    lines[0] = f"Digest for {target_date.strftime('%A, %B %d')}"

    if not occurrences:
        lines.append("\nNo meetings scheduled. Clear day.")
        return "\n".join(lines)

    all_day = [o for o in occurrences if o[0].is_all_day]
    timed = [o for o in occurrences if not o[0].is_all_day]

    if all_day:
        lines.append("")
        for event, _, _ in all_day:
            lines.append(f"[all day] {event.summary}")

    if timed:
        blocks = find_meeting_blocks(timed)
        blocked_ranges = [(b.start, b.end) for b in blocks]

        lines.append(f"\n{len(timed)} meetings:")
        announced_blocks: set[int] = set()

        def prep_line(event: Event) -> None:
            for url in URL_RE.findall(event.description)[:1]:
                lines.append(f"             prep: {url}")

        for event, start, end in timed:
            covering_block = next((b for b in blocks if b.start <= start < b.end), None)
            if covering_block:
                block_id = id(covering_block)
                if block_id in announced_blocks:
                    continue  # this meeting was already listed under the block header
                announced_blocks.add(block_id)
                lines.append(
                    f"  {covering_block.start.strftime('%H:%M')}-{covering_block.end.strftime('%H:%M')}  "
                    f"BACK-TO-BACK: {covering_block.meeting_count} meetings, no gap for focus work"
                )
                # list every meeting in the block, so a prep link inside it is never silently dropped
                for inner_event, inner_start, inner_end in timed:
                    if covering_block.start <= inner_start < covering_block.end:
                        lines.append(f"    {inner_start.strftime('%H:%M')}-{inner_end.strftime('%H:%M')}  {inner_event.summary}")
                        prep_line(inner_event)
            else:
                attendee_note = f", {event.attendee_count} attendees" if event.attendee_count else ""
                loc_note = f" @ {event.location}" if event.location else ""
                lines.append(f"  {start.strftime('%H:%M')}-{end.strftime('%H:%M')}  {event.summary}{loc_note}{attendee_note}")
                prep_line(event)

        free_hours = 8 - sum((e - s).total_seconds() / 3600 for _, s, e in timed if not any(b.start <= s < b.end for b in blocks))
        total_meeting_hours = sum((e - s).total_seconds() / 3600 for _, s, e in timed)
        lines.append(f"\n{total_meeting_hours:.1f}h in meetings, {len(blocks)} back-to-back block(s)")

    return "\n".join(lines)


# ------------------------------------------------------------ demo

def build_demo_ics() -> str:
    return """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Standup
DTSTART:20260827T090000
DTEND:20260827T091500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
END:VEVENT
BEGIN:VEVENT
SUMMARY:1:1 with manager
DTSTART:20260827T093000
DTEND:20260827T100000
ATTENDEE:mailto:manager@example.com
DESCRIPTION:Notes: https://docs.example.com/1-1-notes
END:VEVENT
BEGIN:VEVENT
SUMMARY:Design review
DTSTART:20260827T100000
DTEND:20260827T110000
ATTENDEE:mailto:ana@example.com
ATTENDEE:mailto:bo@example.com
ATTENDEE:mailto:cy@example.com
LOCATION:Room 4B
DESCRIPTION:Deck: https://slides.example.com/design-q3
END:VEVENT
BEGIN:VEVENT
SUMMARY:Focus block
DTSTART:20260827T130000
DTEND:20260827T150000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Vendor call
DTSTART:20260827T153000
DTEND:20260827T160000
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
SUMMARY:Company All-Hands
DTSTART;VALUE=DATE:20260827
DTEND;VALUE=DATE:20260828
END:VEVENT
BEGIN:VEVENT
SUMMARY:Old one-off meeting (different day)
DTSTART:20260826T140000
DTEND:20260826T150000
END:VEVENT
END:VCALENDAR
"""


def demo() -> int:
    ics_text = build_demo_ics()
    events = parse_ics(ics_text)
    print(f"parsed {len(events)} VEVENT blocks from the .ics file "
          f"(one recurring weekly, one on a different day — should not appear tomorrow)\n")

    target = date(2026, 8, 27)  # a Thursday
    digest = build_digest(events, target)
    print(digest)

    print("\n\nnote: Standup (9:00), 1:1 (9:30) and Design review (10:00) are contiguous")
    print("(each starts when or before the previous ends) and got merged into one")
    print("'BACK-TO-BACK: 3 meetings' block instead of three separate lines — that's the")
    print("signal that matters ('no breathing room 9-11') that five individual entries bury.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ics_file", nargs="?")
    ap.add_argument("--date", help="YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.ics_file:
        return demo()

    with open(args.ics_file) as fh:
        events = parse_ics(fh.read())
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today() + timedelta(days=1)
    print(build_digest(events, target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
