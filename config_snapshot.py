#!/usr/bin/env python3
"""Fingerprint config files across hosts and report anything that diverged.

    config_snapshot.py snapshot /etc/nginx /etc/ssh --out host-a.json
    config_snapshot.py compare host-a.json host-b.json
    config_snapshot.py --demo

Configuration drift between servers that are supposed to be identical is one
of the most common causes of "works on one box, not the other." This walks a
set of directories, hashes every file's content (not its mtime — a file
touched but not changed shouldn't count as drift), and records permissions
separately, since a file with identical content but different mode bits is
its own kind of problem. Comparing two snapshots then reports exactly three
things: files only on one side, files with the same path but different
content, and files with the same content but different permissions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class FileFingerprint:
    path: str  # relative to the snapshot root, normalized
    content_hash: str
    size: int
    mode: str  # octal permission string, e.g. "644"


@dataclass
class Snapshot:
    hostname: str
    taken_at: str
    files: dict[str, FileFingerprint] = field(default_factory=dict)


def hash_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def take_snapshot(roots: list[str], hostname: str) -> Snapshot:
    snapshot = Snapshot(hostname=hostname, taken_at=datetime.now(timezone.utc).isoformat())
    for root in roots:
        root = root.rstrip("/")
        base = os.path.dirname(root) or "/"
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                try:
                    st = os.stat(full)
                    content_hash = hash_file(full)
                except (OSError, PermissionError):
                    continue
                rel = os.path.relpath(full, base)
                snapshot.files[rel] = FileFingerprint(
                    path=rel, content_hash=content_hash, size=st.st_size, mode=oct(stat.S_IMODE(st.st_mode))[2:]
                )
    return snapshot


def save_snapshot(snapshot: Snapshot, path: str) -> None:
    payload = {
        "hostname": snapshot.hostname,
        "taken_at": snapshot.taken_at,
        "files": {k: {"content_hash": v.content_hash, "size": v.size, "mode": v.mode} for k, v in snapshot.files.items()},
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def load_snapshot(path: str) -> Snapshot:
    with open(path) as fh:
        raw = json.load(fh)
    files = {k: FileFingerprint(path=k, **v) for k, v in raw["files"].items()}
    return Snapshot(hostname=raw["hostname"], taken_at=raw["taken_at"], files=files)


@dataclass
class DriftReport:
    only_in_a: list[str]
    only_in_b: list[str]
    content_diff: list[str]
    mode_only_diff: list[str]  # same content, different permissions
    identical_count: int


def compare_snapshots(a: Snapshot, b: Snapshot) -> DriftReport:
    keys_a, keys_b = set(a.files), set(b.files)
    only_a = sorted(keys_a - keys_b)
    only_b = sorted(keys_b - keys_a)

    content_diff = []
    mode_diff = []
    identical = 0
    for key in sorted(keys_a & keys_b):
        fa, fb = a.files[key], b.files[key]
        if fa.content_hash != fb.content_hash:
            content_diff.append(key)
        elif fa.mode != fb.mode:
            mode_diff.append(key)
        else:
            identical += 1

    return DriftReport(only_a, only_b, content_diff, mode_diff, identical)


def format_drift_report(report: DriftReport, name_a: str, name_b: str) -> str:
    total_diff = len(report.only_in_a) + len(report.only_in_b) + len(report.content_diff) + len(report.mode_only_diff)
    lines = [f"{report.identical_count} files identical, {total_diff} differences\n"]

    if report.content_diff:
        lines.append(f"content differs ({len(report.content_diff)}):")
        for path in report.content_diff:
            lines.append(f"  {path}")
        lines.append("")

    if report.mode_only_diff:
        lines.append(f"same content, different permissions ({len(report.mode_only_diff)}):")
        for path in report.mode_only_diff:
            lines.append(f"  {path}")
        lines.append("")

    if report.only_in_a:
        lines.append(f"only on {name_a} ({len(report.only_in_a)}):")
        for path in report.only_in_a:
            lines.append(f"  {path}")
        lines.append("")

    if report.only_in_b:
        lines.append(f"only on {name_b} ({len(report.only_in_b)}):")
        for path in report.only_in_b:
            lines.append(f"  {path}")

    return "\n".join(lines).rstrip()


# ------------------------------------------------------------ demo

def build_demo_snapshots() -> tuple[Snapshot, Snapshot]:
    def fp(path, content, mode="644"):
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        return FileFingerprint(path, h, len(content), mode)

    web1_files = {
        "nginx/nginx.conf": fp("nginx/nginx.conf", "worker_processes 4;\nkeepalive_timeout 65;"),
        "nginx/sites-enabled/app.conf": fp("nginx/sites-enabled/app.conf", "server { listen 80; }"),
        "ssh/sshd_config": fp("ssh/sshd_config", "PermitRootLogin no\nPasswordAuthentication no", mode="600"),
        "app/settings.py": fp("app/settings.py", "DEBUG = False\nTIMEOUT = 30"),
        "app/local_override.py": fp("app/local_override.py", "# a file that only exists on web-1, left over from debugging"),
    }
    web2_files = {
        "nginx/nginx.conf": fp("nginx/nginx.conf", "worker_processes 2;\nkeepalive_timeout 65;"),  # different worker count
        "nginx/sites-enabled/app.conf": fp("nginx/sites-enabled/app.conf", "server { listen 80; }"),  # identical
        "ssh/sshd_config": fp("ssh/sshd_config", "PermitRootLogin no\nPasswordAuthentication no", mode="644"),  # same content, wrong mode!
        "app/settings.py": fp("app/settings.py", "DEBUG = False\nTIMEOUT = 30"),  # identical
        "app/feature_flags.py": fp("app/feature_flags.py", "NEW_CHECKOUT = True"),  # only on web-2
    }

    return (
        Snapshot("web-1", "2026-08-27T10:00:00Z", web1_files),
        Snapshot("web-2", "2026-08-27T10:05:00Z", web2_files),
    )


def demo() -> int:
    print("comparing config snapshots from two servers that are supposed to be identical\n")
    web1, web2 = build_demo_snapshots()

    print(f"web-1: {len(web1.files)} files fingerprinted")
    print(f"web-2: {len(web2.files)} files fingerprinted\n")

    report = compare_snapshots(web1, web2)
    print(format_drift_report(report, "web-1", "web-2"))

    print(f"\n\nnote: sshd_config has the SAME content on both hosts but web-2's file is world-")
    print(f"readable (644) where web-1's is 600 — that's a real security-relevant divergence")
    print(f"a content-only hash comparison would completely miss. nginx.conf genuinely differs")
    print(f"(worker_processes 4 vs 2), which is a real config difference worth asking about.")
    print(f"local_override.py and feature_flags.py are each unique to one host — the kind of")
    print(f"leftover debug file or half-shipped feature flag that's easy to lose track of.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    snap_p = sub.add_parser("snapshot")
    snap_p.add_argument("roots", nargs="+")
    snap_p.add_argument("--out", required=True)
    snap_p.add_argument("--hostname", default=None)
    cmp_p = sub.add_parser("compare")
    cmp_p.add_argument("snapshot_a")
    cmp_p.add_argument("snapshot_b")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()

    if args.cmd == "snapshot":
        import socket

        hostname = args.hostname or socket.gethostname()
        snapshot = take_snapshot(args.roots, hostname)
        save_snapshot(snapshot, args.out)
        print(f"{len(snapshot.files)} files fingerprinted -> {args.out}")
        return 0

    if args.cmd == "compare":
        a = load_snapshot(args.snapshot_a)
        b = load_snapshot(args.snapshot_b)
        report = compare_snapshots(a, b)
        print(format_drift_report(report, a.hostname, b.hostname))
        total_diff = len(report.only_in_a) + len(report.only_in_b) + len(report.content_diff) + len(report.mode_only_diff)
        return 1 if total_diff else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
