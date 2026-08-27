#!/usr/bin/env python3
"""Attribute memory growth to the line that caused it, with a decorator.

    @profile_memory(top=5)
    def load(): ...

    memprof.py --demo
    memprof.py script.py --top 10       # profile a whole script

tracemalloc gives allocation snapshots; the useful part is the diff, filtered to
your own code and sorted by growth. `--retained` runs a gc.collect() before the
final snapshot so you see what actually stayed alive, not what merely passed
through — usually the number you care about.
"""
from __future__ import annotations

import argparse
import functools
import gc
import linecache
import os
import sys
import tracemalloc


def human(n: float) -> str:
    sign = "-" if n < 0 else "+"
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{sign}{n:.1f}{unit}"
        n /= 1024
    return f"{sign}{n:.1f}GB"


def _report(before, after, top: int, label: str, seconds: float, peak: int, retained: bool) -> None:
    stats = after.compare_to(before, "lineno")
    mine = [s for s in stats if s.traceback and "site-packages" not in str(s.traceback[0].filename)
            and "tracemalloc" not in str(s.traceback[0].filename)]
    total = sum(s.size_diff for s in stats)
    print(f"\n{label}  {seconds * 1000:.1f}ms  "
          f"{'retained' if retained else 'net'} {human(total)}  peak {human(peak)}")
    for stat in mine[:top]:
        frame = stat.traceback[0]
        line = linecache.getline(frame.filename, frame.lineno).strip()
        print(f"  {human(stat.size_diff):>10}  {stat.count_diff:+6} blocks  "
              f"{os.path.basename(frame.filename)}:{frame.lineno}")
        if line:
            print(f"              {line[:88]}")


def profile_memory(top: int = 5, retained: bool = True, label: str | None = None):
    """Decorator: report per-line memory growth caused by one call."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            import time
            already = tracemalloc.is_tracing()
            if not already:
                tracemalloc.start(25)
            before = tracemalloc.take_snapshot()
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                if retained:
                    gc.collect()
                after = tracemalloc.take_snapshot()
                peak = tracemalloc.get_traced_memory()[1]
                _report(before, after, top, label or fn.__qualname__, elapsed, peak, retained)
                if not already:
                    tracemalloc.stop()
        return inner
    return wrap


class MemoryRegion:
    """Context manager version, for a block that isn't a function."""

    def __init__(self, label: str, top: int = 5, retained: bool = True):
        self.label, self.top, self.retained = label, top, retained

    def __enter__(self):
        import time
        self.already = tracemalloc.is_tracing()
        if not self.already:
            tracemalloc.start(25)
        self.before = tracemalloc.take_snapshot()
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        import time
        elapsed = time.perf_counter() - self.start
        if self.retained:
            gc.collect()
        after = tracemalloc.take_snapshot()
        peak = tracemalloc.get_traced_memory()[1]
        _report(self.before, after, self.top, self.label, elapsed, peak, self.retained)
        if not self.already:
            tracemalloc.stop()
        return False


@profile_memory(top=4, label="list of dicts (kept)")
def build_dicts(n: int):
    return [{"id": i, "name": f"user-{i}", "tags": ["a", "b"]} for i in range(n)]


@profile_memory(top=4, label="same data in tuples")
def build_tuples(n: int):
    return [(i, f"user-{i}", ("a", "b")) for i in range(n)]


@profile_memory(top=4, label="generator consumed lazily")
def stream_sum(n: int):
    return sum(len(f"user-{i}") for i in range(n))


@profile_memory(top=3, label="leaks into a module-level cache")
def leaky(n: int):
    for i in range(n):
        _CACHE.setdefault(i, [0] * 20)
    return len(_CACHE)


_CACHE: dict = {}


def demo() -> int:
    n = 40_000
    print(f"three ways to hold {n:,} records\n" + "=" * 60)
    rows = build_dicts(n)
    tuples = build_tuples(n)
    total = stream_sum(n)
    del rows, tuples

    print("\n" + "=" * 60)
    print("a function that quietly retains everything it touches")
    print("=" * 60)
    leaky(20_000)
    leaky(20_000)      # second call adds nothing: the cache is already warm

    print("\n" + "=" * 60)
    print("context manager form")
    print("=" * 60)
    with MemoryRegion("string concatenation in a loop", top=3):
        text = ""
        for i in range(20_000):
            text += str(i % 10)
    with MemoryRegion("join instead", top=3):
        text2 = "".join(str(i % 10) for i in range(20_000))
    print(f"\n  (both produced {len(text)} == {len(text2)} characters)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", nargs="?")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.script:
        return demo()
    source = open(args.script).read()
    with MemoryRegion(os.path.basename(args.script), top=args.top):
        exec(compile(source, args.script, "exec"), {"__name__": "__main__", "__file__": args.script})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
