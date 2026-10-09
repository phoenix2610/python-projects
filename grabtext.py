#!/usr/bin/env python3
"""Select a region of the screen, OCR it, and put the text on your clipboard.

    grabtext.py                    # drag a box, text lands on the clipboard
    grabtext.py --image bug.png --lang eng+deu
    grabtext.py --code             # keep line breaks and indentation for code
    grabtext.py --dry-run          # print the pipeline it would run

Auto-detects the stack it is on: grim+slurp on Wayland, maim+slop on X11,
screencapture on macOS, then tesseract for OCR and wl-copy/xclip/pbcopy for the
clipboard. Each dependency is checked before use with a message that tells you
exactly what to install.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

CAPTURE = [
    ("grim", lambda out, region: (["grim"] + (["-g", region] if region else []) + [out], "slurp")),
    ("maim", lambda out, region: (["maim"] + (["-g", region] if region else ["-s"]) + [out], "slop")),
    ("screencapture", lambda out, region: (["screencapture", "-i", out], None)),
]
CLIPBOARD = [(["wl-copy"], "wayland"), (["xclip", "-selection", "clipboard"], "x11"), (["pbcopy"], "macos")]


def need(binary: str, hint: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise SystemExit(f"missing {binary} — install it ({hint})")
    return path


def pick_capture() -> tuple[str, callable, str | None]:
    for name, builder in CAPTURE:
        if shutil.which(name):
            _, picker = builder("x", None)
            return name, builder, picker
    raise SystemExit("no screen capture tool found — install grim (Wayland), maim (X11) or use macOS")


def select_region(picker: str | None) -> str | None:
    """Ask the user to drag a box; returns a geometry string the capture tool understands."""
    if not picker or not shutil.which(picker):
        return None
    proc = subprocess.run([picker], capture_output=True, text=True)
    if proc.returncode or not proc.stdout.strip():
        raise SystemExit("selection cancelled")
    return proc.stdout.strip()


def clean(text: str, code_mode: bool) -> str:
    if code_mode:
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
        return "\n".join(ln[indent:] for ln in lines)
    # prose: unwrap hard line breaks that are just the screenshot's width
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"(?<![.!?:])\n(?!\n)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def to_clipboard(text: str) -> str | None:
    for cmd, label in CLIPBOARD:
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True, check=True)
            return label
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", help="OCR this file instead of capturing the screen")
    ap.add_argument("--lang", default="eng", help="tesseract language(s), e.g. eng+deu")
    ap.add_argument("--psm", type=int, default=6, help="tesseract page segmentation mode (6 = uniform block)")
    ap.add_argument("--code", action="store_true", help="preserve line breaks and dedent")
    ap.add_argument("--keep", help="also save the captured image here")
    ap.add_argument("--print", dest="show", action="store_true", help="print the text as well as copying it")
    ap.add_argument("--dry-run", action="store_true", help="show the commands without running them")
    args = ap.parse_args()

    tmp = args.image
    plan: list[str] = []
    if not args.image:
        name, builder, picker = pick_capture()
        tmp = os.path.join(tempfile.mkdtemp(prefix="grabtext-"), "shot.png")
        region = None if args.dry_run else select_region(picker)
        cmd, _ = builder(tmp, region)
        plan.append(" ".join(cmd))
        if not args.dry_run:
            subprocess.run(cmd, check=True)

    ocr = ["tesseract", tmp or "shot.png", "stdout", "--psm", str(args.psm), "-l", args.lang]
    plan.append(" ".join(ocr))
    plan.append(next((" ".join(c) for c, _ in CLIPBOARD if shutil.which(c[0])), "wl-copy | xclip | pbcopy"))
    if args.dry_run:
        for step in plan:
            print(f"  {step}")
        return 0

    need("tesseract", "pacman -S tesseract tesseract-data-eng | brew install tesseract")
    proc = subprocess.run(ocr, capture_output=True, text=True)
    if proc.returncode:
        print(proc.stderr.strip(), file=sys.stderr)
        return 1

    text = clean(proc.stdout, args.code)
    if not text.strip():
        print("no text recognised — try a tighter crop or --psm 7 for a single line", file=sys.stderr)
        return 1
    where = to_clipboard(text)
    if args.keep and tmp:
        shutil.copy(tmp, args.keep)
    if args.show or not where:
        print(text)
    words = len(text.split())
    print(f"\n{words} words {'copied to the ' + where + ' clipboard' if where else '(no clipboard tool found)'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
