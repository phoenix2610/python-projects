#!/usr/bin/env python3
"""Speak Redis: an incremental RESP2/RESP3 parser, a client, and a tiny server to test it.

    resp.py --demo                       # server + client, no Redis required
    resp.py client --host localhost --port 6379 PING
    resp.py serve --port 6380            # a mini Redis: GET/SET/DEL/INCR/EXPIRE/KEYS

The parser is incremental: feed it whatever bytes arrived and it yields complete
values, keeping the partial tail for next time. That is the part people get wrong
with `recv()` — a reply can arrive split across any byte boundary, including in
the middle of a length prefix.
"""
from __future__ import annotations

import argparse
import fnmatch
import socket
import socketserver
import threading
import time

CRLF = b"\r\n"


class Error(str):
    """A RESP error reply — a string subclass so it prints naturally but stays distinguishable."""


class Incomplete(Exception):
    pass


class Parser:
    """Feed bytes, take complete values. Anything partial stays buffered."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> None:
        self.buf += data

    def __iter__(self):
        while True:
            try:
                value, consumed = self._parse(0)
            except Incomplete:
                return
            del self.buf[:consumed]
            yield value

    def _line(self, start: int) -> tuple[bytes, int]:
        end = self.buf.find(CRLF, start)
        if end < 0:
            raise Incomplete
        return bytes(self.buf[start:end]), end + 2

    def _parse(self, start: int):
        if start >= len(self.buf):
            raise Incomplete
        kind = self.buf[start:start + 1]
        line, pos = self._line(start + 1)

        if kind == b"+":
            return line.decode(), pos
        if kind == b"-":
            return Error(line.decode()), pos
        if kind == b":":
            return int(line), pos
        if kind == b",":                                  # RESP3 double
            return float(line), pos
        if kind == b"#":                                  # RESP3 boolean
            return line == b"t", pos
        if kind == b"_":                                  # RESP3 null
            return None, pos
        if kind in (b"$", b"="):                          # bulk string / verbatim
            length = int(line)
            if length < 0:
                return None, pos
            if len(self.buf) < pos + length + 2:
                raise Incomplete
            return bytes(self.buf[pos:pos + length]), pos + length + 2
        if kind in (b"*", b"~", b">"):                    # array / set / push
            count = int(line)
            if count < 0:
                return None, pos
            items = []
            for _ in range(count):
                item, pos = self._parse(pos)
                items.append(item)
            return items, pos
        if kind == b"%":                                  # RESP3 map
            pairs = {}
            for _ in range(int(line)):
                key, pos = self._parse(pos)
                value, pos = self._parse(pos)
                pairs[key.decode() if isinstance(key, bytes) else key] = value
            return pairs, pos
        raise ValueError(f"unknown RESP type byte {kind!r}")


def encode(value) -> bytes:
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, Error):
        return b"-" + str(value).encode() + CRLF
    if isinstance(value, bool):
        return b":" + (b"1" if value else b"0") + CRLF
    if isinstance(value, int):
        return b":" + str(value).encode() + CRLF
    if isinstance(value, (list, tuple)):
        return b"*" + str(len(value)).encode() + CRLF + b"".join(encode(v) for v in value)
    raw = value if isinstance(value, bytes) else str(value).encode()
    return b"$" + str(len(raw)).encode() + CRLF + raw + CRLF


def encode_command(*args) -> bytes:
    """Commands always go out as an array of bulk strings — the inline form is legacy."""
    return b"*" + str(len(args)).encode() + CRLF + b"".join(
        encode(a if isinstance(a, (bytes, str)) else str(a)) for a in args)


class Client:
    def __init__(self, host="127.0.0.1", port=6379, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.parser = Parser()

    def command(self, *args):
        self.sock.sendall(encode_command(*args))
        while True:
            for value in self.parser:
                return value
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.parser.feed(chunk)

    def close(self):
        self.sock.close()


class MiniRedis(socketserver.StreamRequestHandler):
    store: dict[str, bytes] = {}
    expiry: dict[str, float] = {}
    lock = threading.Lock()

    @classmethod
    def _live(cls, key: str) -> bool:
        deadline = cls.expiry.get(key)
        if deadline and deadline <= time.monotonic():
            cls.store.pop(key, None)
            cls.expiry.pop(key, None)
            return False
        return key in cls.store

    def handle(self):
        parser = Parser()
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            parser.feed(chunk)
            for command in parser:
                if not isinstance(command, list) or not command:
                    continue
                name = command[0].decode().upper()
                args = command[1:]
                self.request.sendall(encode(self.dispatch(name, args)))

    def dispatch(self, name: str, args: list):
        with self.lock:
            if name == "PING":
                return args[0] if args else "PONG"
            if name == "ECHO":
                return args[0]
            if name == "SET":
                key = args[0].decode()
                self.store[key] = args[1]
                self.expiry.pop(key, None)
                for i, opt in enumerate(args[2:]):
                    if opt.decode().upper() == "PX":
                        self.expiry[key] = time.monotonic() + int(args[3 + i]) / 1000
                    elif opt.decode().upper() == "EX":
                        self.expiry[key] = time.monotonic() + int(args[3 + i])
                return "OK"
            if name == "GET":
                key = args[0].decode()
                return self.store[key] if self._live(key) else None
            if name == "DEL":
                return sum(1 for a in args if self.store.pop(a.decode(), None) is not None)
            if name == "EXISTS":
                return sum(1 for a in args if self._live(a.decode()))
            if name == "INCR":
                key = args[0].decode()
                value = int(self.store.get(key, b"0")) + 1
                self.store[key] = str(value).encode()
                return value
            if name == "KEYS":
                pattern = args[0].decode()
                return [k.encode() for k in sorted(self.store) if self._live(k) and fnmatch.fnmatch(k, pattern)]
            if name == "PTTL":
                key = args[0].decode()
                if not self._live(key):
                    return -2
                return int((self.expiry[key] - time.monotonic()) * 1000) if key in self.expiry else -1
            if name == "COMMAND":
                return []
            return Error(f"ERR unknown command '{name}'")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def demo() -> int:
    print("parsing every RESP type\n")
    parser = Parser()
    parser.feed(b"+OK\r\n:42\r\n$5\r\nhello\r\n*3\r\n:1\r\n$3\r\ntwo\r\n*1\r\n+deep\r\n"
                b"-ERR bad\r\n$-1\r\n#t\r\n,3.25\r\n%1\r\n$3\r\nkey\r\n:7\r\n")
    for value in parser:
        print(f"  {type(value).__name__:<6} {value!r}")

    print("\na reply split across every possible byte boundary still parses once")
    payload = encode(["multi", 7, [b"nested", None]])
    for split in range(1, len(payload)):
        p = Parser()
        p.feed(payload[:split])
        got = list(p)
        assert got == [], f"parsed early at byte {split}: {got}"
        p.feed(payload[split:])
        assert list(p) == [[b"multi", 7, [b"nested", None]]], f"failed at split {split}"
    print(f"  {len(payload) - 1}/{len(payload) - 1} split points parsed correctly, none early")

    print("\nclient against the built-in mini server")
    server = Server(("127.0.0.1", 0), MiniRedis)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    client = Client("127.0.0.1", port)
    for cmd in [("PING",), ("SET", "user:1", "ana"), ("GET", "user:1"), ("INCR", "hits"),
                ("INCR", "hits"), ("SET", "temp", "x", "PX", "80"), ("PTTL", "temp"),
                ("KEYS", "*"), ("GET", "missing"), ("BOGUS",)]:
        result = client.command(*cmd)
        print(f"  {' '.join(cmd):<28} -> {result!r}")
    time.sleep(0.1)
    print(f"  {'GET temp (after expiry)':<28} -> {client.command('GET', 'temp')!r}")

    print("\npipelining: 1000 commands, one round trip")
    start = time.perf_counter()
    client.sock.sendall(b"".join(encode_command("SET", f"k{i}", i) for i in range(1000)))
    seen = 0
    while seen < 1000:
        client.parser.feed(client.sock.recv(65536))
        seen += sum(1 for _ in client.parser)
    pipelined = time.perf_counter() - start
    start = time.perf_counter()
    for i in range(200):
        client.command("SET", f"s{i}", i)
    serial = (time.perf_counter() - start) / 200 * 1000
    print(f"  1000 pipelined in {pipelined * 1000:.1f}ms ({pipelined / 1000 * 1e6:.0f}us each) "
          f"vs {serial * 1000:.0f}us each round-tripped -> {serial * 1000 / (pipelined / 1000 * 1e6):.0f}x")
    client.close()
    server.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["client", "serve"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.cmd:
        return demo()
    if args.cmd == "serve":
        server = Server((args.host, args.port), MiniRedis)
        print(f"mini redis on {args.host}:{args.port} — try: redis-cli -p {args.port} set a 1")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        return 0
    client = Client(args.host, args.port)
    print(repr(client.command(*args.args)) if args.args else repr(client.command("PING")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
