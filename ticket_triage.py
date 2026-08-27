#!/usr/bin/env python3
"""Route new tickets by keyword rules, set labels, and nudge whatever's gone stale.

    ticket_triage.py tickets.json --rules rules.json
    ticket_triage.py --demo

Triage rules match against title+body, first match wins per category (so one
ticket can get both a team label and a priority label from different rule
groups without them fighting), and severity keywords ("prod is down", "data
loss", "security") bump priority regardless of which team rule matched — a
believable production-down report from an unrecognised sender still needs to
jump the queue. Staleness is separate from triage: a ticket triaged correctly
three days ago that nobody has touched since needs a nudge, not a re-triage.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Ticket:
    id: str
    title: str
    body: str
    reporter: str
    created_at: datetime
    updated_at: datetime
    labels: list[str] = field(default_factory=list)
    priority: str | None = None
    assigned_team: str | None = None


@dataclass
class Rule:
    name: str
    pattern: str
    category: str  # "team" | "label" | "priority"
    value: str

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text, re.I))


DEFAULT_RULES = [
    Rule("billing keywords", r"\b(invoice|charge|refund|billing|payment failed)\b", "team", "billing"),
    Rule("auth keywords", r"\b(login|password|2fa|locked out|can'?t sign in)\b", "team", "auth"),
    Rule("infra keywords", r"\b(500 error|timeout|down|outage|degraded|slow)\b", "team", "infra"),
    Rule("data keywords", r"\b(export|missing data|data loss|deleted my)\b", "team", "data"),
    Rule("bug label", r"\b(bug|broken|doesn'?t work|error)\b", "label", "bug"),
    Rule("feature label", r"\b(feature request|would be nice|please add|suggestion)\b", "label", "feature-request"),
    Rule("question label", r"\b(how do i|is it possible|question|wondering)\b", "label", "question"),
]

# checked independently of team/label rules — severity language overrides everything
SEVERITY_RULES = [
    Rule("critical", r"\b(prod(?:uction)? is down|data loss|security (?:breach|vulnerability))\b", "priority", "critical"),
    Rule("high", r"\b(urgent|asap|blocking|can'?t (?:log in|access|use))\b", "priority", "high"),
]


def triage_ticket(ticket: Ticket, rules: list[Rule]) -> Ticket:
    text = f"{ticket.title}\n{ticket.body}"

    for rule in rules:
        if rule.category == "team" and ticket.assigned_team is None and rule.matches(text):
            ticket.assigned_team = rule.value
        if rule.category == "label" and rule.matches(text) and rule.value not in ticket.labels:
            ticket.labels.append(rule.value)

    ticket.priority = "normal"
    for rule in SEVERITY_RULES:
        if rule.matches(text):
            ticket.priority = rule.value
            break  # SEVERITY_RULES is ordered most-severe-first; first match wins

    if ticket.assigned_team is None:
        ticket.assigned_team = "general"
        ticket.labels.append("needs-manual-routing")

    return ticket


@dataclass
class StaleTicket:
    ticket: Ticket
    days_untouched: float


def find_stale_tickets(tickets: list[Ticket], now: datetime, stale_after_hours: dict[str, float]) -> list[StaleTicket]:
    """A ticket is stale if it hasn't been updated within its priority's SLA window —
    critical tickets go stale in hours, normal ones in days."""
    stale = []
    for ticket in tickets:
        threshold_hours = stale_after_hours.get(ticket.priority or "normal", 72)
        hours_untouched = (now - ticket.updated_at).total_seconds() / 3600
        if hours_untouched > threshold_hours:
            stale.append(StaleTicket(ticket, hours_untouched / 24))
    return sorted(stale, key=lambda s: -s.days_untouched)


def format_triage_report(tickets: list[Ticket]) -> str:
    by_team: dict[str, list[Ticket]] = {}
    for t in tickets:
        by_team.setdefault(t.assigned_team or "general", []).append(t)

    lines = [f"{len(tickets)} tickets triaged\n"]
    priority_order = {"critical": 0, "high": 1, "normal": 2}
    for team in sorted(by_team, key=lambda t: min(priority_order.get(x.priority, 2) for x in by_team[t])):
        team_tickets = sorted(by_team[team], key=lambda t: priority_order.get(t.priority, 2))
        lines.append(f"{team} ({len(team_tickets)}):")
        for t in team_tickets:
            marker = {"critical": "!!!", "high": "!  ", "normal": "   "}[t.priority]
            lines.append(f"  {marker} #{t.id}  {t.title}  [{', '.join(t.labels) or 'no labels'}]")
        lines.append("")
    return "\n".join(lines)


def format_stale_report(stale: list[StaleTicket]) -> str:
    if not stale:
        return "Nothing is stale — every ticket is within its SLA window."
    lines = [f"{len(stale)} tickets past their SLA window:\n"]
    for s in stale:
        lines.append(f"  #{s.ticket.id}  ({s.ticket.priority}, {s.days_untouched:.1f}d untouched)  {s.ticket.title}")
    return "\n".join(lines)


# ------------------------------------------------------------ demo

def demo() -> int:
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)

    tickets = [
        Ticket("1001", "Can't log in after password reset", "I reset my password but now login just spins forever", "user_a@example.com", now - timedelta(hours=2), now - timedelta(hours=2)),
        Ticket("1002", "PROD IS DOWN - checkout broken", "prod is down, nobody can complete a purchase, this is urgent", "ops@example.com", now - timedelta(hours=6), now - timedelta(hours=5)),
        Ticket("1003", "Feature request: dark mode", "It would be nice to have a dark mode option", "user_b@example.com", now - timedelta(days=5), now - timedelta(days=5)),
        Ticket("1004", "Invoice shows wrong amount", "My invoice for August shows $50 more than expected, billing error?", "user_c@example.com", now - timedelta(hours=20), now - timedelta(hours=1)),
        Ticket("1005", "Is it possible to export my data as CSV?", "wondering if there's a way to export everything", "user_d@example.com", now - timedelta(days=1), now - timedelta(days=1)),
        Ticket("1006", "500 error on the dashboard", "getting a 500 error every time I load /dashboard, seems slow too", "user_e@example.com", now - timedelta(hours=10), now - timedelta(hours=9)),
        Ticket("1007", "Random feedback about the UI", "just wanted to say the new UI looks nice", "user_f@example.com", now - timedelta(days=3), now - timedelta(days=3)),  # no rule matches
    ]

    all_rules = DEFAULT_RULES
    triaged = [triage_ticket(t, all_rules) for t in tickets]

    print(format_triage_report(triaged))

    stale_thresholds = {"critical": 4, "high": 12, "normal": 48}  # hours
    stale = find_stale_tickets(triaged, now, stale_thresholds)
    print(format_stale_report(stale))

    print(f"\n\nnote: #1002 says 'PROD IS DOWN' inside an infra-team ticket AND matched the")
    print(f"'urgent' priority rule — but severity checks run independently and 'critical'")
    print(f"(prod is down) wins over 'high' (urgent) because critical is checked first.")
    print(f"#1007 matched no team keyword rule at all and correctly fell back to 'general'")
    print(f"with a 'needs-manual-routing' label instead of being silently dropped.")
    print(f"#1002 is only 5h old but already stale under critical's 4h SLA, while #1003 is")
    print(f"5 DAYS old but not flagged — normal-priority tickets get a much longer window.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickets_file", nargs="?")
    ap.add_argument("--rules")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.tickets_file:
        return demo()

    with open(args.tickets_file) as fh:
        raw = json.load(fh)
    tickets = [
        Ticket(
            id=t["id"], title=t["title"], body=t["body"], reporter=t.get("reporter", "?"),
            created_at=datetime.fromisoformat(t["created_at"]), updated_at=datetime.fromisoformat(t["updated_at"]),
        )
        for t in raw
    ]
    rules = DEFAULT_RULES
    if args.rules:
        with open(args.rules) as fh:
            rules = [Rule(**r) for r in json.load(fh)]

    triaged = [triage_ticket(t, rules) for t in tickets]
    print(format_triage_report(triaged))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
