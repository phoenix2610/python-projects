#!/usr/bin/env python3
"""A distributed lock with a fencing token, a renewable lease, and a safe release.

    dlock.py --demo                          # runs against an in-process backend
    dlock.py acquire deploy --ttl 30 --redis localhost:6379
    with DistributedLock(backend, "deploy") as lock: ...

Three things separate this from `SETNX key 1`: the value is a unique owner id so
you can only release a lock you still hold (checked atomically), the lease
expires so a dead holder cannot deadlock the system, and every acquisition
returns a monotonically increasing fencing token — the only thing that makes a
paused-then-resumed holder safe against the storage it is writing to.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading
import time
import uuid


class MemoryBackend:
    """Single-process backend — same semantics, useful for tests and the demo."""

    def __init__(self):
        self.lock = threading.Lock()
        self.keys: dict[str, tuple[str, float]] = {}
        self.counters: dict[str, int] = {}

    def set_if_absent(self, key: str, value: str, ttl: float) -> bool:
        with self.lock:
            owner, expires = self.keys.get(key, (None, 0.0))
            if owner is not None and expires > time.monotonic():
                return False
            self.keys[key] = (value, time.monotonic() + ttl)
            return True

    def extend_if_owner(self, key: str, value: str, ttl: float) -> bool:
        with self.lock:
            owner, expires = self.keys.get(key, (None, 0.0))
            if owner != value or expires <= time.monotonic():
                return False
            self.keys[key] = (value, time.monotonic() + ttl)
            return True

    def delete_if_owner(self, key: str, value: str) -> bool:
        with self.lock:
            owner, _ = self.keys.get(key, (None, 0.0))
            if owner != value:
                return False
            del self.keys[key]
            return True

    def incr(self, key: str) -> int:
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key]

    def ttl(self, key: str) -> float:
        owner, expires = self.keys.get(key, (None, 0.0))
        return max(0.0, expires - time.monotonic()) if owner else 0.0


class RedisBackend:
    """Speaks just enough RESP to run SET NX PX, INCR, PTTL and two small Lua scripts."""

    RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
    EXTEND = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, timeout: float = 3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def _command(self, *args) -> object:
        payload = f"*{len(args)}\r\n".encode()
        for arg in args:
            raw = str(arg).encode()
            payload += b"$" + str(len(raw)).encode() + b"\r\n" + raw + b"\r\n"
        self.sock.sendall(payload)
        return self._read()

    def _read(self):
        while b"\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        line, _, self.buf = self.buf.partition(b"\r\n")
        kind, body = line[:1], line[1:].decode()
        if kind in b"+":
            return body
        if kind in b":":
            return int(body)
        if kind in b"-":
            raise RuntimeError(body)
        if kind in b"$":
            length = int(body)
            if length < 0:
                return None
            while len(self.buf) < length + 2:
                self.buf += self.sock.recv(4096)
            value, self.buf = self.buf[:length], self.buf[length + 2:]
            return value.decode()
        if kind in b"*":
            return [self._read() for _ in range(int(body))]
        raise RuntimeError(f"unexpected reply: {line!r}")

    def set_if_absent(self, key, value, ttl) -> bool:
        return self._command("SET", key, value, "NX", "PX", int(ttl * 1000)) == "OK"

    def extend_if_owner(self, key, value, ttl) -> bool:
        return bool(self._command("EVAL", self.EXTEND, 1, key, value, int(ttl * 1000)))

    def delete_if_owner(self, key, value) -> bool:
        return bool(self._command("EVAL", self.RELEASE, 1, key, value))

    def incr(self, key) -> int:
        return int(self._command("INCR", key))

    def ttl(self, key) -> float:
        return max(0, int(self._command("PTTL", key))) / 1000


class LockError(RuntimeError):
    pass


class DistributedLock:
    def __init__(self, backend, name: str, ttl: float = 10.0, owner: str | None = None):
        self.backend, self.name, self.ttl = backend, name, ttl
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.token: int | None = None
        self._renewer: threading.Thread | None = None
        self._stop = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = 10.0, retry: float = 0.05) -> int:
        deadline = time.monotonic() + timeout
        attempts = 0
        while True:
            attempts += 1
            if self.backend.set_if_absent(self.name, self.owner, self.ttl):
                # fencing token: strictly increasing, so stale holders are detectable downstream
                self.token = self.backend.incr(f"{self.name}:fence")
                return self.token
            if not blocking or time.monotonic() >= deadline:
                raise LockError(f"could not acquire {self.name!r} after {attempts} attempts")
            time.sleep(min(retry * attempts, 0.5))

    def extend(self) -> bool:
        return self.backend.extend_if_owner(self.name, self.owner, self.ttl)

    def release(self) -> bool:
        self._stop.set()
        if self._renewer:
            self._renewer.join(timeout=1)
        released = self.backend.delete_if_owner(self.name, self.owner)
        self.token = None
        return released

    def start_renewing(self, every: float | None = None) -> None:
        """Keep the lease alive while the work runs — the watchdog half of a lease."""
        interval = every or self.ttl / 3

        def loop():
            while not self._stop.wait(interval):
                if not self.extend():
                    return          # we lost it; stop pretending otherwise
        self._renewer = threading.Thread(target=loop, daemon=True)
        self._renewer.start()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def demo() -> int:
    backend = MemoryBackend()
    print("two workers, one lock\n")
    a = DistributedLock(backend, "deploy", ttl=1.0, owner="worker-a")
    b = DistributedLock(backend, "deploy", ttl=1.0, owner="worker-b")
    print(f"  worker-a acquires: token {a.acquire()}")
    try:
        b.acquire(blocking=True, timeout=0.3)
    except LockError as exc:
        print(f"  worker-b blocked:  {exc}")
    print(f"  worker-b cannot release a lock it does not hold: {backend.delete_if_owner('deploy', 'worker-b')}")
    a.release()
    print(f"  worker-a released; worker-b now gets token {b.acquire()}  (token increased)")
    b.release()

    print("\na crashed holder's lease expires instead of deadlocking")
    ghost = DistributedLock(backend, "deploy", ttl=0.4, owner="crashed")
    ghost.acquire()
    print(f"  crashed holder took the lock, ttl {backend.ttl('deploy'):.2f}s — now it dies without releasing")
    time.sleep(0.5)
    survivor = DistributedLock(backend, "deploy", ttl=1.0, owner="survivor")
    print(f"  after the lease expires, survivor acquires: token {survivor.acquire()}")

    print("\nlease renewal keeps a long job's lock alive")
    survivor.ttl = 0.4
    survivor.start_renewing(every=0.1)
    time.sleep(0.9)
    still_held = not backend.set_if_absent("deploy", "thief", 1.0)
    print(f"  after 0.9s of work with a 0.4s lease, still held: {still_held}")
    survivor.release()

    print("\ncontention: 8 threads, 200 increments of a non-atomic counter")
    shared = {"value": 0}

    def worker(n):
        for _ in range(25):
            with DistributedLock(backend, "counter", ttl=2.0, owner=f"t{n}"):
                current = shared["value"]
                time.sleep(0.0002)          # a window a racy implementation would lose
                shared["value"] = current + 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    start = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    print(f"  final value {shared['value']} (expected 200) in {time.monotonic() - start:.2f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["acquire", "release", "status"])
    ap.add_argument("name", nargs="?", default="lock")
    ap.add_argument("--ttl", type=float, default=10.0)
    ap.add_argument("--redis", default=None, metavar="HOST:PORT")
    ap.add_argument("--hold", type=float, default=0, help="seconds to hold before releasing")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    if not args.redis:
        print("pass --redis HOST:PORT (or --demo for the in-process backend)")
        return 1
    host, _, port = args.redis.partition(":")
    backend = RedisBackend(host, int(port or 6379))
    lock = DistributedLock(backend, args.name, ttl=args.ttl)
    if args.cmd == "status":
        print(f"  {args.name}: {backend.ttl(args.name):.1f}s remaining on the lease")
        return 0
    try:
        token = lock.acquire(timeout=5)
    except LockError as exc:
        print(f"  {exc}")
        return 1
    print(f"  acquired {args.name} with fencing token {token}")
    if args.hold:
        lock.start_renewing()
        time.sleep(args.hold)
    print(f"  released: {lock.release()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
