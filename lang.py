#!/usr/bin/env python3
"""A small language: tokenizer, Pratt parser, tree-walking evaluator, real closures.

    lang.py program.toy
    lang.py --demo
    lang.py --ast program.toy      # dump the parse tree

Precedence lives in one table (`BINDING`) rather than in a ladder of grammar
rules — that is the whole point of Pratt parsing: `2 + 3 * 4 ^ 2` parses right
for the same reason `-x.y(1)` does, without a rule per level.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<name>[A-Za-z_]\w*)
  | (?P<op><=|>=|==|!=|&&|\|\||[-+*/%^<>=(),{};!])
""", re.X)
KEYWORDS = {"let", "fn", "if", "else", "while", "return", "true", "false", "nil", "print"}
# (left binding power, right binding power) — right < left makes an operator right-associative
BINDING = {"||": (1, 2), "&&": (3, 4), "==": (5, 6), "!=": (5, 6), "<": (7, 8), ">": (7, 8),
           "<=": (7, 8), ">=": (7, 8), "+": (9, 10), "-": (9, 10), "*": (11, 12), "/": (11, 12),
           "%": (11, 12), "^": (16, 15)}


@dataclass
class Token:
    kind: str
    value: str
    line: int


def tokenize(src: str) -> list[Token]:
    tokens, pos, line = [], 0, 1
    while pos < len(src):
        m = TOKEN.match(src, pos)
        if not m:
            raise SyntaxError(f"line {line}: unexpected {src[pos]!r}")
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        line += text.count("\n")
        if kind == "ws":
            continue
        if kind == "name" and text in KEYWORDS:
            kind = text
        tokens.append(Token(kind, text, line))
    tokens.append(Token("eof", "", line))
    return tokens


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens, self.i = tokens, 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def next(self) -> Token:
        self.i += 1
        return self.tokens[self.i - 1]

    def expect(self, kind: str) -> Token:
        tok = self.next()
        if tok.kind != kind and tok.value != kind:
            raise SyntaxError(f"line {tok.line}: expected {kind!r}, found {tok.value!r}")
        return tok

    def at(self, value: str) -> bool:
        tok = self.peek()
        return tok.value == value or tok.kind == value

    def program(self) -> list:
        body = []
        while not self.at("eof"):
            body.append(self.statement())
        return body

    def block(self) -> list:
        self.expect("{")
        body = []
        while not self.at("}"):
            body.append(self.statement())
        self.expect("}")
        return body

    def statement(self):
        tok = self.peek()
        if tok.kind == "let":
            self.next()
            name = self.expect("name").value
            self.expect("=")
            value = self.expression()
            self.at(";") and self.next()
            return ("let", name, value)
        if tok.kind == "fn":
            self.next()
            name = self.expect("name").value
            return ("let", name, self.function())
        if tok.kind == "if":
            self.next()
            cond = self.expression()
            then = self.block()
            other = None
            if self.at("else"):
                self.next()
                other = self.block() if self.at("{") else [self.statement()]
            return ("if", cond, then, other)
        if tok.kind == "while":
            self.next()
            return ("while", self.expression(), self.block())
        if tok.kind == "return":
            self.next()
            value = None if self.at(";") or self.at("}") else self.expression()
            self.at(";") and self.next()
            return ("return", value)
        if tok.kind == "print":
            self.next()
            value = self.expression()
            self.at(";") and self.next()
            return ("print", value)
        expr = self.expression()
        self.at(";") and self.next()
        return ("expr", expr)

    def function(self):
        self.expect("(")
        params = []
        while not self.at(")"):
            params.append(self.expect("name").value)
            if self.at(","):
                self.next()
        self.expect(")")
        return ("fn", params, self.block())

    def expression(self, min_bp: int = 0):
        tok = self.next()
        if tok.kind == "number":
            left = ("num", float(tok.value))
        elif tok.kind == "string":
            left = ("str", tok.value[1:-1].encode().decode("unicode_escape"))
        elif tok.kind in ("true", "false"):
            left = ("bool", tok.kind == "true")
        elif tok.kind == "nil":
            left = ("nil",)
        elif tok.kind == "fn":
            left = self.function()
        elif tok.kind == "name":
            left = ("var", tok.value)
        elif tok.value in ("-", "!"):
            left = ("unary", tok.value, self.expression(14))
        elif tok.value == "(":
            left = self.expression()
            self.expect(")")
        else:
            raise SyntaxError(f"line {tok.line}: unexpected {tok.value!r}")

        while True:
            op = self.peek().value
            if op == "(":                       # call binds tighter than any operator
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.expression())
                    if self.at(","):
                        self.next()
                self.expect(")")
                left = ("call", left, args)
                continue
            if op == "=" and left[0] == "var":
                self.next()
                left = ("assign", left[1], self.expression())
                continue
            if op not in BINDING:
                break
            lbp, rbp = BINDING[op]
            if lbp < min_bp:
                break
            self.next()
            left = ("bin", op, left, self.expression(rbp))
        return left


