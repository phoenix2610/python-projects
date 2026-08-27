#!/usr/bin/env python3
"""Snapshot DNS zones, diff against expected records, and alert on drift.

    dns_audit.py check --zone zone.json --domain example.com
    dns_audit.py --demo

Speaks raw DNS over UDP with the stdlib's `socket` (no dnspython): hand-builds
a query packet, parses the response's resource records, and follows the
compression-pointer scheme RFC 1035 uses for names, since that's the part
every from-scratch DNS parser gets wrong first. Comparing against an expected
zone file catches the failure mode that actually bites teams — a DNS record
someone changed by hand in a registrar UI, out of band from the infrastructure
code that's supposed to own it, silently drifting from what's declared.
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import struct
from dataclasses import dataclass, field

TYPE_A, TYPE_NS, TYPE_CNAME, TYPE_MX, TYPE_TXT, TYPE_AAAA = 1, 2, 5, 15, 16, 28
TYPE_NAMES = {TYPE_A: "A", TYPE_NS: "NS", TYPE_CNAME: "CNAME", TYPE_MX: "MX", TYPE_TXT: "TXT", TYPE_AAAA: "AAAA"}
CLASS_IN = 1


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def build_query(domain: str, qtype: int) -> tuple[bytes, int]:
    query_id = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)  # standard query, recursion desired
    question = encode_name(domain) + struct.pack(">HH", qtype, CLASS_IN)
    return header + question, query_id


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name, following compression pointers (RFC 1035 4.1.4)."""
    labels = []
    original_offset = offset
    jumped = False
    seen_offsets = set()

    while True:
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:  # compression pointer: top two bits set
            if offset in seen_offsets:
                raise ValueError("compression pointer loop detected")
            seen_offsets.add(offset)
            pointer = struct.unpack(">H", data[offset : offset + 2])[0] & 0x3FFF
            if not jumped:
                original_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", errors="replace"))
        offset += length

    final_offset = original_offset if jumped else offset
    return ".".join(labels), final_offset


@dataclass
class ResourceRecord:
    name: str
    rtype: int
    ttl: int
    value: str

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.rtype, str(self.rtype))


def parse_rdata(data: bytes, rtype: int, rdata_offset: int, rdlength: int) -> str:
    rdata = data[rdata_offset : rdata_offset + rdlength]
    if rtype == TYPE_A and len(rdata) == 4:
        return ".".join(str(b) for b in rdata)
    if rtype == TYPE_AAAA and len(rdata) == 16:
        groups = struct.unpack(">8H", rdata)
        return ":".join(f"{g:x}" for g in groups)
    if rtype in (TYPE_NS, TYPE_CNAME):
        name, _ = decode_name(data, rdata_offset)
        return name
    if rtype == TYPE_MX:
        preference = struct.unpack(">H", rdata[:2])[0]
        exchange, _ = decode_name(data, rdata_offset + 2)
        return f"{preference} {exchange}"
    if rtype == TYPE_TXT:
        chunks = []
        pos = 0
        while pos < len(rdata):
            length = rdata[pos]
            chunks.append(rdata[pos + 1 : pos + 1 + length].decode("utf-8", errors="replace"))
            pos += 1 + length
        return "".join(chunks)
    return rdata.hex()


def parse_response(data: bytes, expected_id: int) -> list[ResourceRecord]:
    resp_id, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])[0], struct.unpack(">H", data[2:4])[0], struct.unpack(">H", data[4:6])[0], struct.unpack(">H", data[6:8])[0]
    if resp_id != expected_id:
        raise ValueError(f"response ID mismatch: expected {expected_id}, got {resp_id}")
    rcode = flags & 0x000F
    if rcode != 0:
        raise ValueError(f"DNS server returned error code {rcode}")

    offset = 12
    for _ in range(qdcount):
        _, offset = decode_name(data, offset)
        offset += 4  # qtype + qclass

    records = []
    for _ in range(ancount):
        _, offset = decode_name(data, offset)
        rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
        offset += 10
        value = parse_rdata(data, rtype, offset, rdlength)
        offset += rdlength
        records.append(ResourceRecord("", rtype, ttl, value))
    return records


