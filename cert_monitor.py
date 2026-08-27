#!/usr/bin/env python3
"""Check TLS certificate expiry across domains, warning at 30/14/7 days out.

    cert_monitor.py example.com api.example.com --port 443
    cert_monitor.py --demo

Opens a real TLS connection (stdlib `ssl` + `socket`, no external cert-checking
library) and reads the peer certificate's notAfter field directly off the
handshake — the only way to see what a browser would actually see, rather than
trusting a cached value. Three severities instead of one binary "expiring soon"
flag: 30 days out is a heads-up, 14 is a ticket, 7 is paging someone, because
those need different responses and a flat threshold either pages too early or
warns too late.
"""
from __future__ import annotations

import argparse
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class CertCheck:
    host: str
    port: int
    ok: bool
    expires_at: datetime | None
    days_remaining: float | None
    issuer: str | None
    subject: str | None
    error: str | None


def fetch_certificate(host: str, port: int = 443, timeout: float = 5.0) -> CertCheck:
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                cert = tls_sock.getpeercert()
    except (socket.gaierror, socket.timeout, ConnectionRefusedError, OSError) as exc:
        return CertCheck(host, port, False, None, None, None, None, f"connection failed: {exc}")
    except ssl.SSLCertVerificationError as exc:
        return CertCheck(host, port, False, None, None, None, None, f"certificate invalid: {exc.verify_message}")
    except ssl.SSLError as exc:
        return CertCheck(host, port, False, None, None, None, None, f"TLS error: {exc}")

    return parse_cert_dict(host, port, cert)


def parse_cert_dict(host: str, port: int, cert: dict) -> CertCheck:
    not_after = cert.get("notAfter")
    if not not_after:
        return CertCheck(host, port, False, None, None, None, None, "certificate has no expiry field")

    expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds() / 86400

    issuer = dict(x[0] for x in cert.get("issuer", [])).get("organizationName") or dict(x[0] for x in cert.get("issuer", [])).get("commonName")
    subject = dict(x[0] for x in cert.get("subject", [])).get("commonName")

    return CertCheck(host, port, True, expires_at, remaining, issuer, subject, None)


def severity_for(days_remaining: float) -> str:
    if days_remaining < 0:
        return "EXPIRED"
    if days_remaining <= 7:
        return "critical"
    if days_remaining <= 14:
        return "warning"
    if days_remaining <= 30:
        return "notice"
    return "ok"


SEVERITY_ORDER = {"EXPIRED": 0, "critical": 1, "warning": 2, "notice": 3, "ok": 4, "error": 5}


def format_report(checks: list[CertCheck]) -> str:
    ranked = sorted(checks, key=lambda c: SEVERITY_ORDER["error"] if not c.ok else SEVERITY_ORDER[severity_for(c.days_remaining)])
    lines = []
    for check in ranked:
        if not check.ok:
            lines.append(f"  error     {check.host:<28} {check.error}")
            continue
        sev = severity_for(check.days_remaining)
        label = {"EXPIRED": "EXPIRED!", "critical": "critical", "warning": "warning ", "notice": "notice  ", "ok": "ok      "}[sev]
        lines.append(
            f"  {label}  {check.host:<28} {check.days_remaining:>6.1f}d remaining  "
            f"expires {check.expires_at.strftime('%Y-%m-%d')}  issuer={check.issuer or '?'}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------ demo

def demo() -> int:
    print("checking certificate expiry across a fleet of domains")
    print("(using scripted certificate data — no real network calls in the demo)\n")

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    fake_certs = [
        ("shop.example.com", {"notAfter": "Sep 25 12:00:00 2026 GMT", "issuer": [[("organizationName", "Let's Encrypt")]], "subject": [[("commonName", "shop.example.com")]]}),
        ("api.example.com", {"notAfter": "Sep 03 12:00:00 2026 GMT", "issuer": [[("organizationName", "DigiCert Inc")]], "subject": [[("commonName", "api.example.com")]]}),
        ("legacy.example.com", {"notAfter": "Aug 30 12:00:00 2026 GMT", "issuer": [[("organizationName", "Let's Encrypt")]], "subject": [[("commonName", "legacy.example.com")]]}),
        ("staging.example.com", {"notAfter": "Aug 20 12:00:00 2026 GMT", "issuer": [[("organizationName", "Let's Encrypt")]], "subject": [[("commonName", "staging.example.com")]]}),  # already expired
        ("cdn.example.com", {"notAfter": "Dec 01 12:00:00 2026 GMT", "issuer": [[("organizationName", "Amazon")]], "subject": [[("commonName", "cdn.example.com")]]}),
    ]

    checks = []
    for host, cert in fake_certs:
        check = parse_cert_dict(host, 443, cert)
        # override "now" for a deterministic demo, since parse_cert_dict uses real now()
        check.days_remaining = (check.expires_at - now).total_seconds() / 86400
        checks.append(check)

    checks.append(CertCheck("unreachable.example.com", 443, False, None, None, None, None, "connection failed: [Errno -2] Name or service not known"))

    print(format_report(checks))

    critical_or_worse = [c for c in checks if c.ok and severity_for(c.days_remaining) in ("EXPIRED", "critical")]
    print(f"\n{len(critical_or_worse)} certificate(s) need attention within a week:")
    for c in critical_or_worse:
        print(f"  {c.host}: {c.days_remaining:.1f} days")

    print(f"\nnote: staging.example.com already expired (negative days remaining) and legacy.example.com")
    print(f"is 3.5 days out — both sort to the top as 'critical', ahead of api.example.com at 7.5 days,")
    print(f"which lands one tier down as 'warning' since it's just past the 7-day critical cutoff.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hosts", nargs="*")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.hosts:
        return demo()

    checks = [fetch_certificate(host, args.port, args.timeout) for host in args.hosts]
    print(format_report(checks))
    critical = [c for c in checks if c.ok and severity_for(c.days_remaining) in ("EXPIRED", "critical")]
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
