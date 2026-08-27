#!/usr/bin/env python3
"""A consistent hash ring, and proof that removing a node barely moves any keys.

    ring.py --demo
    ring.py --nodes 8 --vnodes 200 --keys 100000 --remove node-3

The point of virtual nodes: one hash position per server gives wildly uneven
load, because random points on a circle cluster. 150+ points per server smooths
the distribution to within a few percent, and removing a server only reassigns
its own share — which the demo measures instead of asserting.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
from collections import Counter


class HashRing:
    def __init__(self, nodes=(), vnodes: int = 150):
        self.vnodes = vnodes
        self.ring: dict[int, str] = {}
        self.sorted_keys: list[int] = []
        for node in nodes:
            self.add(node)

    @staticmethod
    def _hash(key: str) -> int:
        return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")

    def add(self, node: str) -> None:
        for i in range(self.vnodes):
            point = self._hash(f"{node}#{i}")
            self.ring[point] = node
        self.sorted_keys = sorted(self.ring)

    def remove(self, node: str) -> None:
        for i in range(self.vnodes):
            self.ring.pop(self._hash(f"{node}#{i}"), None)
        self.sorted_keys = sorted(self.ring)

    def get(self, key: str) -> str | None:
        """First node clockwise from the key's position on the circle."""
        if not self.sorted_keys:
            return None
        point = self._hash(key)
        idx = bisect.bisect(self.sorted_keys, point) % len(self.sorted_keys)
        return self.ring[self.sorted_keys[idx]]

    def get_replicas(self, key: str, n: int) -> list[str]:
        """Walk clockwise collecting distinct nodes — how replica sets are placed."""
        if not self.sorted_keys:
            return []
        point = self._hash(key)
        idx = bisect.bisect(self.sorted_keys, point)
        out: list[str] = []
        for step in range(len(self.sorted_keys)):
            node = self.ring[self.sorted_keys[(idx + step) % len(self.sorted_keys)]]
            if node not in out:
                out.append(node)
                if len(out) == n:
                    break
        return out

    def distribution(self, keys) -> Counter:
        return Counter(self.get(k) for k in keys)


def spread(counts: Counter, total: int) -> str:
    if not counts:
        return "-"
    shares = [c / total for c in counts.values()]
    return f"{min(shares):.1%}-{max(shares):.1%} per node (ideal {1 / len(counts):.1%})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=6)
    ap.add_argument("--vnodes", type=int, default=150)
    ap.add_argument("--keys", type=int, default=50_000)
    ap.add_argument("--remove", default=None)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    names = [f"node-{i}" for i in range(args.nodes)]
    keys = [f"key:{i}" for i in range(args.keys)]

    if args.demo:
        print("virtual nodes vs load balance\n")
        for vnodes in (1, 5, 50, 150, 500):
            ring = HashRing(names, vnodes=vnodes)
            counts = ring.distribution(keys)
            print(f"  {vnodes:>4} vnodes/node   {spread(counts, len(keys))}")
        print("\nremoving one node of 6 (a ring should move ~1/6 of keys)\n")
        for vnodes in (1, 150):
            ring = HashRing(names, vnodes=vnodes)
            before = {k: ring.get(k) for k in keys}
            ring.remove("node-3")
            moved = sum(1 for k in keys if ring.get(k) != before[k])
            print(f"  {vnodes:>4} vnodes/node   {moved / len(keys):.2%} of keys reassigned")
        print("\n  by comparison, hash(key) % n moves:")
        for n in (6,):
            moved = sum(1 for i, k in enumerate(keys) if HashRing._hash(k) % n != HashRing._hash(k) % (n - 1))
            print(f"    {moved / len(keys):.2%} of keys when going from {n} to {n - 1} servers")
        ring = HashRing(names, vnodes=150)
        print(f"\n  replica set for 'key:42': {ring.get_replicas('key:42', 3)}")
        return 0

    ring = HashRing(names, vnodes=args.vnodes)
    counts = ring.distribution(keys)
    width = max(len(n) for n in names)
    for node in names:
        share = counts[node] / len(keys)
        print(f"  {node.ljust(width)}  {counts[node]:>7}  {share:6.2%}  {'█' * round(share * 120)}")
    print(f"\n  {spread(counts, len(keys))}")

    if args.remove:
        before = {k: ring.get(k) for k in keys}
        ring.remove(args.remove)
        moved = sum(1 for k in keys if ring.get(k) != before[k])
        landed = Counter(ring.get(k) for k in keys if ring.get(k) != before[k])
        print(f"\n  removed {args.remove}: {moved / len(keys):.2%} of keys moved")
        for node, n in landed.most_common():
            print(f"    {node.ljust(width)} absorbed {n:>6} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