def query_dns(domain: str, qtype: int, server: str = "8.8.8.8", timeout: float = 5.0) -> list[ResourceRecord]:
    query, query_id = build_query(domain, qtype)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        data, _ = sock.recvfrom(4096)
    return parse_response(data, query_id)


# ------------------------------------------------------------ drift comparison

@dataclass
class ExpectedRecord:
    type: str
    value: str


@dataclass
class DriftFinding:
    domain: str
    rtype: str
    expected: list[str]
    actual: list[str]
    kind: str  # "missing" | "unexpected" | "value_changed"


def compare_zone(expected: dict[str, list[ExpectedRecord]], actual: dict[str, list[ResourceRecord]]) -> list[DriftFinding]:
    findings = []
    all_domains = set(expected) | set(actual)

    for domain in sorted(all_domains):
        expected_by_type: dict[str, set[str]] = {}
        for rec in expected.get(domain, []):
            expected_by_type.setdefault(rec.type, set()).add(rec.value)

        actual_by_type: dict[str, set[str]] = {}
        for rec in actual.get(domain, []):
            actual_by_type.setdefault(rec.type_name, set()).add(rec.value)

        all_types = set(expected_by_type) | set(actual_by_type)
        for rtype in sorted(all_types):
            exp_values = expected_by_type.get(rtype, set())
            act_values = actual_by_type.get(rtype, set())
            if exp_values == act_values:
                continue
            if not act_values:
                findings.append(DriftFinding(domain, rtype, sorted(exp_values), sorted(act_values), "missing"))
            elif not exp_values:
                findings.append(DriftFinding(domain, rtype, sorted(exp_values), sorted(act_values), "unexpected"))
            else:
                findings.append(DriftFinding(domain, rtype, sorted(exp_values), sorted(act_values), "value_changed"))

    return findings


def format_findings(findings: list[DriftFinding]) -> str:
    if not findings:
        return "No drift — every record matches the expected zone."
    lines = [f"{len(findings)} drifted record(s):\n"]
    for f in findings:
        if f.kind == "missing":
            lines.append(f"  MISSING   {f.domain} {f.rtype}: expected {f.expected}, found nothing")
        elif f.kind == "unexpected":
            lines.append(f"  UNEXPECTED {f.domain} {f.rtype}: not in the zone file, but resolving to {f.actual}")
        else:
            lines.append(f"  CHANGED   {f.domain} {f.rtype}: expected {f.expected}, resolves to {f.actual}")
    return "\n".join(lines)


def load_expected_zone(path: str) -> dict[str, list[ExpectedRecord]]:
    with open(path) as fh:
        raw = json.load(fh)
    return {domain: [ExpectedRecord(**r) for r in records] for domain, records in raw.items()}


# ------------------------------------------------------------ demo

