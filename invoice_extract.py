#!/usr/bin/env python3
"""Pull totals, dates and line items out of PDF invoices into a reconciled spreadsheet.

    invoice_extract.py invoices/*.pdf --out report.csv
    invoice_extract.py --demo

No PyPDF2, no pdfplumber: a PDF is a plain-text object graph (dictionaries,
streams, cross-reference tables) wrapping compressed content streams, and the
content stream itself is a tiny stack-based language whose only operators worth
caring about here are the text-positioning and text-showing ones. This walks the
object table, inflates each content stream with zlib (the FlateDecode filter,
the only one that matters for text-based PDFs), and reconstructs a left-to-right
reading order from the Tm/Td positioning operators — which is what turns "a bag
of text fragments" into lines you can run a total/date/vendor regex over.
"""
from __future__ import annotations

import argparse
import csv
import glob
import re
import sys
import zlib
from dataclasses import dataclass, field

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)endobj", re.S)
STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
DICT_KEY_RE = re.compile(rb"/(\w+)")


def parse_pdf_objects(data: bytes) -> dict[int, bytes]:
    """Map object number -> raw object body (dict header + optional stream), by regex scan.

    A real parser would follow the xref table; scanning for `N G obj ... endobj` directly
    is simpler and works on every PDF that isn't using cross-reference *streams* (a newer,
    binary xref format) or object streams — which covers the vast majority of PDFs written
    by everyday tools like invoicing software, LibreOffice, or a browser's "print to PDF".
    """
    objects: dict[int, bytes] = {}
    for match in OBJ_RE.finditer(data):
        obj_num = int(match.group(1))
        objects[obj_num] = match.group(3)
    return objects


def get_stream_bytes(obj_body: bytes) -> bytes | None:
    match = STREAM_RE.search(obj_body)
    if not match:
        return None
    raw = match.group(1)
    # trim a trailing newline the stream/endstream pair often leaves behind
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n") or raw.endswith(b"\r"):
        raw = raw[:-1]
    if b"/FlateDecode" in obj_body[: match.start()]:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return None
    return raw  # uncompressed content stream


