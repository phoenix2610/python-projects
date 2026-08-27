#!/usr/bin/env python3
"""Subscribe to podcast feeds, fetch what is new, and tag the files as you go.

    podfetch.py add https://example.com/feed.xml --into ~/Podcasts
    podfetch.py sync --limit 3
    podfetch.py ls

Downloads resume with a Range request if a partial file is left behind, and each
MP3 gets an ID3v2.4 tag written from the feed (title, show, date, episode) so
players show something better than "track01".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

STATE = os.path.expanduser(os.environ.get("PODFETCH_STATE", "~/.podfetch.json"))
UA = {"User-Agent": "podfetch/1.0"}
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def load() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"feeds": []}


def save(state: dict) -> None:
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1)


def slug(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s_-]+", "-", text)[:limit].strip("-").lower() or "episode"


def synchsafe(n: int) -> bytes:
    """ID3v2 sizes are 28-bit: 7 usable bits per byte so no byte can look like a frame sync."""
    return bytes((n >> 21 & 0x7F, n >> 14 & 0x7F, n >> 7 & 0x7F, n & 0x7F))


def id3_tag(fields: dict[str, str]) -> bytes:
    frames = b""
    for frame_id, value in fields.items():
        if not value:
            continue
        payload = b"\x03" + value.encode("utf-8") + b"\x00"   # 0x03 = UTF-8
        frames += frame_id.encode("ascii") + synchsafe(len(payload)) + b"\x00\x00" + payload
    return b"ID3\x04\x00\x00" + synchsafe(len(frames)) + frames


def parse_feed(url: str) -> tuple[str, list[dict]]:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        root = ET.fromstring(resp.read())
    channel = root.find("channel")
    if channel is None:
        channel = root
    show = (channel.findtext("title") or "podcast").strip()
    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        episodes.append({
            "title": (item.findtext("title") or "untitled").strip(),
            "url": enclosure.get("url"),
            "guid": (item.findtext("guid") or enclosure.get("url")).strip(),
            "date": (item.findtext("pubDate") or "").strip(),
            "episode": (item.findtext(ITUNES + "episode") or "").strip(),
            "bytes": int(enclosure.get("length") or 0),
        })
    return show, episodes


def download(url: str, path: str) -> int:
    """Fetch to `path`, resuming from a .part file when the server supports Range."""
    part = path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = dict(UA)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        mode = "ab" if resp.status == 206 else "wb"
        if mode == "wb":
            have = 0
        total = int(resp.headers.get("Content-Length") or 0) + have
        with open(part, mode) as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                have += len(chunk)
                if total and sys.stderr.isatty():
                    print(f"\r  {have * 100 // total:3d}%", end="", file=sys.stderr)
    os.replace(part, path)
    return have


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("url"); a.add_argument("--into", default=".")
    s = sub.add_parser("sync"); s.add_argument("--limit", type=int, default=5); s.add_argument("--dry-run", action="store_true")
    sub.add_parser("ls")
    args = ap.parse_args()
    state = load()

    if args.cmd == "add":
        show, episodes = parse_feed(args.url)
        state["feeds"].append({"url": args.url, "show": show,
                               "dir": os.path.abspath(os.path.expanduser(args.into)), "seen": []})
        save(state)
        print(f"added {show} ({len(episodes)} episodes available)")
        return 0

    if args.cmd == "ls":
        for feed in state["feeds"]:
            print(f"{feed['show']}  {len(feed['seen'])} downloaded  -> {feed['dir']}")
        return 0

    for feed in state["feeds"]:
        show, episodes = parse_feed(feed["url"])
        fresh = [e for e in episodes if e["guid"] not in feed["seen"]][: args.limit]
        print(f"{show}: {len(fresh)} new")
        os.makedirs(feed["dir"], exist_ok=True)
        for ep in fresh:
            ext = os.path.splitext(ep["url"].split("?")[0])[1] or ".mp3"
            path = os.path.join(feed["dir"], f"{slug(ep['title'])}{ext}")
            print(f"  {ep['title']}")
            if args.dry_run:
                continue
            size = download(ep["url"], path)
            if ext == ".mp3":
                with open(path, "rb") as fh:
                    body = fh.read()
                year = ""
                try:
                    year = str(datetime.strptime(ep["date"][:16], "%a, %d %b %Y").year)
                except ValueError:
                    pass
                tag = id3_tag({"TIT2": ep["title"], "TALB": show, "TPE1": show,
                               "TDRC": year, "TRCK": ep["episode"], "TCON": "Podcast"})
                with open(path, "wb") as fh:
                    fh.write(tag + body)
            feed["seen"].append(ep["guid"])
            print(f"    {size // 1024}KB -> {path}")
        save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
