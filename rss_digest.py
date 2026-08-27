#!/usr/bin/env python3
"""Aggregate RSS/Atom feeds, deduplicate stories, and produce one readable digest.

    rss_digest.py https://example.com/feed.xml https://other.com/rss --hours 24
    rss_digest.py --demo

Parses both RSS 2.0 and Atom with the stdlib's xml.etree (no feedparser),
handling the namespace and tag-naming differences between them in one function
rather than two. Dedup matters more than parsing here: the same wire story
often runs on three feeds with slightly different titles, so entries are
grouped by a normalized-title fingerprint (lowercased, punctuation stripped,
common suffixes like " - Reuters" removed) rather than exact string match,
and only the earliest-published copy survives into the digest.
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass
class FeedItem:
    title: str
    link: str
    published: datetime
    source: str
    summary: str = ""


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


def parse_rss(root: ET.Element, source: str) -> list[FeedItem]:
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_text = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub_date_text) if pub_date_text else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        summary = strip_html(item.findtext("description") or "")
        if title and link:
            items.append(FeedItem(title, link, published, source, summary[:200]))
    return items


def parse_atom(root: ET.Element, source: str) -> list[FeedItem]:
    items = []
    for entry in root.iter(f"{ATOM_NS}entry"):
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        link_el = entry.find(f"{ATOM_NS}link")
        link = link_el.get("href", "") if link_el is not None else ""
        updated_text = entry.findtext(f"{ATOM_NS}updated") or entry.findtext(f"{ATOM_NS}published")
        try:
            published = datetime.fromisoformat(updated_text.replace("Z", "+00:00")) if updated_text else datetime.now(timezone.utc)
        except ValueError:
            published = datetime.now(timezone.utc)
        summary_raw = entry.findtext(f"{ATOM_NS}summary") or entry.findtext(f"{ATOM_NS}content") or ""
        if title and link:
            items.append(FeedItem(title, link, published, source, strip_html(summary_raw)[:200]))
    return items


def parse_feed(xml_text: str, source: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text)
    if root.tag == f"{ATOM_NS}feed":
        return parse_atom(root, source)
    return parse_rss(root, source)


SUFFIX_RE = re.compile(r"\s*[-|—]\s*[A-Za-z .]+$")
PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_title(title: str) -> str:
    """Fingerprint a title for dedup: strip trailing " - Source Name", punctuation,
    case, and collapse whitespace, so near-identical wire stories collapse together."""
    stripped = SUFFIX_RE.sub("", title)
    stripped = PUNCT_RE.sub("", stripped).lower()
    return re.sub(r"\s+", " ", stripped).strip()


def dedupe_and_filter(items: list[FeedItem], since: datetime) -> list[FeedItem]:
    recent = [i for i in items if i.published >= since]
    by_fingerprint: dict[str, FeedItem] = {}
    for item in recent:
        fp = normalize_title(item.title)
        existing = by_fingerprint.get(fp)
        if existing is None or item.published < existing.published:
            by_fingerprint[fp] = item  # keep whichever copy ran first
    return sorted(by_fingerprint.values(), key=lambda i: i.published, reverse=True)


def format_digest(items: list[FeedItem], since: datetime, now: datetime | None = None) -> str:
    if not items:
        return f"No new stories since {since.strftime('%Y-%m-%d %H:%M UTC')}."

    now = now or datetime.now(timezone.utc)
    lines = [f"{len(items)} stories since {since.strftime('%Y-%m-%d %H:%M UTC')}\n"]
    by_source: dict[str, int] = {}
    for item in items:
        by_source[item.source] = by_source.get(item.source, 0) + 1

    for item in items:
        age_hours = (now - item.published).total_seconds() / 3600
        lines.append(f"[{item.source}] {item.title}  ({age_hours:.0f}h ago)")
        if item.summary:
            lines.append(f"  {item.summary}")
        lines.append(f"  {item.link}\n")

    return "\n".join(lines)


# ------------------------------------------------------------ demo

def build_demo_feeds(now: datetime) -> list[tuple[str, str]]:
    def rss(source_name: str, items: list[tuple[str, str, str, str]]) -> str:
        entries = "".join(
            f"<item><title>{title}</title><link>{link}</link>"
            f"<pubDate>{pub}</pubDate><description>{desc}</description></item>"
            for title, link, pub, desc in items
        )
        return f"<rss><channel><title>{source_name}</title>{entries}</channel></rss>"

    def rfc822(dt: datetime) -> str:
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    def atom(source_name: str, items: list[tuple[str, str, str, str]]) -> str:
        entries = "".join(
            f'<entry><title>{title}</title><link href="{link}"/>'
            f"<updated>{pub}</updated><summary>{desc}</summary></entry>"
            for title, link, pub, desc in items
        )
        return f'<feed xmlns="http://www.w3.org/2005/Atom"><title>{source_name}</title>{entries}</feed>'

    reuters = rss("Reuters Tech", [
        ("Major cloud outage disrupts services worldwide", "https://reuters.com/a1", rfc822(now - timedelta(hours=2)), "A widespread outage affected several major cloud providers today."),
        ("Startup raises $50M Series B", "https://reuters.com/a2", rfc822(now - timedelta(hours=8)), "The company plans to use the funding to expand into new markets."),
        ("Old story from last week", "https://reuters.com/a3", rfc822(now - timedelta(days=8)), "This should be filtered out by the time window."),
    ])
    ap_wire = rss("AP Business", [
        # same story as reuters a1, worded slightly differently, published a bit later — should dedupe
        ("Major Cloud Outage Disrupts Services Worldwide - AP", "https://apnews.com/b1", rfc822(now - timedelta(hours=1, minutes=30)), "Multiple cloud platforms went down."),
        ("Local council approves new bike lanes", "https://apnews.com/b2", rfc822(now - timedelta(hours=5)), "The vote passed 6 to 1."),
    ])
    blog = atom("Tech Blog", [
        ("Why we migrated off Kubernetes", "https://blog.example.com/c1", (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "A postmortem on our infrastructure decisions."),
        # same story again, a third source, published latest of the three — should NOT win the dedupe
        ("Cloud Outage: What Happened and Why", "https://blog.example.com/c2", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "Our take on today's outage."),
    ])
    return [("Reuters Tech", reuters), ("AP Business", ap_wire), ("Tech Blog", blog)]


def demo() -> int:
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    feeds = build_demo_feeds(now)

    print(f"aggregating {len(feeds)} feeds\n")
    all_items = []
    for source, xml_text in feeds:
        items = parse_feed(xml_text, source)
        print(f"  {source}: {len(items)} items")
        all_items.extend(items)

    since = now - timedelta(hours=24)
    digest_items = dedupe_and_filter(all_items, since)

    print(f"\n{len(all_items)} total items -> {len(digest_items)} after 24h filter + dedup\n")
    print(format_digest(digest_items, since, now=now))

    print("\nnote: Reuters and AP ran near-identical headlines for the same outage")
    print("('Major cloud outage disrupts...' vs 'Major Cloud Outage Disrupts... - AP') —")
    print("normalized-title fingerprinting collapsed those two into ONE entry, keeping")
    print("Reuters' copy since it published first, even though AP's copy appeared later.")
    print("The blog's story about the SAME event ('Cloud Outage: What Happened and Why')")
    print("survives as a separate entry — different wording entirely, so the fingerprint")
    print("doesn't match. That's the real boundary of title-based dedup: it catches a wire")
    print("story re-run under a near-identical headline, not a paraphrased human write-up.")
    return 0


def fetch_feed(url: str, timeout: float = 10.0) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "rss-digest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.urls:
        return demo()

    all_items = []
    for url in args.urls:
        try:
            xml_text = fetch_feed(url)
            all_items.extend(parse_feed(xml_text, url))
        except Exception as exc:
            print(f"  warning: could not fetch {url}: {exc}")

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    digest_items = dedupe_and_filter(all_items, since)
    print(format_digest(digest_items, since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
