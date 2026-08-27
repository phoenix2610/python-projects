#!/usr/bin/env python3
"""A connection pool that behaves under load: checkout timeouts, health checks, idle reaping.

    pool.py --demo

The failure modes this exists to prevent, in order of how often they bite:
a caller blocking forever when the pool is exhausted (bounded wait + a clear
error), handing out a connection the server closed while it sat idle (validate on
checkout, cheaply), leaking a connection when the caller raises (context manager),
and holding 200 idle sockets open all night (reaper thread with a floor).
"""
from __future__ import annotations

import argparse
import itertools
import queue
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


class PoolExhausted(RuntimeError):
    pass


@dataclass
class Stats:
    created: int = 0
    closed: int = 0
    checkouts: int = 0
    waits: int = 0
    timeouts: int = 0
    validation_failures: int = 0
    reaped: int = 0
    wait_time: float = 0.0

    def report(self, pool: "ConnectionPool") -> str:
        avg = (self.wait_time / self.waits * 1000) if self.waits else 0
        return (f"created={self.created} closed={self.closed} checkouts={self.checkouts} "
                f"waited={self.waits} (avg {avg:.1f}ms) timeouts={self.timeouts} "
                f"stale={self.validation_failures} reaped={self.reaped} "
                f"idle={pool.idle.qsize()} live={pool.live}")


@dataclass(order=True)
class Pooled:
    last_used: float
    conn: object = field(compare=False)
    created: float = field(compare=False, default_factory=time.monotonic)
    uses: int = field(compare=False, default=0)


class ConnectionPool:
    def __init__(self, factory, *, min_size=1, max_size=8, max_idle=30.0, max_lifetime=300.0,
                 checkout_timeout=2.0, validate=None, close=None):
        self.factory, self.validate, self._close = factory, validate, close or (lambda c: None)
        self.min_size, self.max_size = min_size, max_size
        self.max_idle, self.max_lifetime, self.checkout_timeout = max_idle, max_lifetime, checkout_timeout
        self.idle: queue.LifoQueue[Pooled] = queue.LifoQueue()   # LIFO keeps hot connections hot
        self.lock = threading.Lock()
        self.live = 0
        self.stats = Stats()
        self._stop = threading.Event()
        for _ in range(min_size):
            warm = self._grow()
            if warm is not None:
                self.idle.put(warm)   # pre-warmed connections belong in the queue, not in limbo
        self.reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self.reaper.start()

    def _grow(self) -> Pooled | None:
        with self.lock:
            if self.live >= self.max_size:
                return None
            self.live += 1
        try:
            pooled = Pooled(time.monotonic(), self.factory())
        except Exception:
            with self.lock:
                self.live -= 1
            raise
        self.stats.created += 1
        return pooled

    def _discard(self, pooled: Pooled) -> None:
        with self.lock:
            self.live -= 1
        self.stats.closed += 1
        try:
            self._close(pooled.conn)
        except Exception:
            pass

    def _usable(self, pooled: Pooled, now: float) -> bool:
        if now - pooled.created > self.max_lifetime:
            return False
        if self.validate and not self.validate(pooled.conn):
            self.stats.validation_failures += 1
            return False
        return True

    @contextmanager
    def connection(self, timeout: float | None = None):
        pooled = self.checkout(timeout)
        try:
            yield pooled.conn
        finally:
            self.checkin(pooled)      # returns even when the caller raised

    def checkout(self, timeout: float | None = None) -> Pooled:
        deadline = time.monotonic() + (timeout if timeout is not None else self.checkout_timeout)
        self.stats.checkouts += 1
        waited_from = None
        while True:
            now = time.monotonic()
            try:
                pooled = self.idle.get_nowait()
                if self._usable(pooled, now):
                    pooled.uses += 1
                    if waited_from:
                        self.stats.wait_time += now - waited_from
                    return pooled
                self._discard(pooled)
                continue
            except queue.Empty:
                pass
            pooled = self._grow()
            if pooled is not None:
                pooled.uses += 1
                if waited_from:
                    self.stats.wait_time += time.monotonic() - waited_from
                return pooled
            if waited_from is None:
                waited_from = now
                self.stats.waits += 1
            remaining = deadline - now
            if remaining <= 0:
                self.stats.timeouts += 1
                raise PoolExhausted(
                    f"no connection after {self.checkout_timeout:.1f}s "
                    f"({self.live} live, max {self.max_size}) — raise max_size or shorten your queries")
            try:
                pooled = self.idle.get(timeout=min(remaining, 0.05))
                if self._usable(pooled, time.monotonic()):
                    pooled.uses += 1
                    self.stats.wait_time += time.monotonic() - waited_from
                    return pooled
                self._discard(pooled)
            except queue.Empty:
                continue

    def checkin(self, pooled: Pooled, broken: bool = False) -> None:
        if broken or self._stop.is_set():
            self._discard(pooled)
            return
        pooled.last_used = time.monotonic()
        self.idle.put(pooled)

    def _reap_loop(self) -> None:
        while not self._stop.wait(self.max_idle / 4):
            now = time.monotonic()
            keep: list[Pooled] = []
            while True:
                try:
                    pooled = self.idle.get_nowait()
                except queue.Empty:
                    break
                too_idle = now - pooled.last_used > self.max_idle
                too_old = now - pooled.created > self.max_lifetime
                if (too_idle or too_old) and self.live > self.min_size:
                    self._discard(pooled)
                    self.stats.reaped += 1
                else:
                    keep.append(pooled)
            for pooled in keep:
                self.idle.put(pooled)

    def close(self) -> None:
        self._stop.set()
        while not self.idle.empty():
            self._discard(self.idle.get_nowait())


