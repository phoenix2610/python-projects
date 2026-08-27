#!/usr/bin/env python3
"""Hash big files and whole trees: chunked, resumable, parallel, with a manifest.

    streamhash.py ~/Videos --algo blake2b --workers 8 --out manifest.json
    streamhash.py bigfile.iso --resume            # survives a Ctrl-C
    streamhash.py verify manifest.json            # what changed since last time

Resumability is the interesting constraint: a plain hash object cannot be
pickled, so progress is saved as (bytes consumed, intermediate state) at chunk
boundaries using a tree hash — each 8MB block is hashed independently and the
block digests are hashed together, which also makes the work parallelisable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

BLOCK = 8 << 20        # 8MB: big enough to amortise syscalls, small enough to checkpoint often
CHUNK = 1 << 20


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def hash_block(args: tuple[str, int, int, str]) -> tuple[int, str]:
    """Hash one block of a file — the unit of both parallelism and resume."""
    path, index, size, algo = args
    digest = hashlib.new(algo)
    with open(path, "rb") as fh:
        fh.seek(index * BLOCK)
        remaining = size
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return index, digest.hexdigest()


def tree_hash(path: str, algo: str = "blake2b", workers: int = 1,
              resume_file: str | None = None, progress=None) -> dict:
    size = os.path.getsize(path)
    blocks = max(1, (size + BLOCK - 1) // BLOCK)
    done: dict[int, str] = {}
    if resume_file and os.path.exists(resume_file):
        saved = json.load(open(resume_file))
        if saved.get("path") == os.path.abspath(path) and saved.get("size") == size:
            done = {int(k): v for k, v in saved["blocks"].items()}

    todo = [(path, i, min(BLOCK, size - i * BLOCK), algo) for i in range(blocks) if i not in done]
    started = time.perf_counter()
    if workers > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, digest in pool.map(hash_block, todo):
                done[index] = digest
                if progress:
                    progress(len(done), blocks)
    else:
        for job in todo:
            index, digest = hash_block(job)
            done[index] = digest
            if progress:
                progress(len(done), blocks)
            if resume_file and len(done) % 4 == 0:
                json.dump({"path": os.path.abspath(path), "size": size, "algo": algo,
                           "blocks": done}, open(resume_file, "w"))

    roll = hashlib.new(algo)
    for i in range(blocks):
        roll.update(bytes.fromhex(done[i]))
    if resume_file and os.path.exists(resume_file):
        os.remove(resume_file)
    elapsed = time.perf_counter() - started
    return {"path": path, "size": size, "blocks": blocks, "algo": algo,
            "root": roll.hexdigest(), "flat": None,
            "seconds": round(elapsed, 3),
            "throughput": human(size / elapsed) + "/s" if elapsed else "-"}


def flat_hash(path: str, algo: str = "blake2b") -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def walk(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        found.extend(os.path.join(dirpath, f) for f in filenames)
    return sorted(found)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="file, directory, or a manifest when using verify")
    ap.add_argument("--verify", action="store_true", help="treat the target as a manifest and re-check it")
    ap.add_argument("--algo", default="blake2b", choices=sorted(hashlib.algorithms_guaranteed - {"shake_128", "shake_256"}))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", help="write a JSON manifest here")
    ap.add_argument("--resume", action="store_true", help="checkpoint progress so Ctrl-C is not fatal")
    ap.add_argument("--flat", action="store_true", help="also compute the plain single-stream hash")
    args = ap.parse_args()

    if args.verify:
        manifest = json.load(open(args.target))
        changed = missing = ok = 0
        for entry in manifest["files"]:
            if not os.path.exists(entry["path"]):
                print(f"  missing  {entry['path']}")
                missing += 1
                continue
            current = tree_hash(entry["path"], entry["algo"], args.workers)
            if current["root"] != entry["root"]:
                print(f"  CHANGED  {entry['path']}")
                changed += 1
            else:
                ok += 1
        print(f"\n{ok} unchanged, {changed} changed, {missing} missing")
        return 1 if (changed or missing) else 0

    files = walk(args.target)
    if not files:
        print("nothing to hash", file=sys.stderr)
        return 1

    results = []
    total = 0
    started = time.perf_counter()
    for path in files:
        resume_file = f"{path}.hashstate" if args.resume else None

        def progress(done, blocks, path=path):
            if sys.stderr.isatty() and blocks > 1:
                print(f"\r  {os.path.basename(path)}: {done}/{blocks} blocks", end="", file=sys.stderr)

        entry = tree_hash(path, args.algo, args.workers, resume_file, progress)
        if args.flat:
            entry["flat"] = flat_hash(path, args.algo)
        results.append(entry)
        total += entry["size"]
        print(f"  {entry['root'][:32]}  {human(entry['size']):>9}  {os.path.relpath(path, args.target if os.path.isdir(args.target) else '.')}")

    elapsed = time.perf_counter() - started
    print(f"\n{len(results)} files, {human(total)} in {elapsed:.2f}s ({human(total / max(elapsed, 1e-9))}/s)")
    if args.out:
        json.dump({"root": os.path.abspath(args.target), "algo": args.algo,
                   "created": time.time(), "files": results}, open(args.out, "w"), indent=1)
        print(f"manifest -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
