#!/usr/bin/env python3
"""Token-bucket rate limiting with the headers clients actually need to back off.

    ratelimit.py --demo
    ratelimit.py serve --port 8099 --rate 5 --burst 10

A bucket refills continuously (no ticker thread, no fixed windows) so a caller who
has been quiet can spend a burst, and a steady caller gets exactly `rate` per
second. Rejections carry RateLimit-Remaining, RateLimit-Reset and Retry-After —
without those a 429 just tells a client to guess.
"""
from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Bucket:
    rate: float                       # tokens added per second
    burst: float                      # bucket capacity
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.tokens = self.burst

    def take(self, cost: float = 1.0, now: float | None = None) -> tuple[bool, float]:
        """Return (allowed, seconds until `cost` tokens exist)."""
        now = now if now is not None else time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        return False, (cost - self.tokens) / self.rate


class Limiter:
    """Per-key buckets with lazy eviction — idle keys cost nothing to keep."""

    def __init__(self, rate: float, burst: float, idle_ttl: float = 600.0):
        self.rate, self.burst, self.idle_ttl = rate, burst, idle_ttl
        self.buckets: dict[str, Bucket] = {}
        self.lock = threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, dict]:
        now = time.monotonic()
        with self.lock:
            if len(self.buckets) > 4096:
                self._evict(now)
            bucket = self.buckets.get(key)
            if bucket is None:
                bucket = self.buckets[key] = Bucket(self.rate, self.burst)
            allowed, wait = bucket.take(cost, now)
            headers = {
                "RateLimit-Limit": f"{self.burst:g}",
                "RateLimit-Remaining": f"{int(bucket.tokens)}",
                "RateLimit-Reset": f"{(self.burst - bucket.tokens) / self.rate:.0f}",
                "RateLimit-Policy": f"{self.burst:g};w={self.burst / self.rate:.0f}",
            }
            if not allowed:
                headers["Retry-After"] = f"{max(1, round(wait))}"
            return allowed, headers

    def _evict(self, now: float) -> None:
        stale = [k for k, b in self.buckets.items() if now - b.updated > self.idle_ttl]
        for key in stale:
            del self.buckets[key]


def client_key(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    return (handler.headers.get("Authorization") or forwarded.split(",")[0].strip()
            or handler.client_address[0])


def make_handler(limiter: Limiter):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            allowed, headers = limiter.check(client_key(self))
            body = (b'{"ok":true}' if allowed
                    else b'{"error":"rate limited","retry_after":' + headers["Retry-After"].encode() + b"}")
            self.send_response(200 if allowed else 429)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def demo() -> int:
    print("burst then steady state — 5/sec, burst of 10\n")
    limiter = Limiter(rate=5, burst=10)
    allowed = 0
    for i in range(15):
        ok, headers = limiter.check("ana")
        allowed += ok
        print(f"  request {i + 1:>2}: {'allow' if ok else 'DENY '}  "
              f"remaining={headers['RateLimit-Remaining']}"
              + (f"  retry after {headers['Retry-After']}s" if not ok else ""))
    print(f"\n  {allowed}/15 immediately allowed (the burst), rest deferred")

    print("\n  waiting 1s for the bucket to refill...")
    time.sleep(1)
    ok, headers = limiter.check("ana")
    print(f"  after 1s: {'allow' if ok else 'deny'}, remaining={headers['RateLimit-Remaining']} "
          f"(refilled ~5 tokens)")

    print("\nkeys are independent")
    for key in ("ana", "bo", "cy"):
        ok, h = limiter.check(key)
        print(f"  {key}: {'allow' if ok else 'deny'}  remaining={h['RateLimit-Remaining']}")

    print("\nsustained rate over 2 seconds of hammering (should converge on 5/sec)")
    limiter = Limiter(rate=5, burst=10)
    start = time.monotonic()
    granted = attempts = 0
    while time.monotonic() - start < 2.0:
        ok, _ = limiter.check("hammer")
        granted += ok
        attempts += 1
        time.sleep(0.002)
    elapsed = time.monotonic() - start
    print(f"  {attempts} attempts, {granted} allowed in {elapsed:.1f}s "
          f"= {granted / elapsed:.1f}/sec (burst included)")

    print("\nvariable cost: a heavy call can spend more than one token")
    limiter = Limiter(rate=10, burst=20)
    for cost in (5, 5, 5, 5, 5):
        ok, h = limiter.check("bulk", cost=cost)
        print(f"  cost {cost}: {'allow' if ok else 'DENY '}  remaining={h['RateLimit-Remaining']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["serve"], default=None)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--rate", type=float, default=5)
    ap.add_argument("--burst", type=float, default=10)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if not args.cmd or args.demo:
        return demo()
    limiter = Limiter(args.rate, args.burst)
    print(f"limiting {args.rate}/s (burst {args.burst}) on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(limiter)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
