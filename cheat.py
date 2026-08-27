#!/usr/bin/env python3
"""Your own cheatsheets, one keystroke away, with the examples you actually use.

    cheat.py tar                 # show the sheet
    cheat.py --search rebase     # grep every sheet, ranked
    cheat.py edit ffmpeg         # open in $EDITOR (creates from a template)
    cheat.py --copy tar 3        # put entry 3 on the clipboard

Sheets are plain markdown in ~/.cheat: a `## heading` starts a section and each
`code` line under it is a numbered entry, so a sheet stays readable in any editor
and still gets structure here.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser(os.environ.get("CHEAT_DIR", "~/.cheat"))
BOLD, DIM, CYAN, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[0m"
TEMPLATE = """# {name}

## basics

    # describe what this does
    {name} --help

## recipes

"""


def sheets() -> list[str]:
    if not os.path.isdir(HOME):
        return []
    return sorted(f[:-3] for f in os.listdir(HOME) if f.endswith(".md"))


def path_for(name: str) -> str:
    return os.path.join(HOME, f"{name}.md")


def parse(name: str) -> list[tuple[str, str, str]]:
    """Return [(section, command, comment)] entries in file order."""
    entries: list[tuple[str, str, str]] = []
    section = ""
    pending: list[str] = []
    for line in open(path_for(name)):
        if line.startswith("##"):
            section = line.lstrip("#").strip()
            pending = []
        elif line.startswith(("    ", "\t")) and line.strip():
            body = line.strip()
            if body.startswith("#"):
                pending.append(body.lstrip("# "))
            else:
                entries.append((section, body, " ".join(pending)))
                pending = []
    return entries


def copy(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True)
            return True
    return False


def render(name: str, color: bool, highlight: str = "") -> None:
    entries = parse(name)
    section = None
    print(f"{BOLD if color else ''}{name}{RESET if color else ''}  ({len(entries)} entries)")
    for i, (sec, cmd, comment) in enumerate(entries, 1):
        if sec != section:
            section = sec
            print(f"\n  {DIM if color else ''}{section}{RESET if color else ''}")
        shown = cmd
        if highlight and color:
            shown = re.sub(f"({re.escape(highlight)})", f"{CYAN}\\1{RESET}", shown, flags=re.I)
        print(f"    {i:>2}. {shown}")
        if comment:
            print(f"        {DIM if color else ''}{comment}{RESET if color else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("topic", nargs="?", help="sheet to show, or `edit <name>`")
    ap.add_argument("index", nargs="?", help="entry number (with --copy) or sheet name (with edit)")
    ap.add_argument("--search", "-s", help="search across every sheet")
    ap.add_argument("--copy", "-c", action="store_true", help="copy the chosen entry to the clipboard")
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    os.makedirs(HOME, exist_ok=True)
    color = not args.no_color and sys.stdout.isatty()

    if args.list or (not args.topic and not args.search):
        names = sheets()
        if not names:
            print(f"no sheets yet — try `cheat.py edit git` ({HOME})")
            return 0
        for name in names:
            print(f"  {name.ljust(16)} {len(parse(name))} entries")
        return 0

    if args.topic == "edit":
        name = args.index
        if not name:
            print("usage: cheat.py edit <name>", file=sys.stderr)
            return 1
        target = path_for(name)
        if not os.path.exists(target):
            open(target, "w").write(TEMPLATE.format(name=name))
        subprocess.run([os.environ.get("EDITOR", "vi"), target])
        return 0

    if args.search:
        hits = []
        for name in sheets():
            for i, (sec, cmd, comment) in enumerate(parse(name), 1):
                haystack = f"{cmd} {comment} {sec}".lower()
                if args.search.lower() in haystack:
                    score = 2 if args.search.lower() in cmd.lower() else 1
                    hits.append((score, name, i, cmd, comment))
        if not hits:
            print(f"nothing matches {args.search!r}")
            return 1
        for _, name, i, cmd, comment in sorted(hits, key=lambda h: -h[0]):
            shown = re.sub(f"({re.escape(args.search)})", f"{CYAN}\\1{RESET}", cmd, flags=re.I) if color else cmd
            print(f"  {name}:{i}  {shown}")
            if comment:
                print(f"      {DIM if color else ''}{comment}{RESET if color else ''}")
        return 0

    if args.topic not in sheets():
        near = [s for s in sheets() if args.topic in s]
        print(f"no sheet {args.topic!r}" + (f" — did you mean {', '.join(near)}?" if near else ""), file=sys.stderr)
        return 1

    if args.copy and args.index:
        entries = parse(args.topic)
        index = int(args.index) if args.index.isdigit() else 0
        if not 1 <= index <= len(entries):
            print(f"entry {args.index} out of range (1-{len(entries)})", file=sys.stderr)
            return 1
        cmd = entries[index - 1][1]
        print(cmd if not copy(cmd) else f"copied: {cmd}")
        return 0

    render(args.topic, color)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
