#!/usr/bin/env python3
"""Turn an OpenAPI spec into a running mock API, with latency and failures on tap.

    mockapi.py openapi.json --port 4010
    mockapi.py openapi.json --latency 120 --fail-rate 0.1 --seed 7
    mockapi.py openapi.json --routes          # list what it would serve

Responses come from the spec: an `example` if the operation has one, otherwise a
value synthesised from the schema (respecting type, enum, format, required
fields, and $ref). Path parameters are matched by shape, so /users/{id} answers
/users/42 without you writing a route.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

FORMAT_SAMPLES = {
    "date-time": "2026-08-25T09:00:00Z", "date": "2026-08-25", "email": "ana@example.com",
    "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "uri": "https://example.com/thing",
    "hostname": "api.example.com", "ipv4": "192.0.2.7", "password": "hunter2", "byte": "c3dhZ2dlcg==",
}


def deref(schema: dict, root: dict, depth: int = 0) -> dict:
    if not isinstance(schema, dict) or depth > 12:
        return {}
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return deref(node, root, depth + 1)
    return schema


def synth(schema: dict, root: dict, name: str = "", depth: int = 0):
    """Build a plausible value for a schema — the mock's whole personality lives here."""
    schema = deref(schema, root, depth)
    if depth > 8 or not schema:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    for combiner in ("allOf", "oneOf", "anyOf"):
        if combiner in schema:
            parts = [synth(s, root, name, depth + 1) for s in schema[combiner]]
            if combiner == "allOf":
                merged: dict = {}
                for part in parts:
                    if isinstance(part, dict):
                        merged |= part
                return merged
            return parts[0]
    kind = schema.get("type") or ("object" if "properties" in schema else "string")
    if kind == "object":
        props = schema.get("properties", {})
        return {key: synth(sub, root, key, depth + 1) for key, sub in props.items()}
    if kind == "array":
        item = synth(schema.get("items", {}), root, name, depth + 1)
        return [item, item] if item is not None else []
    if kind == "integer":
        return schema.get("minimum", 1) or 1
    if kind == "number":
        return float(schema.get("minimum", 1.5) or 1.5)
    if kind == "boolean":
        return True
    fmt = schema.get("format", "")
    if fmt in FORMAT_SAMPLES:
        return FORMAT_SAMPLES[fmt]
    if name:
        if re.search(r"(^|_)id$", name, re.I):
            return "abc123"
        if "name" in name.lower():
            return "Example Name"
        if "url" in name.lower():
            return "https://example.com"
    return f"string({name})" if name else "string"


class Route:
    def __init__(self, method: str, template: str, status: int, body, content_type: str, summary: str):
        self.method, self.template, self.status = method, template, status
        self.body, self.content_type, self.summary = body, content_type, summary
        pattern = re.sub(r"\{[^}]+\}", r"([^/]+)", template.rstrip("/") or "/")
        self.regex = re.compile(f"^{pattern}/?$")
        self.params = re.findall(r"\{([^}]+)\}", template)

    def match(self, path: str):
        m = self.regex.match(path)
        return dict(zip(self.params, m.groups())) if m else None


def build_routes(spec: dict) -> list[Route]:
    routes: list[Route] = []
    base = ""
    servers = spec.get("servers") or []
    if servers:
        base = urlparse(servers[0].get("url", "")).path.rstrip("/")
    for template, operations in spec.get("paths", {}).items():
        for method, op in operations.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
                continue
            responses = op.get("responses", {})
            code = next((c for c in sorted(responses, key=str) if str(c).startswith("2")), None) or next(iter(responses), "200")
            response = deref(responses.get(code, {}), spec)
            content = response.get("content", {})
            media = next(iter(content), "application/json")
            payload = content.get(media, {})
            if "example" in payload:
                body = payload["example"]
            elif payload.get("examples"):
                body = next(iter(payload["examples"].values())).get("value")
            else:
                body = synth(payload.get("schema", {}), spec)
            routes.append(Route(method.upper(), base + template, int(str(code)) if str(code).isdigit() else 200,
                                body, media, op.get("summary", "")))
    return routes


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes: list[Route] = []
    latency = 0.0
    jitter = 0.0
    fail_rate = 0.0
    rng = random.Random(0)

    def log_message(self, fmt, *args):
        pass

    def respond(self, status: int, payload, content_type="application/json"):
        data = json.dumps(payload, indent=1).encode() if content_type.startswith("application/json") else str(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def handle_one(self, method: str):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if self.latency:
            time.sleep(max(0.0, self.latency + self.rng.uniform(-self.jitter, self.jitter)) / 1000)
        for route in self.routes:
            params = route.match(path) if route.method == method else None
            if params is None:
                continue
            if self.fail_rate and self.rng.random() < self.fail_rate:
                print(f"  500 {method} {path}   (injected failure)", file=sys.stderr)
                self.respond(500, {"error": "injected failure", "hint": "lower --fail-rate to stop this"})
                return
            body = route.body
            if isinstance(body, dict):
                body = {**body, **{k: v for k, v in params.items() if k in body}}
            print(f"  {route.status} {method} {path}" + (f"  {params}" if params else ""), file=sys.stderr)
            self.respond(route.status, body, route.content_type)
            return
        known = sorted({f"{r.method} {r.template}" for r in self.routes})[:8]
        self.respond(404, {"error": f"no mock for {method} {path}", "try": known})

    def do_GET(self): self.handle_one("GET")
    def do_POST(self): self.handle_one("POST")
    def do_PUT(self): self.handle_one("PUT")
    def do_PATCH(self): self.handle_one("PATCH")
    def do_DELETE(self): self.handle_one("DELETE")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="OpenAPI 3 document (JSON)")
    ap.add_argument("--port", type=int, default=4010)
    ap.add_argument("--latency", type=float, default=0, help="milliseconds to wait before responding")
    ap.add_argument("--jitter", type=float, default=0, help="+/- milliseconds of random variation")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="fraction of requests answered with a 500")
    ap.add_argument("--seed", type=int, default=0, help="make injected failures reproducible")
    ap.add_argument("--routes", action="store_true", help="print the route table and exit")
    args = ap.parse_args()

    spec = json.load(open(args.spec))
    routes = build_routes(spec)
    if not routes:
        print("no operations found in the spec", file=sys.stderr)
        return 1
    if args.routes:
        width = max(len(r.template) for r in routes)
        for r in routes:
            print(f"  {r.method:<6} {r.template.ljust(width)}  -> {r.status}  {r.summary}")
        print(f"\n{len(routes)} routes")
        return 0

    Handler.routes = routes
    Handler.latency, Handler.jitter, Handler.fail_rate = args.latency, args.jitter, args.fail_rate
    Handler.rng = random.Random(args.seed)
    title = spec.get("info", {}).get("title", "mock")
    print(f"{title}: {len(routes)} routes on http://127.0.0.1:{args.port}"
          + (f"  (latency {args.latency}ms, fail rate {args.fail_rate:.0%})" if args.latency or args.fail_rate else ""),
          file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
