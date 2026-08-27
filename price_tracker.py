#!/usr/bin/env python3
"""Scrape watched product prices on a schedule and notify only on a real drop.

    price_tracker.py add "https://shop.example.com/widget" --target 24.99
    price_tracker.py check
    price_tracker.py --demo

Price scraping without a real browser means finding the number in a pile of
HTML, and every store marks it up differently — a meta tag, a data attribute, a
naked "$24.99" near the word "price". This tries several extraction strategies
in order and keeps the first one that yields a plausible number, then only
alerts when the new price is BELOW both the previous recorded price and the
user's target — a price that merely fluctuates within "still too expensive"
should not fire a notification every single check.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

STORE_PATH = os.path.expanduser(os.environ.get("PRICE_TRACKER_STORE", "~/.price-tracker.json"))

# Extraction strategies, tried in order — most reliable first. Each returns a
# price in dollars or None. Real pages vary wildly; this is deliberately a chain
# of "try this, then that" rather than one clever regex that overfits to one site.
EXTRACTORS: list[tuple[str, re.Pattern]] = [
    ("json-ld price", re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d{2})?)"?')),
    ("meta og:price", re.compile(r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\'](\d+(?:\.\d{2})?)["\']', re.I)),
    ("itemprop price", re.compile(r'itemprop=["\']price["\'][^>]*content=["\'](\d+(?:\.\d{2})?)["\']', re.I)),
    ("data-price attr", re.compile(r'data-price=["\'](\d+(?:\.\d{2})?)["\']', re.I)),
    ("dollar near 'price'", re.compile(r'price[^$]{0,40}\$\s?(\d{1,5}(?:\.\d{2})?)', re.I)),
    ("bare dollar amount", re.compile(r'\$\s?(\d{1,5}\.\d{2})\b')),
]


def extract_price(html: str) -> tuple[float, str] | None:
    for name, pattern in EXTRACTORS:
        match = pattern.search(html)
        if match:
            value = float(match.group(1))
            if 0.01 <= value <= 100000:  # sanity bound: reject "$0.00" placeholders and typos
                return value, name
    return None


@dataclass
class Watch:
    url: str
    label: str
    target_price: float | None = None
    history: list[dict] = field(default_factory=list)  # [{"price": float, "at": iso-timestamp, "source": str}]

    @property
    def current_price(self) -> float | None:
        return self.history[-1]["price"] if self.history else None

    @property
    def lowest_price(self) -> float | None:
        return min((h["price"] for h in self.history), default=None)


@dataclass
class PriceDropAlert:
    label: str
    url: str
    old_price: float
    new_price: float
    target_price: float | None
    is_all_time_low: bool


def record_check(watch: Watch, price: float, source: str, at: float | None = None) -> PriceDropAlert | None:
    """Append a price observation; return an alert only for a genuine drop below both
    the previous price AND (if set) the user's target — never on a mere fluctuation."""
    previous_price = watch.current_price
    was_lowest = watch.lowest_price
    timestamp = datetime.fromtimestamp(at or time.time(), tz=timezone.utc).isoformat()
    watch.history.append({"price": price, "at": timestamp, "source": source})

    if previous_price is None:
        return None  # first observation ever: nothing to compare against

    dropped_from_previous = price < previous_price
    below_target = watch.target_price is None or price <= watch.target_price
    if dropped_from_previous and below_target:
        is_ath_low = was_lowest is None or price < was_lowest
        return PriceDropAlert(watch.label, watch.url, previous_price, price, watch.target_price, is_ath_low)
    return None


def format_alert(alert: PriceDropAlert) -> str:
    saved = alert.old_price - alert.new_price
    pct = saved / alert.old_price * 100
    lowest = "  (all-time low)" if alert.is_all_time_low else ""
    target_note = f", under your ${alert.target_price:.2f} target" if alert.target_price else ""
    return f"PRICE DROP  {alert.label}: ${alert.old_price:.2f} -> ${alert.new_price:.2f}  (-{pct:.0f}%, saved ${saved:.2f}{target_note}){lowest}"


def load_watches(path: str) -> list[Watch]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        raw = json.load(fh)
    watches = []
    for entry in raw:
        w = Watch(url=entry["url"], label=entry["label"], target_price=entry.get("target_price"))
        w.history = entry.get("history", [])
        watches.append(w)
    return watches


