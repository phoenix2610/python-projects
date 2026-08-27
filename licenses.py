#!/usr/bin/env python3
"""Walk an installed dependency tree and flag licences outside your allow list.

    licenses.py node_modules --allow MIT,ISC,Apache-2.0,BSD-2-Clause,BSD-3-Clause
    licenses.py . --python --deny GPL-3.0,AGPL-3.0 --json report.json

Reads what is actually installed rather than what the manifest requests, because
the transitive dependency that pulled in a copyleft licence is never the one you
wrote down. Falls back to matching the LICENSE file's text when a package has no
declared licence field.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

PERMISSIVE = {"MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "0BSD", "Unlicense", "CC0-1.0"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "MPL-2.0", "EPL-2.0", "SSPL-1.0"}
FINGERPRINTS = [
    (re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE", re.I), "AGPL-3.0"),
    (re.compile(r"GNU LESSER GENERAL PUBLIC", re.I), "LGPL-3.0"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 3", re.I), "GPL-3.0"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 2", re.I), "GPL-2.0"),
    (re.compile(r"Mozilla Public License Version 2", re.I), "MPL-2.0"),
    (re.compile(r"Apache License\s+Version 2\.0", re.I), "Apache-2.0"),
    (re.compile(r"Permission is hereby granted, free of charge", re.I), "MIT"),
    (re.compile(r"Redistribution and use in source and binary forms.*?3\. Neither", re.I | re.S), "BSD-3-Clause"),
    (re.compile(r"Redistribution and use in source and binary forms", re.I), "BSD-2-Clause"),
    (re.compile(r"This is free and unencumbered software released into the public domain", re.I), "Unlicense"),
]


def sniff_license_file(folder: str) -> str | None:
    for name in os.listdir(folder):
        if re.match(r"(LICEN[CS]E|COPYING)", name, re.I):
            try:
                text = open(os.path.join(folder, name), encoding="utf-8", errors="replace").read(6000)
            except OSError:
                continue
            for pattern, spdx in FINGERPRINTS:
                if pattern.search(text):
                    return spdx
            return "UNKNOWN (has a LICENSE file we cannot identify)"
    return None


def normalise(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("type") or value.get("name") or ""
    if isinstance(value, list):
        value = " OR ".join(normalise(v) for v in value)
    return str(value).strip() or ""


def scan_node(root: str) -> list[dict]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "package.json" not in filenames or os.path.basename(dirpath).startswith("."):
            continue
        try:
            pkg = json.load(open(os.path.join(dirpath, "package.json"), encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not pkg.get("name") or not pkg.get("version"):
            continue
        spdx = normalise(pkg.get("license") or pkg.get("licenses")) or sniff_license_file(dirpath) or "MISSING"
        found.append({"name": pkg["name"], "version": pkg.get("version", "?"), "license": spdx,
                      "path": os.path.relpath(dirpath, root),
                      "repo": normalise(pkg.get("repository", {}) if isinstance(pkg.get("repository"), dict) else pkg.get("repository", ""))})
    return found


def scan_python(root: str) -> list[dict]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not dirpath.endswith((".dist-info", ".egg-info")):
            continue
        meta = os.path.join(dirpath, "METADATA")
        if not os.path.exists(meta):
            meta = os.path.join(dirpath, "PKG-INFO")
        if not os.path.exists(meta):
            continue
        fields: dict[str, str] = {}
        for line in open(meta, encoding="utf-8", errors="replace"):
            if not line.strip():
                break
            key, _, value = line.partition(":")
            fields.setdefault(key.strip().lower(), value.strip())
            if key.strip() == "Classifier" and "License ::" in value:
                fields["classifier-license"] = value.split("::")[-1].strip()
        spdx = fields.get("license") or fields.get("classifier-license") or sniff_license_file(dirpath) or "MISSING"
        found.append({"name": fields.get("name", "?"), "version": fields.get("version", "?"),
                      "license": spdx, "path": os.path.relpath(dirpath, root), "repo": fields.get("home-page", "")})
    return found


def classify(spdx: str, allow: set[str], deny: set[str]) -> str:
    tokens = set(re.split(r"\s+(?:OR|AND)\s+|[()]", spdx.upper().replace("-ONLY", "").replace("-OR-LATER", "")))
    tokens = {t.strip() for t in tokens if t.strip()}
    upper_deny = {d.upper() for d in deny}
    upper_allow = {a.upper() for a in allow}
    if tokens & upper_deny:
        return "denied"
    if spdx.startswith(("MISSING", "UNKNOWN")):
        return "unknown"
    if upper_allow and not (tokens & upper_allow):
        return "review"
    if not upper_allow and tokens & {c.upper() for c in COPYLEFT}:
        return "review"
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--python", action="store_true", help="scan installed Python distributions instead of node_modules")
    ap.add_argument("--allow", default="", help="comma-separated SPDX ids that pass without review")
    ap.add_argument("--deny", default=",".join(sorted(COPYLEFT & {"GPL-3.0", "AGPL-3.0", "SSPL-1.0"})))
    ap.add_argument("--json", dest="json_out", help="also write the full report here")
    ap.add_argument("--quiet", action="store_true", help="only print packages needing attention")
    args = ap.parse_args()

    packages = scan_python(args.root) if args.python else scan_node(args.root)
    if not packages:
        print(f"no packages found under {args.root}", file=sys.stderr)
        return 1
    allow = {a.strip() for a in args.allow.split(",") if a.strip()}
    deny = {d.strip() for d in args.deny.split(",") if d.strip()}

    for pkg in packages:
        pkg["status"] = classify(pkg["license"], allow, deny)
    packages.sort(key=lambda p: (["denied", "unknown", "review", "ok"].index(p["status"]), p["name"]))

    tally = Counter(p["status"] for p in packages)
    licenses = Counter(p["license"] for p in packages)
    width = max(len(p["name"]) for p in packages)
    for pkg in packages:
        if args.quiet and pkg["status"] == "ok":
            continue
        marker = {"denied": "DENY ", "unknown": "?    ", "review": "check", "ok": "ok   "}[pkg["status"]]
        print(f"  {marker} {pkg['name'].ljust(width)} {pkg['version'].ljust(10)} {pkg['license']}")

    print(f"\n{len(packages)} packages: " + ", ".join(f"{n} {s}" for s, n in tally.most_common()))
    print("licences: " + ", ".join(f"{lic} ({n})" for lic, n in licenses.most_common(8)))
    if args.json_out:
        json.dump({"root": os.path.abspath(args.root), "summary": dict(tally), "packages": packages},
                  open(args.json_out, "w"), indent=2)
        print(f"report -> {args.json_out}")
    return 1 if tally["denied"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