def demo() -> int:
    print("1. round-trip: build a query packet, parse a hand-crafted response\n")

    def build_fake_response(query: bytes, query_id: int, answers: list[tuple[str, int, int, bytes]]) -> bytes:
        header = struct.pack(">HHHHHH", query_id, 0x8180, 1, len(answers), 0, 0)
        question = query[12:]
        body = b""
        for name, rtype, ttl, rdata in answers:
            body += b"\xc0\x0c"  # pointer back to the question's name (compression!)
            body += struct.pack(">HHIH", rtype, CLASS_IN, ttl, len(rdata)) + rdata
        return header + question + body

    query, qid = build_query("example.com", TYPE_A)
    fake_response = build_fake_response(query, qid, [("example.com", TYPE_A, 300, bytes([93, 184, 216, 34]))])
    records = parse_response(fake_response, qid)
    print(f"  query for example.com A record -> {records[0].value} (TTL {records[0].ttl}s)")
    print(f"  compression pointer (0xC0 0x0C) correctly resolved back to the question name\n")

    query_mx, qid_mx = build_query("example.com", TYPE_MX)
    mx_rdata = struct.pack(">H", 10) + encode_name("mail.example.com")
    fake_mx = build_fake_response(query_mx, qid_mx, [("example.com", TYPE_MX, 3600, mx_rdata)])
    mx_records = parse_response(fake_mx, qid_mx)
    print(f"  query for example.com MX record -> {mx_records[0].value}")

    query_txt, qid_txt = build_query("example.com", TYPE_TXT)
    txt_value = "v=spf1 include:_spf.example.com ~all"
    txt_rdata = bytes([len(txt_value)]) + txt_value.encode()
    fake_txt = build_fake_response(query_txt, qid_txt, [("example.com", TYPE_TXT, 3600, txt_rdata)])
    txt_records = parse_response(fake_txt, qid_txt)
    print(f"  query for example.com TXT record -> {txt_records[0].value!r}\n")

    print("2. comparing a live snapshot against the expected zone file\n")

    expected = {
        "example.com": [ExpectedRecord("A", "93.184.216.34"), ExpectedRecord("MX", "10 mail.example.com")],
        "www.example.com": [ExpectedRecord("CNAME", "example.com")],
        "api.example.com": [ExpectedRecord("A", "93.184.216.50")],
        "old.example.com": [ExpectedRecord("A", "93.184.216.99")],  # should have been removed, still expected
    }

    # simulate what a live snapshot found — one record changed out of band, one
    # record present that isn't in the zone file, one missing entirely
    live_snapshot = {
        "example.com": [ResourceRecord("example.com", TYPE_A, 300, "93.184.216.34"), ResourceRecord("example.com", TYPE_MX, 3600, "10 mail.example.com")],
        "www.example.com": [ResourceRecord("www.example.com", TYPE_CNAME, 300, "example.com")],
        "api.example.com": [ResourceRecord("api.example.com", TYPE_A, 300, "203.0.113.7")],  # CHANGED — should be .50
        "staging.example.com": [ResourceRecord("staging.example.com", TYPE_A, 300, "198.51.100.5")],  # UNEXPECTED — not in zone file
        # old.example.com is entirely absent from the live snapshot -> MISSING
    }

    findings = compare_zone(expected, live_snapshot)
    print(format_findings(findings))

    print(f"\n\nnote: api.example.com's A record silently changed from 93.184.216.50 to")
    print(f"203.0.113.7 — someone (or something) repointed it outside the infra code that")
    print(f"declares the zone file, and that's exactly the kind of change this tool exists")
    print(f"to catch before it becomes a 2am incident. old.example.com is flagged MISSING")
    print(f"(it's still in the zone file but no longer resolves), and staging.example.com")
    print(f"resolves to something nobody declared — both worth a human's attention.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="subcommand")
    check_p = sub.add_parser("check")
    check_p.add_argument("--zone", required=True, help="JSON: {domain: [{type, value}, ...]}")
    check_p.add_argument("--server", default="8.8.8.8")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.subcommand:
        return demo()

    expected = load_expected_zone(args.zone)
    live: dict[str, list[ResourceRecord]] = {}
    type_lookup = {"A": TYPE_A, "NS": TYPE_NS, "CNAME": TYPE_CNAME, "MX": TYPE_MX, "TXT": TYPE_TXT, "AAAA": TYPE_AAAA}

    for domain, records in expected.items():
        wanted_types = {type_lookup[r.type] for r in records if r.type in type_lookup}
        found = []
        for qtype in wanted_types:
            try:
                found.extend(query_dns(domain, qtype, args.server))
            except (socket.timeout, OSError, ValueError) as exc:
                print(f"  warning: query failed for {domain} {TYPE_NAMES.get(qtype, qtype)}: {exc}")
        live[domain] = found

    findings = compare_zone(expected, live)
    print(format_findings(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
