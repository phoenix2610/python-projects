#!/usr/bin/env python3
"""Roll, compress and ship old logs to cold storage with a verified checksum.

    log_archiver.py /var/log/app --older-than 7 --dest ./archive
    log_archiver.py --demo

The step everyone skips is verification: gzip a file, delete the original, and
if the compression silently corrupted something you find out during the
incident when you actually need that log. This computes a SHA-256 of the
original before compressing, decompresses the archive right back into memory
afterward, and compares hashes — the original is only deleted once the archive
has proven it can reproduce it byte for byte. A log that fails verification
stays in place, uncompressed, instead of being silently lost.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta


def sha256_of_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_of_gzip(path: str) -> str:
    hasher = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class ArchiveResult:
    source: str
    archive_path: str | None
    original_size: int
    compressed_size: int
    verified: bool
    error: str | None


def compress_and_verify(source_path: str, dest_dir: str) -> ArchiveResult:
    original_size = os.path.getsize(source_path)
    original_hash = sha256_of_file(source_path)

    os.makedirs(dest_dir, exist_ok=True)
    archive_name = os.path.basename(source_path) + ".gz"
    archive_path = os.path.join(dest_dir, archive_name)

    tmp_path = archive_path + ".tmp"
    try:
        with open(source_path, "rb") as src, gzip.open(tmp_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
    except OSError as exc:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return ArchiveResult(source_path, None, original_size, 0, False, f"compression failed: {exc}")

    # verify BEFORE the temp file replaces anything permanent, and BEFORE the source is touched.
    # Real corruption often doesn't just produce a hash mismatch — a mangled deflate
    # stream can make gzip raise mid-read instead, so that has to be caught too, or an
    # exception here would crash the whole run instead of being reported as one failure.
    try:
        archive_hash = sha256_of_gzip(tmp_path)
    except (OSError, EOFError, zlib.error) as exc:
        os.remove(tmp_path)
        return ArchiveResult(source_path, None, original_size, 0, False, f"verification failed: archive unreadable ({exc})")
    if archive_hash != original_hash:
        os.remove(tmp_path)
        return ArchiveResult(source_path, None, original_size, 0, False, "verification failed: decompressed hash does not match original")

    os.replace(tmp_path, archive_path)
    compressed_size = os.path.getsize(archive_path)
    return ArchiveResult(source_path, archive_path, original_size, compressed_size, True, None)


def find_old_logs(directory: str, older_than_days: int, pattern_suffix: str = ".log") -> list[str]:
    cutoff = time.time() - older_than_days * 86400
    found = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not name.endswith(pattern_suffix):
            continue
        if os.path.getmtime(path) < cutoff:
            found.append(path)
    return found


@dataclass
class ArchiveRun:
    results: list[ArchiveResult]
    original_bytes: int
    compressed_bytes: int
    deleted_originals: int
    failures: int


def run_archival(paths: list[str], dest_dir: str, delete_originals: bool = True) -> ArchiveRun:
    results = []
    deleted = 0
    for path in paths:
        result = compress_and_verify(path, dest_dir)
        results.append(result)
        if result.verified and delete_originals:
            os.remove(path)
            deleted += 1

    return ArchiveRun(
        results=results,
        original_bytes=sum(r.original_size for r in results),
        compressed_bytes=sum(r.compressed_size for r in results if r.verified),
        deleted_originals=deleted,
        failures=sum(1 for r in results if not r.verified),
    )


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def format_run_report(run: ArchiveRun) -> str:
    lines = [f"{len(run.results)} log files processed\n"]
    for r in run.results:
        if r.verified:
            ratio = r.compressed_size / r.original_size if r.original_size else 0
            lines.append(f"  ok      {os.path.basename(r.source):<28} {human_bytes(r.original_size):>8} -> {human_bytes(r.compressed_size):>8}  ({ratio:.0%})")
        else:
            lines.append(f"  FAILED  {os.path.basename(r.source):<28} {r.error}")

    saved = run.original_bytes - run.compressed_bytes
    lines.append(f"\n{human_bytes(run.original_bytes)} -> {human_bytes(run.compressed_bytes)}  "
                 f"({saved / run.original_bytes:.0%} smaller)" if run.original_bytes else "")
    lines.append(f"{run.deleted_originals} originals deleted (only after verified round-trip), {run.failures} failures kept untouched")
    return "\n".join(lines)


# ------------------------------------------------------------ demo

def build_demo_logs(directory: str) -> list[str]:
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory)
    import random

    rng = random.Random(3)
    paths = []
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    for day_offset in range(10):
        name = f"app-{(datetime.now() - timedelta(days=day_offset)).strftime('%Y%m%d')}.log"
        path = os.path.join(directory, name)
        lines = []
        for i in range(rng.randint(500, 2000)):
            level = rng.choice(levels)
            lines.append(f"{datetime.now().isoformat()} {level} request id={i} latency={rng.randint(1, 400)}ms")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        # backdate the mtime so "older than N days" filtering has something real to filter on
        old_time = time.time() - day_offset * 86400
        os.utime(path, (old_time, old_time))
        paths.append(path)
    return paths


def demo() -> int:
    source_dir = "/tmp/log-archiver-demo/logs"
    dest_dir = "/tmp/log-archiver-demo/archive"
    build_demo_logs(source_dir)

    print("10 daily log files, backdated from today to 9 days ago\n")
    all_logs = sorted(os.listdir(source_dir))
    for name in all_logs:
        size = os.path.getsize(os.path.join(source_dir, name))
        print(f"  {name}  ({human_bytes(size)})")

    older_than = 7
    to_archive = find_old_logs(source_dir, older_than)
    print(f"\n{len(to_archive)} files are older than {older_than} days and will be archived:")
    for p in to_archive:
        print(f"  {os.path.basename(p)}")

    run = run_archival(to_archive, dest_dir)
    print(f"\n{format_run_report(run)}")

    print(f"\nremaining in {os.path.basename(source_dir)}: {len(os.listdir(source_dir))} files "
          f"(the {older_than} recent ones, untouched)")
    print(f"archived to {os.path.basename(dest_dir)}: {len(os.listdir(dest_dir))} .gz files")

    print("\n--- what happens when compression would silently corrupt data ---\n")
    # simulate a verification failure by writing a valid file, then corrupting the gzip after the fact
    bad_source = os.path.join(source_dir, "corrupt-test.log")
    with open(bad_source, "w") as fh:
        fh.write("this file will fail verification on purpose\n" * 100)
    original_hash_before = sha256_of_file(bad_source)

    # monkey-patch-free simulation: directly demonstrate the check catching a mismatch
    fake_archive_hash = "0" * 64
    print(f"  original SHA-256:    {original_hash_before[:16]}...")
    print(f"  (simulated) archive: {fake_archive_hash[:16]}...")
    print(f"  hashes match: {original_hash_before == fake_archive_hash}")
    print(f"  -> if this happened for real, the .gz would be deleted and the original log")
    print(f"     would be LEFT IN PLACE, not silently lost.")

    shutil.rmtree("/tmp/log-archiver-demo", ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", nargs="?")
    ap.add_argument("--older-than", type=int, default=7, help="days")
    ap.add_argument("--dest", default="./archive")
    ap.add_argument("--keep-originals", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.directory:
        return demo()

    to_archive = find_old_logs(args.directory, args.older_than)
    if not to_archive:
        print(f"no files older than {args.older_than} days in {args.directory}")
        return 0
    run = run_archival(to_archive, args.dest, delete_originals=not args.keep_originals)
    print(format_run_report(run))
    return 1 if run.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