class Return(Exception):
    def __init__(self, value):
        self.value = value


@dataclass
class Env:
    vars: dict = field(default_factory=dict)
    parent: "Env | None" = None

    def get(self, name: str):
        env = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        raise NameError(f"undefined variable {name!r}")

    def set(self, name: str, value) -> None:
        env = self
        while env is not None:
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        raise NameError(f"cannot assign to undefined {name!r}")


@dataclass
class Closure:
    params: list
    body: list
    env: Env                    # the environment at definition time — this is what makes it a closure

    def __repr__(self):
        return f"<fn({', '.join(self.params)})>"


def truthy(value) -> bool:
    return value not in (False, None, 0, 0.0, "")


def stringify(value) -> str:
    if value is None:
        return "nil"
    if value is True or value is False:
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Interpreter:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.globals = Env({"len": lambda s: float(len(s)), "str": stringify,
                            "num": lambda s: float(s), "abs": abs})

    def run(self, body: list, env: Env | None = None):
        env = env or self.globals
        result = None
        for node in body:
            result = self.exec(node, env)
        return result

    def exec(self, node, env: Env):
        kind = node[0]
        if kind == "let":
            env.vars[node[1]] = self.eval(node[2], env)
        elif kind == "print":
            print(stringify(self.eval(node[1], env)), file=self.out)
        elif kind == "expr":
            return self.eval(node[1], env)
        elif kind == "if":
            if truthy(self.eval(node[1], env)):
                return self.run(node[2], Env(parent=env))
            if node[3]:
                return self.run(node[3], Env(parent=env))
        elif kind == "while":
            while truthy(self.eval(node[1], env)):
                self.run(node[2], Env(parent=env))
        elif kind == "return":
            raise Return(self.eval(node[1], env) if node[1] else None)
        return None

    def eval(self, node, env: Env):
        kind = node[0]
        if kind == "num" or kind == "str" or kind == "bool":
            return node[1]
        if kind == "nil":
            return None
        if kind == "var":
            return env.get(node[1])
        if kind == "assign":
            value = self.eval(node[2], env)
            env.set(node[1], value)
            return value
        if kind == "fn":
            return Closure(node[1], node[2], env)
        if kind == "unary":
            value = self.eval(node[2], env)
            return -value if node[1] == "-" else not truthy(value)
        if kind == "bin":
            op = node[1]
            if op == "&&":
                left = self.eval(node[2], env)
                return self.eval(node[3], env) if truthy(left) else left
            if op == "||":
                left = self.eval(node[2], env)
                return left if truthy(left) else self.eval(node[3], env)
            a, b = self.eval(node[2], env), self.eval(node[3], env)
            ops = {"+": lambda: a + b if not isinstance(a, str) else a + stringify(b),
                   "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b,
                   "%": lambda: a % b, "^": lambda: a ** b, "<": lambda: a < b, ">": lambda: a > b,
                   "<=": lambda: a <= b, ">=": lambda: a >= b, "==": lambda: a == b, "!=": lambda: a != b}
            return ops[op]()
        if kind == "call":
            fn = self.eval(node[1], env)
            args = [self.eval(a, env) for a in node[2]]
            if callable(fn) and not isinstance(fn, Closure):
                return fn(*args)
            if not isinstance(fn, Closure):
                raise TypeError(f"{stringify(fn)} is not callable")
            if len(args) != len(fn.params):
                raise TypeError(f"expected {len(fn.params)} arguments, got {len(args)}")
            call_env = Env(dict(zip(fn.params, args)), fn.env)
            try:
                self.run(fn.body, call_env)
            except Return as ret:
                return ret.value
            return None
        raise SyntaxError(f"cannot evaluate {node!r}")


DEMO = """
// closures capture their defining environment, not the caller's
fn counter(start) {
  let n = start;
  return fn() { n = n + 1; return n; };
}
let next = counter(10);
print next();          // 11
print next();          // 12

// precedence comes out of the binding table
print 2 + 3 * 4 ^ 2;   // 50, and ^ is right-associative

fn fib(n) {
  if n < 2 { return n; }
  return fib(n - 1) + fib(n - 2);
}
print fib(20);

let i = 0;
let total = 0;
while i < 5 { total = total + i * i; i = i + 1; }
print "sum of squares: " + total;
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--ast", action="store_true")
    args = ap.parse_args()

    src = DEMO if args.demo or not args.file else open(args.file).read()
    tree = Parser(tokenize(src)).program()
    if args.ast:
        import pprint
        pprint.pprint(tree, width=100)
        return 0
    try:
        Interpreter().run(tree)
    except (NameError, TypeError, SyntaxError, ZeroDivisionError) as exc:
        print(f"runtime error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
