#!/usr/bin/env python3
"""Audit a password dump the way an attacker would rank it, entirely offline.

    pwaudit.py --check 'Summ3r2024!'
    pwaudit.py vault.csv --column password --breached breached-sha1.txt

Scores real guessability, not the theatre of "one symbol required": it strips
leet substitutions, walks the keyboard for runs like `qwerty` and `1qaz2wsx`,
finds repeated and sequential blocks, and prices the remainder as a search space
in guesses. Breach checking reads a local SHA-1 prefix file — nothing leaves the
machine.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import sys
from collections import Counter

ROWS = ["`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./"]
LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "1": "l", "!": "i", "0": "o", "$": "s", "5": "s", "7": "t"})
COMMON = {
    "password", "welcome", "letmein", "monkey", "dragon", "qwerty", "iloveyou", "admin",
    "login", "master", "shadow", "football", "baseball", "sunshine", "princess", "summer",
    "winter", "spring", "autumn", "secret", "trustno", "hello", "freedom", "whatever",
}
CLASSES = [(re.compile(r"[a-z]"), 26), (re.compile(r"[A-Z]"), 26), (re.compile(r"\d"), 10),
           (re.compile(r"[^\w]"), 33), (re.compile(r"_"), 1)]


def keyboard_runs(pw: str) -> int:
    """Longest adjacent-key run, horizontal or vertical, e.g. qwerty or 1qaz."""
    pos = {ch: (r, c) for r, row in enumerate(ROWS) for c, ch in enumerate(row)}
    low = pw.lower()
    best = run = 1
    for a, b in zip(low, low[1:]):
        if a in pos and b in pos:
            (r1, c1), (r2, c2) = pos[a], pos[b]
            if (r1 == r2 and abs(c1 - c2) == 1) or (abs(r1 - r2) == 1 and abs(c1 - c2) <= 1):
                run += 1
                best = max(best, run)
                continue
        run = 1
    return best


def sequences(pw: str) -> int:
    """Longest ascending or descending run: abcd, 4321."""
    best = run = 1
    for a, b in zip(pw.lower(), pw.lower()[1:]):
        if ord(b) - ord(a) in (1, -1):
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def repeats(pw: str) -> int:
    """Length of the shortest repeating unit, if the password is a repeated block."""
    for size in range(1, len(pw) // 2 + 1):
        if len(pw) % size == 0 and pw[:size] * (len(pw) // size) == pw:
            return size
    return len(pw)


def analyse(pw: str) -> dict:
    findings: list[str] = []
    alphabet = sum(size for pattern, size in CLASSES if pattern.search(pw))
    raw_bits = len(pw) * math.log2(alphabet) if alphabet else 0

    stripped = pw.lower().translate(LEET)
    penalty = 0.0
    for word in COMMON:
        if word in stripped:
            penalty += len(word) * 2.2
            findings.append(f"contains the common word {word!r} (leet spelling does not help)")
            break
    if m := re.search(r"(19|20)\d{2}", pw):
        penalty += 6
        findings.append(f"contains the year {m.group(0)} — attackers try every year first")
    run = keyboard_runs(pw)
    if run >= 4:
        penalty += run * 1.8
        findings.append(f"walks {run} adjacent keys on the keyboard")
    seq = sequences(pw)
    if seq >= 4:
        penalty += seq * 1.6
        findings.append(f"contains a {seq}-character sequence like abcd or 4321")
    unit = repeats(pw)
    if unit < len(pw):
        penalty += (len(pw) - unit) * math.log2(max(alphabet, 2)) * 0.8
        findings.append(f"is the block {pw[:unit]!r} repeated {len(pw) // unit} times")
    counts = Counter(pw)
    if pw and max(counts.values()) > len(pw) / 2:
        penalty += 4
        findings.append("more than half the characters are the same")
    if re.fullmatch(r"[A-Z][a-z]+\d{0,4}[!?.]?", pw):
        penalty += 5
        findings.append("follows the Capital + word + digits + symbol shape every policy produces")

    bits = max(raw_bits - penalty, 0.0)
    guesses = 2 ** bits
    return {"bits": bits, "raw_bits": raw_bits, "guesses": guesses, "findings": findings}


def crack_time(guesses: float, per_second: float) -> str:
    seconds = guesses / 2 / per_second
    for limit, unit, name in ((60, 1, "seconds"), (3600, 60, "minutes"), (86400, 3600, "hours"),
                              (2_592_000, 86400, "days"), (31_536_000, 2_592_000, "months")):
        if seconds < limit:
            return f"{seconds / unit:.1f} {name}"
    years = seconds / 31_536_000
    return f"{years:.0f} years" if years < 1e6 else f"{years:.1e} years"


def verdict(bits: float) -> str:
    return ("critical" if bits < 28 else "weak" if bits < 40 else
            "fair" if bits < 55 else "strong" if bits < 70 else "excellent")


def load_breached(path: str | None) -> set[str]:
    if not path:
        return set()
    with open(path) as fh:
        return {line.strip().upper() for line in fh if line.strip()}


def report(label: str, pw: str, breached: set[str], rate: float) -> str:
    res = analyse(pw)
    sha1 = hashlib.sha1(pw.encode()).hexdigest().upper()
    hit = sha1 in breached or sha1[:5] in breached
    print(f"\n{label}")
    print(f"  strength   {verdict(res['bits'])}  ({res['bits']:.0f} bits, raw {res['raw_bits']:.0f})")
    print(f"  offline    {crack_time(res['guesses'], rate)} at {rate:.0e} guesses/sec")
    if hit:
        print("  breached   FOUND in the breach list — change it now")
    for note in res["findings"]:
        print(f"  weakness   {note}")
    if not res["findings"] and not hit:
        print("  weakness   none detected")
    return "breached" if hit else verdict(res["bits"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", nargs="?", help="CSV export from a password manager")
    ap.add_argument("--check", help="audit a single password")
    ap.add_argument("--column", default="password")
    ap.add_argument("--label-column", default="name")
    ap.add_argument("--breached", help="file of breached SHA-1 hashes (or 5-char prefixes)")
    ap.add_argument("--rate", type=float, default=1e11, help="attacker guesses per second")
    args = ap.parse_args()

    breached = load_breached(args.breached)
    if args.check:
        report("password", args.check, breached, args.rate)
        return 0
    if not args.csv_path:
        ap.error("pass a CSV or --check")

    tally, reuse = Counter(), Counter()
    with open(args.csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        reuse[row[args.column]] += 1
    for row in rows:
        pw = row[args.column]
        label = row.get(args.label_column) or "(unnamed)"
        tally[report(label, pw, breached, args.rate)] += 1
        if reuse[pw] > 1:
            print(f"  reused     the same password appears on {reuse[pw]} accounts")

    print("\nsummary")
    for name in ("breached", "critical", "weak", "fair", "strong", "excellent"):
        if tally[name]:
            print(f"  {name.ljust(10)} {tally[name]}")
    return 1 if tally["breached"] or tally["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