def save_watches(path: str, watches: list[Watch]) -> None:
    payload = [{"url": w.url, "label": w.label, "target_price": w.target_price, "history": w.history} for w in watches]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def fetch_html(url: str, timeout: float = 10.0) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (price-tracker)"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(1 << 20).decode("utf-8", errors="replace")


# ------------------------------------------------------------ demo

def demo() -> int:
    print("simulating a product being checked across several days, without a real network call\n")

    pages = [
        # day 1: first check, no comparison possible
        '<html><meta property="og:price:amount" content="49.99"><body>Widget Pro</body></html>',
        # day 2: unrelated price-looking noise near a review; the real price comes from a more reliable extractor
        '<html><meta property="og:price:amount" content="49.99"><body>"customers rated this $5 well spent"</body></html>',
        # day 3: a genuine markdown, still above target
        '<html><meta property="og:price:amount" content="39.99"><body>Widget Pro - on sale</body></html>',
        # day 4: price ticks back up slightly — should NOT alert, it's a rise not a drop
        '<html><meta property="og:price:amount" content="42.99"><body>Widget Pro</body></html>',
        # day 5: drops below the user's $30 target — should alert, and it's an all-time low
        '<html><meta property="og:price:amount" content="27.50"><body>Widget Pro - clearance</body></html>',
        # day 6: drops again but stays same-ish... a tiny further drop, still under target, still a new low
        '<html><meta property="og:price:amount" content="26.00"><body>Widget Pro</body></html>',
        # day 7: a site with no structured data at all — falls back to the bare-dollar extractor
        '<html><body><div class="product">Widget Pro — now just $22.00 while supplies last!</div></body></html>',
    ]

    watch = Watch(url="https://shop.example.com/widget-pro", label="Widget Pro", target_price=30.00)
    alerts = []

    for day, html in enumerate(pages, 1):
        found = extract_price(html)
        if found is None:
            print(f"  day {day}: could not extract a price")
            continue
        price, source = found
        alert = record_check(watch, price, source, at=1735689600 + day * 86400)
        marker = " <- ALERT" if alert else ""
        print(f"  day {day}: ${price:.2f}  (via {source}){marker}")
        if alert:
            alerts.append(alert)

    print(f"\n{len(alerts)} alerts fired out of {len(pages)} checks:\n")
    for alert in alerts:
        print(f"  {format_alert(alert)}")

    print(f"\nfull price history for {watch.label}:")
    for h in watch.history:
        print(f"  {h['at'][:10]}  ${h['price']:.2f}  ({h['source']})")
    print(f"\nlowest ever seen: ${watch.lowest_price:.2f}")

    print(f"\nnote: day 4's rise to $42.99 did not alert (it's a rise, not a drop), and day 6's")
    print(f"$26.00 alerted even though day 5 already alerted at $27.50 — each new all-time low")
    print(f"still under target gets its own notification, it isn't a one-shot per watch.")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    watches = load_watches(STORE_PATH)
    watches.append(Watch(url=args.url, label=args.label or args.url, target_price=args.target))
    save_watches(STORE_PATH, watches)
    print(f"watching {args.url}" + (f" (alert under ${args.target:.2f})" if args.target else ""))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    watches = load_watches(STORE_PATH)
    if not watches:
        print("no watches configured — use `add` first, or run --demo")
        return 0
    any_alert = False
    for watch in watches:
        try:
            html = fetch_html(watch.url)
        except Exception as exc:
            print(f"  {watch.label}: fetch failed ({exc})")
            continue
        found = extract_price(html)
        if not found:
            print(f"  {watch.label}: could not find a price on the page")
            continue
        price, source = found
        alert = record_check(watch, price, source)
        print(f"  {watch.label}: ${price:.2f} (via {source})")
        if alert:
            print(f"    {format_alert(alert)}")
            any_alert = True
    save_watches(STORE_PATH, watches)
    return 0 if not any_alert else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    add_p = sub.add_parser("add")
    add_p.add_argument("url")
    add_p.add_argument("--label")
    add_p.add_argument("--target", type=float)
    sub.add_parser("check")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "check":
        return cmd_check(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
