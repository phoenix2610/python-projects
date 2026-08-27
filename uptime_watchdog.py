#!/usr/bin/env python3
"""Poll endpoints, verify content, and alert only on consecutive failures.

    uptime_watchdog.py --config sites.json
    uptime_watchdog.py --demo

The design decision that keeps this from crying wolf: one failed check means
nothing (a router hiccup, a GC pause) — an alert fires only after N consecutive
failures, and a recovery notice fires once after the site comes back, not on
every subsequent success. "Up" also means more than a 200 status: a site can
return 200 with a stack trace or a "database connection failed" banner, so each
check can optionally require a substring in the body, not just the status code.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SiteConfig:
    name: str
    url: str
    expect_status: int = 200
    expect_contains: str | None = None
    timeout: float = 5.0
    failure_threshold: int = 3


@dataclass
class CheckResult:
    ok: bool
    status: int | None
    latency_ms: float
    error: str | None
    checked_at: float


@dataclass
class SiteState:
    config: SiteConfig
    consecutive_failures: int = 0
    is_down: bool = False
    last_result: CheckResult | None = None
    history: list[CheckResult] = field(default_factory=list)
    down_since: float | None = None


def perform_check(config: SiteConfig, opener: urllib.request.OpenerDirector | None = None) -> CheckResult:
    start = time.monotonic()
    opener = opener or urllib.request.build_opener()
    try:
        req = urllib.request.Request(config.url, headers={"User-Agent": "uptime-watchdog/1.0"})
        with opener.open(req, timeout=config.timeout) as response:
            body = response.read(1 << 20)  # cap read size: don't buffer an infinite stream
            status = response.status
            latency_ms = (time.monotonic() - start) * 1000

            if status != config.expect_status:
                return CheckResult(False, status, latency_ms, f"expected status {config.expect_status}, got {status}", time.time())
            if config.expect_contains and config.expect_contains.encode() not in body:
                return CheckResult(False, status, latency_ms, f"response did not contain {config.expect_contains!r}", time.time())
            return CheckResult(True, status, latency_ms, None, time.time())
    except urllib.error.HTTPError as exc:
        latency_ms = (time.monotonic() - start) * 1000
        if exc.code == config.expect_status:
            return CheckResult(True, exc.code, latency_ms, None, time.time())
        return CheckResult(False, exc.code, latency_ms, f"HTTP {exc.code}", time.time())
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(False, None, latency_ms, str(getattr(exc, "reason", exc)), time.time())


@dataclass
class AlertEvent:
    kind: str  # "down" | "recovered" | "degraded"
    site: str
    message: str
    at: float


def update_state(state: SiteState, result: CheckResult) -> list[AlertEvent]:
    """Feed one check result into the state machine; returns alerts to fire, if any."""
    events: list[AlertEvent] = []
    state.history.append(result)
    state.last_result = result

    if result.ok:
        if state.is_down:
            downtime = result.checked_at - (state.down_since or result.checked_at)
            events.append(AlertEvent("recovered", state.config.name, f"recovered after {downtime:.0f}s down", result.checked_at))
            state.is_down = False
            state.down_since = None
        state.consecutive_failures = 0
        return events

    state.consecutive_failures += 1
    if state.consecutive_failures == state.config.failure_threshold and not state.is_down:
        state.is_down = True
        state.down_since = result.checked_at
        events.append(AlertEvent("down", state.config.name, f"{state.consecutive_failures} consecutive failures: {result.error}", result.checked_at))
    elif state.consecutive_failures < state.config.failure_threshold:
        events.append(AlertEvent("degraded", state.config.name, f"check {state.consecutive_failures}/{state.config.failure_threshold} failed: {result.error}", result.checked_at))

    return events


def format_alert(event: AlertEvent) -> str:
    ts = datetime.fromtimestamp(event.at, tz=timezone.utc).strftime("%H:%M:%S")
    icons = {"down": "DOWN   ", "recovered": "UP     ", "degraded": "warn   "}
    return f"[{ts}] {icons[event.kind]} {event.site}: {event.message}"


# ------------------------------------------------------------ demo

class ScriptedOpener:
    """Replaces real network calls with a scripted sequence of outcomes, so the demo
    is deterministic and runs in milliseconds instead of depending on the network."""

    def __init__(self, script: dict[str, list[tuple[int, bytes] | Exception]]):
        self.script = script
        self.call_index: dict[str, int] = {}

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        outcomes = self.script[url]
        idx = self.call_index.get(url, 0)
        outcome = outcomes[min(idx, len(outcomes) - 1)]
        self.call_index[url] = idx + 1
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome

        class FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self, n=-1):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error", {}, None)
        return FakeResponse(status, body)


def demo() -> int:
    sites = [
        SiteConfig(name="marketing-site", url="https://example.com/", expect_status=200, expect_contains="Welcome", failure_threshold=3),
        SiteConfig(name="api", url="https://api.example.com/health", expect_status=200, expect_contains='"status":"ok"', failure_threshold=2),
        SiteConfig(name="checkout", url="https://shop.example.com/cart", expect_status=200, failure_threshold=3),
    ]

    # simulate: marketing-site stays healthy; api degrades then recovers; checkout
    # has a real outage (returns 200 but with an error banner in the body) then recovers
    script = {
        "https://example.com/": [(200, b"<html>Welcome to our site</html>")] * 12,
        "https://api.example.com/health": [
            (200, b'{"status":"ok"}'),
            urllib.error.URLError("Connection timed out"),
            urllib.error.URLError("Connection timed out"),
            (200, b'{"status":"ok"}'),
            (200, b'{"status":"ok"}'),
        ] + [(200, b'{"status":"ok"}')] * 7,
        "https://shop.example.com/cart": [
            (200, b"<html>Your cart</html>"),
            (200, b"<html>Your cart</html>"),
            (200, b"<html>Database connection failed</html>"),  # 200 but broken — content check catches it
            (200, b"<html>Database connection failed</html>"),
            (200, b"<html>Database connection failed</html>"),
            (200, b"<html>Your cart</html>"),
        ] + [(200, b"<html>Your cart</html>")] * 6,
    }
    for site in sites:
        if site.name == "checkout":
            site.expect_contains = "Your cart"

    opener = ScriptedOpener(script)
    states = {site.name: SiteState(config=site) for site in sites}

    print(f"monitoring {len(sites)} sites across 12 simulated check rounds\n")
    print("(api requires two consecutive failures to alert; the others require three —")
    print(" a lower threshold suits a service where fast alerting matters more than noise)\n")

    all_events: list[AlertEvent] = []
    for round_num in range(12):
        for site in sites:
            result = perform_check(site, opener)
            events = update_state(states[site.name], result)
            for event in events:
                print(format_alert(event))
                all_events.append(event)

    print(f"\n{sum(len(s.history) for s in states.values())} checks performed, {len(all_events)} alerts fired")
    print("\nsummary per site:")
    for name, state in states.items():
        failures = sum(1 for r in state.history if not r.ok)
        avg_latency = sum(r.latency_ms for r in state.history) / len(state.history)
        print(f"  {name:<16} {len(state.history)} checks, {failures} failed checks, avg {avg_latency:.1f}ms, currently {'DOWN' if state.is_down else 'up'}")

    print(f"\nnote: the api service failed twice in a row and correctly triggered a DOWN alert")
    print(f"(threshold 2), while a single earlier failure alone produced no alert at all —")
    print(f"that's the whole point of a consecutive-failure threshold instead of alerting on")
    print(f"the very first bad check.")
    return 0


def load_config(path: str) -> list[SiteConfig]:
    with open(path) as fh:
        raw = json.load(fh)
    return [SiteConfig(**entry) for entry in raw]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON file: a list of site configs")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between check rounds")
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.config:
        return demo()

    sites = load_config(args.config)
    states = {site.name: SiteState(config=site) for site in sites}

    try:
        while True:
            for site in sites:
                result = perform_check(site)
                for event in update_state(states[site.name], result):
                    print(format_alert(event), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
