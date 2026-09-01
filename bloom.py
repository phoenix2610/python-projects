#!/usr/bin/env python3
"""A Bloom filter sized from the error rate you ask for, then measured against reality.

    bloom.py --demo
    bloom.py build words.txt --error 0.001 --out words.bloom
    bloom.py query words.bloom hello world

Two parameters fall out of (n, p): m = -n*ln(p)/ln(2)^2 bits and k = m/n*ln(2)
hashes. Rather than run k real hashes, this uses Kirsch-Mitzenmacher double
hashing — g_i(x) = h1 + i*h2 — which is provably as good asymptotically and costs
one hash computation.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import random
import struct
import sys

MAGIC = b"BLM1"


class BloomFilter:
    def __init__(self, capacity: int, error_rate: float = 0.01):
        if not 0 < error_rate < 1:
            raise ValueError("error rate must be between 0 and 1")
        self.capacity = max(capacity, 1)
        self.error_rate = error_rate
        self.bits = max(8, math.ceil(-self.capacity * math.log(error_rate) / (math.log(2) ** 2)))
        self.hashes = max(1, round(self.bits / self.capacity * math.log(2)))
        self.data = bytearray((self.bits + 7) // 8)
        self.count = 0

    def _positions(self, item: bytes):
        digest = hashlib.blake2b(item, digest_size=16).digest()
        h1, h2 = struct.unpack("<QQ", digest)
        h2 |= 1                                   # keep the stride odd so it walks the whole filter
        for i in range(self.hashes):
            yield (h1 + i * h2) % self.bits

    def add(self, item) -> None:
        item = item.encode() if isinstance(item, str) else item
        for pos in self._positions(item):
            self.data[pos >> 3] |= 1 << (pos & 7)
        self.count += 1

    def __contains__(self, item) -> bool:
        item = item.encode() if isinstance(item, str) else item
        return all(self.data[pos >> 3] >> (pos & 7) & 1 for pos in self._positions(item))

    def fill_ratio(self) -> float:
        set_bits = sum(bin(b).count("1") for b in self.data)
        return set_bits / self.bits

    def expected_error(self) -> float:
        """Current false-positive probability given how full the filter actually is."""
        return self.fill_ratio() ** self.hashes

    def union(self, other: "BloomFilter") -> "BloomFilter":
        if (self.bits, self.hashes) != (other.bits, other.hashes):
            raise ValueError("filters must have identical geometry to union")
        merged = BloomFilter(self.capacity, self.error_rate)
        merged.data = bytearray(a | b for a, b in zip(self.data, other.data))
        merged.count = self.count + other.count
        return merged

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(MAGIC + struct.pack("<QIId", self.bits, self.hashes, self.count, self.error_rate))
            fh.write(self.data)

    @classmethod
    def load(cls, path: str) -> "BloomFilter":
        with open(path, "rb") as fh:
            if fh.read(4) != MAGIC:
                raise ValueError("not a bloom file")
            bits, hashes, count, error = struct.unpack("<QIId", fh.read(24))
            bf = cls.__new__(cls)
            bf.bits, bf.hashes, bf.count, bf.error_rate = bits, hashes, count, error
            bf.capacity = max(1, round(-bits * (math.log(2) ** 2) / math.log(error)))
            bf.data = bytearray(fh.read())
            return bf

    def __repr__(self):
        return (f"BloomFilter({self.count}/{self.capacity} items, {self.bits} bits "
                f"({self.bits / 8 / 1024:.1f}KB), k={self.hashes})")


def demo() -> int:
    rng = random.Random(7)
    for target in (0.1, 0.01, 0.001):
        bf = BloomFilter(capacity=20_000, error_rate=target)
        inserted = {f"user-{rng.getrandbits(48):x}" for _ in range(20_000)}
        for item in inserted:
            bf.add(item)
        missing = [f"absent-{i}" for i in range(50_000)]
        false_positives = sum(1 for m in missing if m in bf)
        recall = all(item in bf for item in list(inserted)[:5000])
        print(f"  target {target:<6}  measured {false_positives / len(missing):<9.5f}  "
              f"predicted {bf.expected_error():.5f}  {bf.bits // 8 // 1024}KB  k={bf.hashes}  "
              f"no false negatives: {recall}")
    a, b = BloomFilter(1000, 0.01), BloomFilter(1000, 0.01)
    a.add("only-in-a"); b.add("only-in-b")
    merged = a.union(b)
    print(f"\n  union: 'only-in-a' in merged = {'only-in-a' in merged}, "
          f"'only-in-b' in merged = {'only-in-b' in merged}, 'neither' = {'neither' in merged}")
    print(f"  {merged!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build"); b.add_argument("wordlist"); b.add_argument("--error", type=float, default=0.01)
    b.add_argument("--out", default="filter.bloom")
    q = sub.add_parser("query"); q.add_argument("filter"); q.add_argument("items", nargs="+")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    if args.cmd == "build":
        lines = [ln.strip() for ln in open(args.wordlist) if ln.strip()]
        bf = BloomFilter(len(lines), args.error)
        for line in lines:
            bf.add(line)
        bf.save(args.out)
        print(f"{bf!r} -> {args.out}  ({bf.fill_ratio():.1%} of bits set)")
        return 0
    bf = BloomFilter.load(args.filter)
    for item in args.items:
        print(f"  {item}: {'probably present' if item in bf else 'definitely absent'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