def decode_pdf_string(raw: bytes) -> str:
    """Decode a PDF literal string `(...)`, honouring backslash escapes."""
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1]
            escapes = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:  # octal escape, up to 3 digits
                j = i + 1
                digits = b""
                while j < len(raw) and len(digits) < 3 and 0x30 <= raw[j] <= 0x37:
                    digits += bytes([raw[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    try:
        return out.decode("latin-1")
    except UnicodeDecodeError:
        return out.decode("latin-1", "replace")


@dataclass
class TextFragment:
    x: float
    y: float
    text: str


def tokenize_content_stream(stream: bytes) -> list:
    """Split a content stream into operands and operators — a minimal PostScript-ish lexer."""
    tokens: list = []
    i = 0
    n = len(stream)
    while i < n:
        c = stream[i : i + 1]
        if c in b" \t\r\n":
            i += 1
            continue
        if c == b"%":
            while i < n and stream[i : i + 1] not in b"\r\n":
                i += 1
            continue
        if c == b"(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if stream[j : j + 1] == b"\\":
                    j += 2
                    continue
                if stream[j : j + 1] == b"(":
                    depth += 1
                elif stream[j : j + 1] == b")":
                    depth -= 1
                j += 1
            tokens.append(("string", decode_pdf_string(stream[i + 1 : j - 1])))
            i = j
            continue
        if c == b"[":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if stream[j : j + 1] == b"[":
                    depth += 1
                elif stream[j : j + 1] == b"]":
                    depth -= 1
                j += 1
            tokens.append(("array", stream[i + 1 : j - 1]))
            i = j
            continue
        if c == b"/":
            j = i + 1
            while j < n and stream[j : j + 1] not in b" \t\r\n/[]<>()":
                j += 1
            tokens.append(("name", stream[i + 1 : j].decode("latin-1")))
            i = j
            continue
        if c.isdigit() or c in b"+-.":
            j = i
            while j < n and stream[j : j + 1] in b"0123456789+-.":
                j += 1
            tokens.append(("number", float(stream[i:j])))
            i = j
            continue
        # an operator: a run of letters/asterisks, e.g. Tj, TJ, Tm, BT, ET, Td
        j = i
        while j < n and stream[j : j + 1] not in b" \t\r\n/[](){}<>":
            j += 1
        if j > i:
            tokens.append(("op", stream[i:j].decode("latin-1")))
            i = j
        else:
            i += 1
    return tokens


def extract_fragments(stream: bytes) -> list[TextFragment]:
    """Walk the tokenized content stream, tracking the text matrix, to place each string."""
    tokens = tokenize_content_stream(stream)
    fragments: list[TextFragment] = []
    tx, ty = 0.0, 0.0  # text-line origin, updated by Td/TD/Tm/T*
    stack: list = []

    for kind, value in tokens:
        if kind != "op":
            stack.append(value)
            continue
        op = value
        if op in ("Td", "TD"):
            # padding must come BEFORE the real stack, not after — `(stack + defaults)[-2:]`
            # always yields the trailing defaults themselves when stack is non-empty, which
            # silently discards every real operand and pins tx/ty at (0, 0) forever
            dx, dy = ([0, 0] + stack)[-2:]
            tx += float(dx)
            ty += float(dy)
        elif op == "Tm":
            nums = ([1, 0, 0, 1, 0, 0] + stack)[-6:]
            tx, ty = float(nums[4]), float(nums[5])
        elif op == "T*":
            ty -= 14  # approximate a default leading when none was set
        elif op == "Tj":
            if stack and isinstance(stack[-1], str):
                fragments.append(TextFragment(tx, ty, stack[-1]))
        elif op == "TJ":
            if stack:
                raw_array = stack[-1]
                if isinstance(raw_array, bytes):
                    text = "".join(t for k, t in tokenize_content_stream(raw_array) if k == "string")
                    if text:
                        fragments.append(TextFragment(tx, ty, text))
        elif op == "BT":
            tx, ty = 0.0, 0.0
        stack = []
    return fragments


def fragments_to_lines(fragments: list[TextFragment], y_tolerance: float = 2.0) -> list[str]:
    """Group fragments into reading-order lines by y-coordinate, then sort each line by x."""
    if not fragments:
        return []
    by_y: list[list[TextFragment]] = []
    for frag in sorted(fragments, key=lambda f: -f.y):
        placed = False
        for group in by_y:
            if abs(group[0].y - frag.y) <= y_tolerance:
                group.append(frag)
                placed = True
                break
        if not placed:
            by_y.append([frag])
    lines = []
    for group in by_y:
        group.sort(key=lambda f: f.x)
        lines.append(" ".join(f.text for f in group).strip())
    return [line for line in lines if line]


def extract_text(pdf_bytes: bytes) -> str:
    objects = parse_pdf_objects(pdf_bytes)
    all_lines: list[str] = []
    for obj_num in sorted(objects):
        body = objects[obj_num]
        if b"/Contents" in body or b"stream" not in body:
            continue
        stream = get_stream_bytes(body)
        if stream is None:
            continue
        fragments = extract_fragments(stream)
        all_lines.extend(fragments_to_lines(fragments))
    return "\n".join(all_lines)


# ------------------------------------------------------------ invoice field extraction

MONEY_RE = re.compile(r"\$?\s?([\d,]+\.\d{2})\b")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2},? \d{4})\b")
INVOICE_NO_RE = re.compile(r"(?:Invoice|INV)\s*#?\s*[:\-]?\s*([A-Z0-9\-]+)", re.I)


@dataclass
class InvoiceRecord:
    source: str
    vendor: str | None = None
    invoice_number: str | None = None
    date: str | None = None
    total: str | None = None
    line_items: list[str] = field(default_factory=list)


def parse_invoice_fields(text: str, source: str) -> InvoiceRecord:
    lines = text.split("\n")
    record = InvoiceRecord(source=source)
    record.vendor = lines[0].strip() if lines else None

    for line in lines:
        if inv_match := INVOICE_NO_RE.search(line):
            record.invoice_number = inv_match.group(1)
        if date_match := DATE_RE.search(line):
            if record.date is None:
                record.date = date_match.group(1)
        if re.search(r"\btotal\b", line, re.I) and not re.search(r"subtotal", line, re.I):
            if money_match := MONEY_RE.search(line):
                record.total = money_match.group(1)

    if record.total is None:
        amounts = [m.group(1) for line in lines for m in [MONEY_RE.search(line)] if m]
        if amounts:
            record.total = max(amounts, key=lambda a: float(a.replace(",", "")))

    for line in lines:
        # "subtotal" doesn't match \btotal\b (no word boundary before "total" in
        # "Subtotal"), so it needs its own exclusion or it leaks into line items
        if MONEY_RE.search(line) and not re.search(r"\btotal\b", line, re.I) and not re.search(r"subtotal", line, re.I):
            record.line_items.append(line.strip())

    return record


# ------------------------------------------------------------ demo: build real PDFs

def build_minimal_pdf(lines_with_positions: list[tuple[float, float, str]]) -> bytes:
    """Hand-author a tiny valid PDF: one page, one Flate-compressed content stream."""
    content_ops = ["BT", "/F1 11 Tf"]
    for x, y, text in lines_with_positions:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_ops.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj")
    content_ops.append("ET")
    content = "\n".join(content_ops).encode("latin-1")
    compressed = zlib.compress(content)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(compressed)).encode() + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def make_demo_invoice(vendor: str, invoice_no: str, date: str, items: list[tuple[str, str]], total: str) -> bytes:
    y = 750.0
    lines: list[tuple[float, float, str]] = [(72, y, vendor)]
    y -= 30
    lines.append((72, y, f"Invoice #{invoice_no}"))
    y -= 16
    lines.append((72, y, f"Date: {date}"))
    y -= 30
    for desc, amount in items:
        lines.append((72, y, f"{desc}  ${amount}"))
        y -= 16
    y -= 10
    lines.append((72, y, f"Subtotal  ${float(total) - 5.00:.2f}"))
    y -= 16
    lines.append((72, y, f"Total Due  ${total}"))
    return build_minimal_pdf(lines)


