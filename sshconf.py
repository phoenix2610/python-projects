#!/usr/bin/env python3
"""Read, edit and test ~/.ssh/config without hand-editing it and hoping.

    sshconf.py ls
    sshconf.py add prod --host 10.0.4.7 --user deploy --key ~/.ssh/deploy --jump bastion
    sshconf.py test prod
    sshconf.py rm old-box

Rewrites are atomic and always leave a timestamped backup, because a mangled ssh
config is the kind of mistake you discover when you most need to log in.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time

CONFIG = os.path.expanduser(os.environ.get("SSH_CONFIG", "~/.ssh/config"))
FIELDS = {"host": "HostName", "user": "User", "port": "Port", "key": "IdentityFile",
          "jump": "ProxyJump", "forward-agent": "ForwardAgent"}


class Block:
    def __init__(self, alias: str):
        self.alias = alias
        self.options: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        for k, v in self.options:
            if k.lower() == key.lower():
                return v
        return None

    def set(self, key: str, value: str) -> None:
        for i, (k, _) in enumerate(self.options):
            if k.lower() == key.lower():
                self.options[i] = (key, value)
                return
        self.options.append((key, value))

    def render(self) -> str:
        body = "".join(f"    {k} {v}\n" for k, v in self.options)
        return f"Host {self.alias}\n{body}"


def parse(path: str) -> tuple[list[str], list[Block]]:
    preamble: list[str] = []
    blocks: list[Block] = []
    if not os.path.exists(path):
        return preamble, blocks
    for line in open(path):
        stripped = line.strip()
        if re.match(r"^host\s+", stripped, re.I):
            blocks.append(Block(stripped.split(None, 1)[1]))
        elif blocks and stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition(" ")
            blocks[-1].options.append((key, value.strip()))
        elif not blocks:
            preamble.append(line.rstrip("\n"))
    return preamble, blocks


def write(path: str, preamble: list[str], blocks: list[Block]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = ""
    if os.path.exists(path):
        backup = f"{path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        if preamble:
            fh.write("\n".join(preamble).rstrip() + "\n\n")
        fh.write("\n".join(b.render() for b in blocks))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return backup


def resolve(alias: str, blocks: list[Block]) -> Block | None:
    for b in blocks:
        if b.alias == alias:
            return b
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    for name in ("add", "set"):
        p = sub.add_parser(name, help="create or update a host entry")
        p.add_argument("alias")
        for flag in FIELDS:
            p.add_argument(f"--{flag}")
    p = sub.add_parser("rm"); p.add_argument("alias")
    p = sub.add_parser("test", help="TCP-connect and, if ssh is installed, try auth")
    p.add_argument("alias", nargs="?"); p.add_argument("--timeout", type=float, default=4.0)
    args = ap.parse_args()

    preamble, blocks = parse(CONFIG)

    if args.cmd == "ls":
        if not blocks:
            print(f"no hosts in {CONFIG}")
            return 0
        width = max(len(b.alias) for b in blocks)
        for b in blocks:
            target = b.get("HostName") or "-"
            user = b.get("User")
            port = b.get("Port")
            extra = " ".join(filter(None, [
                f"via {b.get('ProxyJump')}" if b.get("ProxyJump") else "",
                f"key {os.path.basename(b.get('IdentityFile'))}" if b.get("IdentityFile") else "",
            ]))
            dest = f"{user + '@' if user else ''}{target}{':' + port if port else ''}"
            print(f"{b.alias.ljust(width)}  {dest}  {extra}".rstrip())
        return 0

    if args.cmd in ("add", "set"):
        block = resolve(args.alias, blocks)
        if block and args.cmd == "add":
            print(f"{args.alias!r} already exists — use `set` to change it", file=sys.stderr)
            return 1
        if not block:
            block = Block(args.alias)
            blocks.append(block)
        for flag, option in FIELDS.items():
            value = getattr(args, flag.replace("-", "_"))
            if value:
                block.set(option, os.path.expanduser(value) if flag == "key" else value)
        if not block.options:
            print("nothing to set — pass at least one of " + ", ".join(f"--{f}" for f in FIELDS), file=sys.stderr)
            return 1
        backup = write(CONFIG, preamble, blocks)
        print(block.render().rstrip())
        print(f"\nwritten to {CONFIG}" + (f" (backup {os.path.basename(backup)})" if backup else ""))
        return 0

    if args.cmd == "rm":
        block = resolve(args.alias, blocks)
        if not block:
            print(f"no host {args.alias!r}", file=sys.stderr)
            return 1
        blocks.remove(block)
        backup = write(CONFIG, preamble, blocks)
        print(f"removed {args.alias} (backup {os.path.basename(backup)})")
        return 0

    targets = [resolve(args.alias, blocks)] if args.alias else blocks
    failures = 0
    for block in targets:
        if block is None:
            print(f"no host {args.alias!r}", file=sys.stderr)
            return 1
        host = block.get("HostName") or block.alias
        port = int(block.get("Port") or 22)
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=args.timeout) as sock:
                banner = sock.recv(64).decode("ascii", "ignore").strip()
            ms = (time.perf_counter() - start) * 1000
            print(f"  ok    {block.alias.ljust(14)} {host}:{port}  {ms:.0f}ms  {banner}")
        except OSError as exc:
            print(f"  FAIL  {block.alias.ljust(14)} {host}:{port}  {exc}")
            failures += 1
            continue
        if shutil.which("ssh") and args.alias:
            probe = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(args.timeout)}",
                                    block.alias, "true"], capture_output=True, text=True)
            print(f"  auth  {'ok' if probe.returncode == 0 else probe.stderr.strip().splitlines()[-1:] or 'failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
