#!/usr/bin/env python3
"""Rename photos to their capture time, read straight out of the JPEG's EXIF block.

    exifrename.py ~/Pictures/dump                 # dry run, prints the plan
    exifrename.py ~/Pictures/dump --apply --into '%Y/%m'

No Pillow: this walks the JPEG segment list to APP1, parses the TIFF header and
IFD entries by hand, and pulls tag 0x9003 (DateTimeOriginal). Files without EXIF
fall back to mtime only when you pass --fallback-mtime.
"""
from __future__ import annotations

import argparse
import os
import struct
from datetime import datetime

DATETIME_ORIGINAL = 0x9003
DATETIME_DIGITIZED = 0x9004
DATETIME = 0x0132
EXIF_IFD_POINTER = 0x8769
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _read_ifd(blob: bytes, offset: int, endian: str, want: set[int]) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    if offset + 2 > len(blob):
        return found
    (count,) = struct.unpack_from(endian + "H", blob, offset)
    for i in range(count):
        entry = offset + 2 + i * 12
        if entry + 12 > len(blob):
            break
        tag, typ, n = struct.unpack_from(endian + "HHI", blob, entry)
        if tag not in want:
            continue
        size = TYPE_SIZES.get(typ, 1) * n
        if size <= 4:
            value = blob[entry + 8 : entry + 8 + size]
        else:
            (ptr,) = struct.unpack_from(endian + "I", blob, entry + 8)
            value = blob[ptr : ptr + size]
        found[tag] = value
    return found


def exif_datetime(path: str) -> datetime | None:
    """Return DateTimeOriginal from a JPEG, or None if it has no usable EXIF."""
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":  # not a JPEG
            return None
        while True:
            marker = fh.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            if marker[1] in (0xD9, 0xDA):  # end of image / start of scan
                return None
            (length,) = struct.unpack(">H", fh.read(2))
            payload = fh.read(length - 2)
            if marker[1] != 0xE1 or not payload.startswith(b"Exif\x00\x00"):
                continue
            tiff = payload[6:]
            if len(tiff) < 8:
                return None
            endian = "<" if tiff[:2] == b"II" else ">"
            (ifd0,) = struct.unpack_from(endian + "I", tiff, 4)
            want = {DATETIME, EXIF_IFD_POINTER}
            entries = _read_ifd(tiff, ifd0, endian, want)
            if EXIF_IFD_POINTER in entries:
                (sub,) = struct.unpack(endian + "I", entries[EXIF_IFD_POINTER][:4])
                entries |= _read_ifd(tiff, sub, endian, {DATETIME_ORIGINAL, DATETIME_DIGITIZED})
            for tag in (DATETIME_ORIGINAL, DATETIME_DIGITIZED, DATETIME):
                raw = entries.get(tag)
                if not raw:
                    continue
                text = raw.split(b"\x00")[0].decode("ascii", "ignore")
                try:
                    return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue
            return None


def unique(path: str, taken: set[str]) -> str:
    if path not in taken and not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while f"{stem}-{n}{ext}" in taken or os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--pattern", default="%Y%m%d-%H%M%S", help="strftime pattern for the new name")
    ap.add_argument("--into", default=None, help="strftime pattern for subfolders, e.g. '%%Y/%%m'")
    ap.add_argument("--apply", action="store_true", help="actually rename (default is a dry run)")
    ap.add_argument("--fallback-mtime", action="store_true", help="use mtime when EXIF is missing")
    args = ap.parse_args()

    taken: set[str] = set()
    renamed = skipped = 0
    for entry in sorted(os.scandir(args.folder), key=lambda e: e.name):
        if not entry.is_file() or os.path.splitext(entry.name)[1].lower() not in (".jpg", ".jpeg"):
            continue
        stamp = exif_datetime(entry.path)
        source = "exif"
        if stamp is None and args.fallback_mtime:
            stamp, source = datetime.fromtimestamp(entry.stat().st_mtime), "mtime"
        if stamp is None:
            print(f"  skip   {entry.name} (no EXIF)")
            skipped += 1
            continue
        folder = os.path.join(args.folder, stamp.strftime(args.into)) if args.into else args.folder
        target = unique(os.path.join(folder, stamp.strftime(args.pattern) + os.path.splitext(entry.name)[1].lower()), taken)
        taken.add(target)
        rel = os.path.relpath(target, args.folder)
        print(f"  {'rename' if args.apply else 'would '} {entry.name} -> {rel}  [{source}]")
        if args.apply:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(entry.path, target)
        renamed += 1

    print(f"\n{renamed} to rename, {skipped} skipped" + ("" if args.apply else "  (dry run — pass --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
