#!/usr/bin/env python3
"""Show the bytecode behind your Python, annotated, and diff two ways of writing it.

    bcexplore.py mymodule.py --function parse
    bcexplore.py --expr "sum(x*x for x in data)"
    bcexplore.py --compare "[x*2 for x in xs]" "list(map(lambda x: x*2, xs))"
    bcexplore.py --demo

The annotations are the part `dis` leaves out: what each opcode does to the value
stack, and why the compiler chose it. Comparing two spellings of the same idea is
where it earns its keep — you see the extra function call, or the constant that
got folded at compile time.
"""
from __future__ import annotations

import argparse
import dis
import importlib.util
import sys
import textwrap
import time
from types import CodeType, FunctionType

NOTES = {
    "LOAD_CONST": "push a literal baked into the code object at compile time",
    "LOAD_FAST": "push a local — an array index, the cheapest load there is",
    "LOAD_FAST_BORROW": "push a local without touching its refcount (3.14 optimisation)",
    "LOAD_GLOBAL": "dict lookup in globals, then builtins — why locals are faster",
    "LOAD_NAME": "locals, then globals, then builtins: module-level scope rules",
    "LOAD_ATTR": "attribute lookup, which may run __getattr__ or a descriptor",
    "LOAD_METHOD": "attribute lookup that avoids building a bound method object",
    "STORE_FAST": "pop into a local slot",
    "BINARY_OP": "pop two, push one — dispatches on the operands' types",
    "COMPARE_OP": "pop two, push a bool",
    "CALL": "pop the callable and its arguments, push the return value",
    "RETURN_VALUE": "pop the top of stack and hand it back to the caller",
    "RETURN_CONST": "return a literal without pushing it first",
    "GET_ITER": "replace the iterable on the stack with its iterator",
    "FOR_ITER": "call __next__; jump past the loop when it raises StopIteration",
    "JUMP_BACKWARD": "the loop edge — the only backward jump in a for loop",
    "POP_JUMP_IF_FALSE": "pop, and branch when falsey",
    "POP_JUMP_IF_TRUE": "pop, and branch when truthy",
    "BUILD_LIST": "pop n items, push a list — how a list display is built",
    "LIST_APPEND": "append to the list n slots down: a comprehension's accumulator",
    "MAKE_FUNCTION": "build a function object from a code constant",
    "SET_FUNCTION_ATTRIBUTE": "attach defaults/closure to the function just made",
    "MAKE_CELL": "allocate a cell so an inner function can close over this variable",
    "LOAD_DEREF": "read a closed-over variable through its cell",
    "COPY_FREE_VARS": "bring the enclosing function's cells into this frame",
    "UNPACK_SEQUENCE": "pop one, push its items — tuple unpacking",
    "SWAP": "reorder the stack, usually to avoid a temporary",
    "NOP": "placeholder left by the peephole optimiser",
    "RESUME": "bookkeeping at function entry; costs nothing at runtime",
    "PUSH_NULL": "reserve the slot the calling convention expects",
    "CACHE": "inline cache slot for the adaptive interpreter, not executed",
    "LOAD_SMALL_INT": "push a cached small integer — no constant table entry needed",
    "LOAD_FAST_AND_CLEAR": "push a local and null its slot, so the comprehension gets a fresh binding",
    "STORE_FAST_LOAD_FAST": "one opcode doing a store then a load, fused by the compiler",
    "END_FOR": "drop the exhausted loop value",
    "POP_ITER": "drop the iterator now that the loop is done",
    "POP_TOP": "discard the top of stack — a value nobody wanted",
    "RERAISE": "re-raise the in-flight exception after cleanup",
    "TO_BOOL": "coerce via __bool__ before a branch",
    "CALL_FUNCTION_EX": "call with *args/**kwargs packed into a tuple and dict",
    "BUILD_TUPLE": "pop n items, push a tuple",
    "BUILD_MAP": "pop 2n items, push a dict",
    "CONTAINS_OP": "the `in` operator: pop two, push a bool",
    "IS_OP": "identity comparison, no __eq__ call",
}


