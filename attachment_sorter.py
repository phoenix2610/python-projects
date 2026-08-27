#!/usr/bin/env python3
"""Watch a maildir, file attachments by sender/rule, and log what happened.

    attachment_sorter.py ~/Mail/INBOX --rules rules.json --into ~/Downloads/sorted
    attachment_sorter.py --demo

Parses raw RFC 822 messages with the stdlib `email` package (no IMAP client
needed if mail is already a maildir), decodes each attachment regardless of its
Content-Transfer-Encoding (base64, quoted-printable, or none), and files it
under a folder chosen by matching rules against sender/subject — first match
wins, falling back to a sender-domain folder so nothing is ever silently
dropped. Filenames are sanitised and de-duplicated so two attachments named
"invoice.pdf" from different emails don't clobber each other.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass
class Rule:
    match_from: str | None = None
    match_subject: str | None = None
    match_extension: str | None = None
    folder: str = "misc"

    def matches(self, sender: str, subject: str, filename: str) -> bool:
        if self.match_from and not re.search(self.match_from, sender, re.I):
            return False
        if self.match_subject and not re.search(self.match_subject, subject, re.I):
            return False
        if self.match_extension and not filename.lower().endswith(self.match_extension.lower()):
            return False
        return bool(self.match_from or self.match_subject or self.match_extension)


@dataclass
class FiledAttachment:
    source_subject: str
    sender: str
    original_name: str
    saved_path: str
    rule_folder: str
    size: int


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[/\\\x00-\x1f]", "_", name)
    name = name.strip().strip(".")
    return name or "attachment"


def sender_domain(sender: str) -> str:
    match = re.search(r"@([\w.-]+)", sender)
    return match.group(1) if match else "unknown"


def choose_folder(rules: list[Rule], sender: str, subject: str, filename: str) -> str:
    for rule in rules:
        if rule.matches(sender, subject, filename):
            return rule.folder
    return f"by-domain/{sender_domain(sender)}"


def unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}-{n}{ext}")
        n += 1
    return candidate


def extract_attachments(raw_message: bytes) -> list[tuple[str, bytes, str]]:
    """Return [(filename, content_bytes, content_type)] for every real attachment.

    email.message_from_bytes with the default (compat32) policy leaves payloads
    base64-encoded; the modern EmailMessage policy decodes CTE automatically via
    get_content()/get_payload(decode=True) either way — but only for parts marked
    as an actual attachment, not inline text/html bodies.
    """
    msg = email.message_from_bytes(raw_message, policy=email.policy.default)
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append((filename or "unnamed", payload, part.get_content_type()))
    return attachments


def sort_message(raw_message: bytes, rules: list[Rule], into_dir: str, dry_run: bool = False) -> list[FiledAttachment]:
    msg = email.message_from_bytes(raw_message, policy=email.policy.default)
    sender = str(msg.get("From", "unknown@unknown"))
    subject = str(msg.get("Subject", "(no subject)"))
    filed: list[FiledAttachment] = []

    for filename, content, _content_type in extract_attachments(raw_message):
        clean_name = sanitize_filename(filename)
        folder = choose_folder(rules, sender, subject, clean_name)
        target_dir = os.path.join(into_dir, folder)
        if not dry_run:
            os.makedirs(target_dir, exist_ok=True)
        target_path = unique_path(target_dir, clean_name) if not dry_run and os.path.isdir(target_dir) else os.path.join(target_dir, clean_name)
        if not dry_run:
            with open(target_path, "wb") as fh:
                fh.write(content)
        filed.append(FiledAttachment(subject, sender, filename, target_path, folder, len(content)))
    return filed


def load_rules(path: str) -> list[Rule]:
    with open(path) as fh:
        raw = json.load(fh)
    return [Rule(**r) for r in raw]


def process_maildir(maildir: str, rules: list[Rule], into_dir: str, dry_run: bool = False) -> list[FiledAttachment]:
    all_filed: list[FiledAttachment] = []
    candidates = [maildir] if os.path.isfile(maildir) else []
    if os.path.isdir(maildir):
        for sub in ("new", "cur", "."):
            sub_path = os.path.join(maildir, sub)
            if os.path.isdir(sub_path):
                candidates.extend(os.path.join(sub_path, f) for f in sorted(os.listdir(sub_path)) if not f.startswith("."))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        all_filed.extend(sort_message(raw, rules, into_dir, dry_run))
    return all_filed


# ------------------------------------------------------------ demo

def build_message(sender: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, content, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(msg)


def build_demo_maildir(root: str) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    new_dir = os.path.join(root, "new")
    os.makedirs(new_dir)

    messages = [
        build_message(
            "billing@acmecloud.com", "Your invoice for July",
            "Please find your invoice attached.",
            [("invoice-july.pdf", b"%PDF-1.4 fake invoice bytes", "application/pdf")],
        ),
        build_message(
            "billing@acmecloud.com", "Your invoice for August",
            "Please find your invoice attached.",
            [("invoice.pdf", b"%PDF-1.4 August invoice bytes", "application/pdf")],
        ),
        build_message(
            "billing@acmecloud.com", "Your invoice for September",
            "Please find your invoice attached.",
            [("invoice.pdf", b"%PDF-1.4 September invoice bytes, deliberately same filename as August", "application/pdf")],
        ),
        build_message(
            "ana@design-studio.io", "Logo revisions",
            "Here's the updated logo pack.",
            [("logo-final.png", b"\x89PNG fake bytes", "image/png"), ("logo-final.svg", b"<svg fake/>", "image/svg+xml")],
        ),
        build_message(
            "noreply@github.com", "[repo] Weekly digest",
            "Here is your weekly digest.",
            [],  # no attachment: should not appear in results at all
        ),
        build_message(
            "receipts@cloudstore.com", "Receipt #4471",
            "Thanks for your purchase.",
            [("receipt.pdf", b"%PDF-1.4 receipt bytes", "application/pdf")],
        ),
        build_message(
            "unknown-sender@random-domain.net", "hey check this out",
            "no rule will match this one",
            [("photo.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")],
        ),
    ]
    for i, msg in enumerate(messages):
        with open(os.path.join(new_dir, f"{1700000000 + i}.msg"), "wb") as fh:
            fh.write(msg)


def demo() -> int:
    maildir = "/tmp/attachment-sorter-demo/maildir"
    into_dir = "/tmp/attachment-sorter-demo/sorted"
    build_demo_maildir(maildir)

    rules = [
        Rule(match_from=r"@acmecloud\.com$", folder="invoices/acme"),
        Rule(match_from=r"@cloudstore\.com$", match_subject=r"receipt", folder="receipts"),
        Rule(match_extension=".svg", folder="design-assets/vector"),
        Rule(match_extension=".png", folder="design-assets/raster"),
    ]

    print("rules (first match wins):")
    for r in rules:
        parts = [f"from~/{r.match_from}/" if r.match_from else None,
                 f"subject~/{r.match_subject}/" if r.match_subject else None,
                 f"ext={r.match_extension}" if r.match_extension else None]
        print(f"  -> {r.folder}   [{', '.join(p for p in parts if p)}]")
    print("  (no match) -> by-domain/<sender's domain>\n")

    filed = process_maildir(maildir, rules, into_dir)

    print(f"{len(filed)} attachments filed from 6 messages (one message had none, correctly skipped):\n")
    for f in filed:
        rel = os.path.relpath(f.saved_path, into_dir)
        print(f"  {f.sender:<32} {f.original_name:<20} -> {rel}  ({f.size}B)")

    print("\nfolder layout:")
    for dirpath, _, filenames in sorted(os.walk(into_dir)):
        for name in sorted(filenames):
            print(f"  {os.path.relpath(os.path.join(dirpath, name), into_dir)}")

    invoice_files = sorted(f for f in os.listdir(os.path.join(into_dir, "invoices/acme")))
    print(f"\ninvoices/acme contains {len(invoice_files)} files even though two messages both attached")
    print(f'"invoice.pdf": {invoice_files} — the second one was renamed instead of overwriting the first.')
    print(f"the random-domain sender with no matching rule fell back to by-domain/random-domain.net")
    print(f"instead of being dropped.")

    import shutil
    shutil.rmtree("/tmp/attachment-sorter-demo", ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("maildir", nargs="?", help="a maildir (new/cur) or a single .eml file")
    ap.add_argument("--rules", help="JSON file: a list of {match_from, match_subject, match_extension, folder}")
    ap.add_argument("--into", default="./sorted", help="destination root for filed attachments")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.maildir:
        return demo()

    rules = load_rules(args.rules) if args.rules else []
    filed = process_maildir(args.maildir, rules, args.into, args.dry_run)
    for f in filed:
        print(f"  {f.sender} -> {f.saved_path}" + (" (dry run)" if args.dry_run else ""))
    print(f"\n{len(filed)} attachments {'would be ' if args.dry_run else ''}filed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
