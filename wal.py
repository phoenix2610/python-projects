#!/usr/bin/env python3
"""A durable key-value store: append to a log, fsync, replay on start, checkpoint to compact.

    wal.py --demo
    wal.py put user:1 '{"name":"ana"}' --dir ./data
    wal.py get user:1 --dir ./data
    wal.py recover --dir ./data --verbose

Every record is length-prefixed and CRC-tagged, so a write torn by a crash is
detected at replay and the log is truncated to the last intact record rather than
loading garbage. That is the whole contract of a WAL: a write is durable once
fsync returns, and never half-applied.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import zlib

HEADER = struct.Struct("<IIB")     # crc32, payload length, op
PUT, DELETE, CHECKPOINT = 1, 2, 3


class Store:
    def __init__(self, directory: str, fsync: bool = True):
        self.dir = directory
        self.fsync = fsync
        os.makedirs(directory, exist_ok=True)
        self.log_path = os.path.join(directory, "wal.log")
        self.snapshot_path = os.path.join(directory, "snapshot.json")
        self.data: dict[str, str] = {}
        self.applied = 0
        self.truncated_at: int | None = None
        self._replay()
        self.log = open(self.log_path, "ab", buffering=0)

    def _write(self, op: int, payload: bytes) -> None:
        record = HEADER.pack(zlib.crc32(payload), len(payload), op) + payload
        self.log.write(record)
        if self.fsync:
            os.fsync(self.log.fileno())   # the line between "written" and "durable"

    def _replay(self) -> None:
        if os.path.exists(self.snapshot_path):
            self.data = json.load(open(self.snapshot_path))
        if not os.path.exists(self.log_path):
            return
        with open(self.log_path, "rb") as fh:
            raw = fh.read()
        pos = 0
        while pos + HEADER.size <= len(raw):
            crc, length, op = HEADER.unpack_from(raw, pos)
            body = raw[pos + HEADER.size: pos + HEADER.size + length]
            if len(body) < length or zlib.crc32(body) != crc:
                self.truncated_at = pos       # torn tail: everything after this is unusable
                break
            payload = json.loads(body)
            if op == PUT:
                self.data[payload["k"]] = payload["v"]
            elif op == DELETE:
                self.data.pop(payload["k"], None)
            elif op == CHECKPOINT:
                pass
            self.applied += 1
            pos += HEADER.size + length
        if self.truncated_at is not None:
            with open(self.log_path, "r+b") as fh:
                fh.truncate(self.truncated_at)

    def put(self, key: str, value: str) -> None:
        self._write(PUT, json.dumps({"k": key, "v": value}).encode())
        self.data[key] = value

    def delete(self, key: str) -> None:
        self._write(DELETE, json.dumps({"k": key}).encode())
        self.data.pop(key, None)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def checkpoint(self) -> int:
        """Fold the log into a snapshot and start a fresh log — bounded recovery time."""
        tmp = self.snapshot_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.snapshot_path)
        size = self.log.tell()
        self.log.close()
        os.remove(self.log_path)
        self.log = open(self.log_path, "ab", buffering=0)
        self._write(CHECKPOINT, json.dumps({"at": time.time(), "keys": len(self.data)}).encode())
        return size

    def close(self) -> None:
        self.log.close()


def demo() -> int:
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="wal-")
    try:
        store = Store(root)
        for i in range(1000):
            store.put(f"key:{i}", f"value-{i}")
        store.delete("key:500")
        print(f"  wrote 1000 keys + 1 delete, log is {os.path.getsize(store.log_path) // 1024}KB")
        store.close()

        reopened = Store(root)
        print(f"  reopened: {len(reopened.data)} keys recovered, {reopened.applied} records replayed, "
              f"key:500 = {reopened.get('key:500')}")
        size = reopened.checkpoint()
        print(f"  checkpoint: folded {size // 1024}KB of log into a snapshot; "
              f"log is now {os.path.getsize(reopened.log_path)}B")
        reopened.put("after:checkpoint", "yes")
        reopened.close()

        # simulate a crash mid-write by appending a truncated record
        with open(os.path.join(root, "wal.log"), "ab") as fh:
            payload = json.dumps({"k": "torn", "v": "x" * 200}).encode()
            fh.write(HEADER.pack(zlib.crc32(payload), len(payload), PUT) + payload[:80])
        crashed = Store(root)
        print(f"  after a torn write: log truncated at byte {crashed.truncated_at}, "
              f"'torn' present = {'torn' in crashed.data}, "
              f"'after:checkpoint' survived = {crashed.get('after:checkpoint') == 'yes'}")
        crashed.close()

        durable = Store(root, fsync=True)
        start = time.perf_counter()
        for i in range(200):
            durable.put(f"sync:{i}", "v")
        sync_ms = (time.perf_counter() - start) * 1000 / 200
        durable.close()
        loose = Store(root, fsync=False)
        start = time.perf_counter()
        for i in range(200):
            loose.put(f"nosync:{i}", "v")
        nosync_ms = (time.perf_counter() - start) * 1000 / 200
        loose.close()
        print(f"  cost of durability: {sync_ms:.3f}ms per write with fsync, "
              f"{nosync_ms:.3f}ms without ({sync_ms / max(nosync_ms, 1e-6):.0f}x)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["put", "get", "del", "list", "checkpoint", "recover"])
    ap.add_argument("key", nargs="?"); ap.add_argument("value", nargs="?")
    ap.add_argument("--dir", default="./wal-data")
    ap.add_argument("--no-fsync", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    store = Store(args.dir, fsync=not args.no_fsync)
    if args.verbose or args.cmd == "recover":
        print(f"  replayed {store.applied} records, {len(store.data)} live keys"
              + (f", truncated a torn record at byte {store.truncated_at}" if store.truncated_at is not None else ""))
    if args.cmd == "put":
        store.put(args.key, args.value)
        print(f"  {args.key} = {args.value}")
    elif args.cmd == "get":
        value = store.get(args.key)
        print(value if value is not None else f"  {args.key} not found")
        return 0 if value is not None else 1
    elif args.cmd == "del":
        store.delete(args.key)
        print(f"  deleted {args.key}")
    elif args.cmd == "list":
        for k, v in sorted(store.data.items()):
            print(f"  {k} = {v}")
    elif args.cmd == "checkpoint":
        print(f"  compacted {store.checkpoint()} bytes of log")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