class FakeConnection:
    """Stands in for a socket: can be closed by the 'server' at any time."""
    ids = itertools.count(1)

    def __init__(self, fail_after: float | None = None):
        self.id = next(self.ids)
        self.open = True
        self.dies_at = time.monotonic() + fail_after if fail_after else None

    def alive(self) -> bool:
        if self.dies_at and time.monotonic() > self.dies_at:
            self.open = False
        return self.open

    def query(self, ms: float = 1.0):
        if not self.alive():
            raise ConnectionError("server closed the connection")
        time.sleep(ms / 1000)
        return f"result from conn {self.id}"


def demo() -> int:
    rng = random.Random(4)
    pool = ConnectionPool(
        factory=lambda: FakeConnection(fail_after=rng.choice([None, None, 0.3])),
        min_size=2, max_size=5, max_idle=0.4, checkout_timeout=1.0,
        validate=lambda c: c.alive(), close=lambda c: setattr(c, "open", False))

    print("16 threads against a pool of 5\n")
    errors: list[str] = []
    served = []

    def worker(n: int):
        try:
            with pool.connection() as conn:
                served.append(conn.query(ms=rng.uniform(5, 30)))
        except (PoolExhausted, ConnectionError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    start = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    print(f"  {len(served)} queries served in {time.monotonic() - start:.2f}s, {len(errors)} errors")
    print(f"  {pool.stats.report(pool)}")

    print("\nstale connections are caught on checkout, not by the caller")
    time.sleep(0.45)
    with pool.connection() as conn:
        print(f"  got a live connection after the idle window: {conn.query()}")
    print(f"  {pool.stats.report(pool)}")

    print("\nexhaustion gives a bounded error, not a hang")
    hold = [pool.checkout() for _ in range(pool.max_size)]
    start = time.monotonic()
    try:
        pool.checkout(timeout=0.3)
    except PoolExhausted as exc:
        print(f"  after {time.monotonic() - start:.2f}s: {exc}")
    for pooled in hold:
        pool.checkin(pooled)

    print("\na caller that raises still returns its connection")
    before = pool.idle.qsize()
    try:
        with pool.connection():
            raise ValueError("boom")
    except ValueError:
        pass
    print(f"  idle before {before}, after the exception {pool.idle.qsize()}")

    print("\nidle reaping down to min_size")
    time.sleep(0.6)
    print(f"  {pool.stats.report(pool)}")
    pool.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true")
    ap.parse_args()
    raise SystemExit(demo())
