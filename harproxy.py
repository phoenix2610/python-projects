#!/usr/bin/env python3
"""A recording proxy: capture real traffic to a HAR file, then replay it offline.

    harproxy.py record --port 8080 --out session.har
        http_proxy=http://localhost:8080 curl http://example.com/api/users

    harproxy.py replay session.har --port 8081     # serves the captured responses
    harproxy.py ls session.har

Record mode is a forward proxy: it reads the absolute-form request line that
proxied clients send, fetches upstream, and writes both halves to HAR 1.2.
Replay matches on method + path (+ query, unless you relax it) so a test suite
can run against yesterday's API with the network unplugged.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "accept-encoding"}


def har_skeleton() -> dict:
    return {"log": {"version": "1.2", "creator": {"name": "harproxy", "version": "1.0"}, "entries": []}}


def headers_list(items) -> list[dict]:
    return [{"name": k, "value": v} for k, v in items]


class Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    har: dict = har_skeleton()
    out_path = "session.har"

    def log_message(self, fmt, *args):  # quieter than the default access log
        pass

    def _proxy(self, method: str) -> None:
        target = self.path if self.path.startswith("http") else f"http://{self.headers.get('Host','')}{self.path}"
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        req = urllib.request.Request(target, data=body, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload, status, reason = resp.read(), resp.status, resp.reason
                resp_headers = list(resp.headers.items())
        except urllib.error.HTTPError as err:      # a 4xx/5xx is a real response, record it
            payload, status, reason = err.read(), err.code, err.reason
            resp_headers = list(err.headers.items())
        except urllib.error.URLError as err:
            self.send_error(502, f"upstream: {err.reason}")
            return
        elapsed = (time.perf_counter() - started) * 1000

        parsed = urlparse(target)
        Recorder.har["log"]["entries"].append({
            "startedDateTime": datetime.now(timezone.utc).isoformat(),
            "time": round(elapsed, 2),
            "request": {"method": method, "url": target, "httpVersion": "HTTP/1.1",
                        "headers": headers_list(headers.items()), "queryString": [], "cookies": [],
                        "headersSize": -1, "bodySize": length,
                        "postData": {"mimeType": headers.get("Content-Type", ""),
                                     "text": (body or b"").decode("utf-8", "replace")} if body else None},
            "response": {"status": status, "statusText": reason, "httpVersion": "HTTP/1.1",
                         "headers": headers_list(resp_headers), "cookies": [], "headersSize": -1,
                         "bodySize": len(payload), "redirectURL": "",
                         "content": {"size": len(payload),
                                     "mimeType": dict((k.lower(), v) for k, v in resp_headers).get("content-type", ""),
                                     "text": payload.decode("utf-8", "replace")}},
            "cache": {}, "timings": {"send": 0, "wait": round(elapsed, 2), "receive": 0},
            "_path": parsed.path or "/", "_query": parsed.query,
        })
        with open(Recorder.out_path, "w") as fh:
            json.dump(Recorder.har, fh, indent=1)
        print(f"  {status} {method} {target}  {elapsed:.0f}ms  {len(payload)}B", file=sys.stderr)

        self.send_response(status)
        for key, value in resp_headers:
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self): self._proxy("GET")
    def do_POST(self): self._proxy("POST")
    def do_PUT(self): self._proxy("PUT")
    def do_PATCH(self): self._proxy("PATCH")
    def do_DELETE(self): self._proxy("DELETE")


class Replayer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    entries: list[dict] = []
    ignore_query = False

    def log_message(self, fmt, *args):
        pass

    def _match(self, method: str):
        parsed = urlparse(self.path)
        for entry in self.entries:
            url = urlparse(entry["request"]["url"])
            if entry["request"]["method"] != method or url.path != parsed.path:
                continue
            if not self.ignore_query and url.query != parsed.query:
                continue
            return entry
        return None

    def _serve(self, method: str) -> None:
        entry = self._match(method)
        if entry is None:
            self.send_error(404, f"no recording for {method} {self.path}")
            return
        resp = entry["response"]
        payload = resp["content"].get("text", "").encode()
        self.send_response(resp["status"], resp.get("statusText", ""))
        for header in resp["headers"]:
            if header["name"].lower() not in HOP_BY_HOP | {"content-length"}:
                self.send_header(header["name"], header["value"])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        print(f"  replay {resp['status']} {method} {self.path}", file=sys.stderr)

    def do_GET(self): self._serve("GET")
    def do_POST(self): self._serve("POST")
    def do_PUT(self): self._serve("PUT")
    def do_PATCH(self): self._serve("PATCH")
    def do_DELETE(self): self._serve("DELETE")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record"); r.add_argument("--port", type=int, default=8080); r.add_argument("--out", default="session.har")
    p = sub.add_parser("replay"); p.add_argument("har"); p.add_argument("--port", type=int, default=8081)
    p.add_argument("--ignore-query", action="store_true")
    l = sub.add_parser("ls"); l.add_argument("har")
    args = ap.parse_args()

    if args.cmd == "ls":
        har = json.load(open(args.har))
        entries = har["log"]["entries"]
        for e in entries:
            print(f"  {e['response']['status']} {e['request']['method']:<6} {e['request']['url']}  "
                  f"{e['time']:.0f}ms  {e['response']['bodySize']}B")
        print(f"\n{len(entries)} entries, "
              f"{sum(e['response']['bodySize'] for e in entries) / 1024:.1f}KB of bodies")
        return 0

    if args.cmd == "record":
        Recorder.out_path = args.out
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Recorder)
        print(f"recording to {args.out} — point a client at http://127.0.0.1:{args.port}\n"
              f"  http_proxy=http://127.0.0.1:{args.port} curl http://example.com/", file=sys.stderr)
    else:
        Replayer.entries = json.load(open(args.har))["log"]["entries"]
        Replayer.ignore_query = args.ignore_query
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Replayer)
        print(f"replaying {len(Replayer.entries)} entries on http://127.0.0.1:{args.port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        entries = len(Recorder.har["log"]["entries"])
        print(f"\nstopped ({entries} recorded)" if args.cmd == "record" else "\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