def demo() -> int:
    print("generating three real (hand-authored, Flate-compressed) PDF invoices...\n")
    invoices = [
        make_demo_invoice("Acme Cloud Hosting", "INV-2044", "2026-07-14", [("Compute - 720 hrs", "212.40"), ("Storage - 2TB", "18.00")], "235.40"),
        make_demo_invoice("Bright Office Supply", "B-99213", "07/22/2026", [("Paper - 10 reams", "42.50"), ("Toner cartridge", "89.99")], "137.49"),
        make_demo_invoice("Meridian Legal LLP", "ML-3390", "August 1, 2026", [("Consultation - 3 hrs", "900.00")], "905.00"),
    ]

    records = []
    for i, pdf_bytes in enumerate(invoices, 1):
        print(f"invoice {i}: {len(pdf_bytes)} bytes on disk\n")
        text = extract_text(pdf_bytes)
        print("  extracted text:")
        for line in text.split("\n"):
            print(f"    {line}")
        record = parse_invoice_fields(text, source=f"invoice-{i}.pdf")
        records.append(record)
        print(f"\n  parsed: vendor={record.vendor!r} invoice#={record.invoice_number!r} "
              f"date={record.date!r} total=${record.total}")
        print(f"  line items: {len(record.line_items)}\n")

    print("=" * 60)
    print("reconciled report (report.csv):\n")
    fieldnames = ["source", "vendor", "invoice_number", "date", "total"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for r in records:
        writer.writerow({k: getattr(r, k) for k in fieldnames})

    total_sum = sum(float(r.total.replace(",", "")) for r in records if r.total)
    print(f"\n{len(records)} invoices, ${total_sum:.2f} total")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="*", help="invoice PDF files (globs supported)")
    ap.add_argument("--out", help="write a CSV report here")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.pdfs:
        return demo()

    paths = [p for pattern in args.pdfs for p in glob.glob(pattern)] or args.pdfs
    records = []
    for path in paths:
        with open(path, "rb") as fh:
            text = extract_text(fh.read())
        records.append(parse_invoice_fields(text, source=path))

    out = open(args.out, "w", newline="") if args.out else sys.stdout
    fieldnames = ["source", "vendor", "invoice_number", "date", "total"]
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for r in records:
        writer.writerow({k: getattr(r, k) for k in fieldnames})
    if args.out:
        out.close()
        print(f"{len(records)} invoices -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