def annotate(code: CodeType, show_cache: bool = False) -> None:
    depth = 0
    for ins in dis.get_instructions(code, adaptive=False):
        if ins.opname == "CACHE" and not show_cache:
            continue
        effect = ""
        try:
            effect = f"{dis.stack_effect(ins.opcode, ins.arg):+d}"
            depth += dis.stack_effect(ins.opcode, ins.arg)
        except (ValueError, TypeError):
            effect = "  "
        marker = ">>" if ins.is_jump_target else "  "
        arg = f" {ins.argrepr}" if ins.argrepr else ""
        note = NOTES.get(ins.opname, "")
        print(f"  {marker} {ins.offset:>4}  {ins.opname}{arg}".ljust(52)
              + f" {effect:>3} stack~{max(depth, 0):<3}" + (f"  {note}" if note else ""))
    consts = [c for c in code.co_consts if isinstance(c, CodeType)]
    for nested in consts:
        print(f"\n  --- nested code object: {nested.co_name} ---")
        annotate(nested, show_cache)


def summarize(code: CodeType) -> dict:
    counts: dict[str, int] = {}
    for ins in dis.get_instructions(code):
        if ins.opname != "CACHE":
            counts[ins.opname] = counts.get(ins.opname, 0) + 1
    return counts


def compile_expr(expr: str, names: dict) -> CodeType:
    body = f"def __probe__({', '.join(names)}):\n    return {expr}\n"
    module = compile(textwrap.dedent(body), "<expr>", "exec")
    return next(c for c in module.co_consts if isinstance(c, CodeType))


def bench(expr: str, env: dict, rounds: int = 20000) -> float:
    fn = FunctionType(compile_expr(expr, env), {"__builtins__": __builtins__})
    args = tuple(env.values())
    start = time.perf_counter()
    for _ in range(rounds):
        fn(*args)
    return (time.perf_counter() - start) / rounds * 1e6


def compare(left: str, right: str, env: dict) -> None:
    for label, expr in (("A", left), ("B", right)):
        code = compile_expr(expr, env)
        counts = summarize(code)
        print(f"\n{label}: {expr}")
        annotate(code)
        print(f"  {sum(counts.values())} instructions, "
              f"{len(code.co_consts)} constants, stack size {code.co_stacksize}")
    a_us, b_us = bench(left, env), bench(right, env)
    faster, slower = ("A", "B") if a_us < b_us else ("B", "A")
    print(f"\n  timing over 20k runs: A {a_us:.2f}us, B {b_us:.2f}us "
          f"-> {faster} is {max(a_us, b_us) / min(a_us, b_us):.2f}x faster than {slower}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="python file to disassemble")
    ap.add_argument("--function", help="only this function")
    ap.add_argument("--expr", help="disassemble a single expression")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="two expressions to compare")
    ap.add_argument("--cache", action="store_true", help="show CACHE slots the adaptive interpreter uses")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    env = {"xs": list(range(64)), "data": list(range(64))}

    if args.demo:
        print(f"python {sys.version.split()[0]}\n")
        print("=" * 78)
        print("constant folding: the compiler does the arithmetic, not the runtime")
        print("=" * 78)
        annotate(compile_expr("60 * 60 * 24", {}))
        print("\n" + "=" * 78)
        print("comprehension vs map+lambda")
        print("=" * 78)
        compare("[x * 2 for x in xs]", "list(map(lambda x: x * 2, xs))", {"xs": env["xs"]})
        print("\n" + "=" * 78)
        print("attribute lookup in a loop vs hoisting it out")
        print("=" * 78)
        compare("[str(x).upper() for x in xs]", "[u(x) for x in xs]",
                {"xs": env["xs"], "u": lambda v: str(v).upper()})
        return 0

    if args.compare:
        compare(args.compare[0], args.compare[1], env)
        return 0
    if args.expr:
        annotate(compile_expr(args.expr, env), args.cache)
        return 0
    if not args.path:
        ap.error("pass a file, --expr, --compare or --demo")

    spec = importlib.util.spec_from_file_location("target", args.path)
    source = open(args.path).read()
    code = compile(source, args.path, "exec")
    if args.function:
        found = None
        for const in code.co_consts:
            if isinstance(const, CodeType) and const.co_name == args.function:
                found = const
        if not found:
            names = [c.co_name for c in code.co_consts if isinstance(c, CodeType)]
            print(f"no function {args.function!r} (found: {', '.join(names)})", file=sys.stderr)
            return 1
        code = found
    annotate(code, args.cache)
    counts = summarize(code)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    print(f"\n  {sum(counts.values())} instructions; most common: "
          + ", ".join(f"{op} x{n}" for op, n in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
