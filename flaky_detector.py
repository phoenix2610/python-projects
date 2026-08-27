#!/usr/bin/env python3
"""Rerun failing tests, track flake rates over time, and flag what crosses a threshold.

    flaky_detector.py run --cmd "pytest tests/" --reruns 3
    flaky_detector.py --demo

A test that fails once and passes on rerun without any code change is either
flaky or timing-dependent — either way, a single failure shouldn't block a
merge, but a test that flakes often enough is actively harmful (it trains
people to ignore CI red). This runs the suite, reruns only what failed, and
persists a rolling pass/fail history per test name across invocations, so
"this test failed 1 time out of 40 runs over the last two weeks" is a real
number instead of a guess from institutional memory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

HISTORY_PATH = os.path.expanduser(os.environ.get("FLAKY_HISTORY", "~/.flaky-test-history.json"))


@dataclass
class TestOutcome:
    name: str
    passed: bool
    duration_ms: float


@dataclass
class RunResult:
    outcomes: list[TestOutcome]
    exit_code: int


# A simplified pytest-style result line parser: "test_foo.py::test_bar PASSED [ 12%]"
RESULT_LINE_RE = re.compile(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR)\b")


def parse_test_output(output: str) -> list[TestOutcome]:
    outcomes = []
    for line in output.splitlines():
        match = RESULT_LINE_RE.match(line.strip())
        if match:
            name, status = match.groups()
            outcomes.append(TestOutcome(name, status == "PASSED", 0.0))
    return outcomes


def run_tests(cmd: str, test_filter: list[str] | None = None) -> RunResult:
    full_cmd = cmd
    if test_filter:
        full_cmd = f"{cmd} {' '.join(test_filter)}"
    proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=300)
    return RunResult(parse_test_output(proc.stdout), proc.returncode)


@dataclass
class TestHistory:
    runs: dict[str, list[bool]] = field(default_factory=dict)  # test name -> list of pass/fail, newest last

    def record(self, name: str, passed: bool, keep_last: int = 50) -> None:
        history = self.runs.setdefault(name, [])
        history.append(passed)
        del history[:-keep_last]

    def flake_rate(self, name: str) -> float:
        history = self.runs.get(name, [])
        if not history:
            return 0.0
        return 1 - sum(history) / len(history)

    def is_flaky(self, name: str, min_runs: int = 5, threshold: float = 0.05) -> bool:
        history = self.runs.get(name, [])
        if len(history) < min_runs:
            return False
        rate = self.flake_rate(name)
        return 0 < rate < 1.0 and rate >= threshold  # some failures, some passes, above threshold


def load_history(path: str) -> TestHistory:
    if not os.path.exists(path):
        return TestHistory()
    with open(path) as fh:
        raw = json.load(fh)
    return TestHistory(runs=raw.get("runs", {}))


def save_history(history: TestHistory, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"runs": history.runs}, fh, indent=1)
    os.replace(tmp, path)


@dataclass
class FlakyRunReport:
    initial_failures: list[str]
    confirmed_failures: list[str]  # failed on rerun too — a real failure
    flaky_this_run: list[str]  # failed then passed on rerun
    newly_flagged: list[str]  # crossed the flakiness threshold based on history


def run_with_reruns(cmd: str, history: TestHistory, max_reruns: int = 2) -> FlakyRunReport:
    initial = run_tests(cmd)
    for outcome in initial.outcomes:
        history.record(outcome.name, outcome.passed)

    initial_failures = [o.name for o in initial.outcomes if not o.passed]
    confirmed = []
    flaky_this_run = []

    if initial_failures:
        remaining = list(initial_failures)
        for attempt in range(max_reruns):
            if not remaining:
                break
            requested = set(remaining)
            rerun = run_tests(cmd, test_filter=remaining)
            still_failing = []
            for outcome in rerun.outcomes:
                # Only trust outcomes for tests we actually asked to be rerun. A
                # test command that doesn't honor the filter (or over-reports) could
                # otherwise mark an already-passing test "flaky" just for showing up
                # in this output, without it ever having failed in the first place.
                if outcome.name not in requested:
                    continue
                history.record(outcome.name, outcome.passed)
                if outcome.passed:
                    flaky_this_run.append(outcome.name)
                else:
                    still_failing.append(outcome.name)
            remaining = still_failing
        confirmed = remaining

    newly_flagged = [name for name in history.runs if history.is_flaky(name)]
    return FlakyRunReport(initial_failures, confirmed, flaky_this_run, newly_flagged)


def format_report(report: FlakyRunReport, history: TestHistory) -> str:
    lines = []
    if report.confirmed_failures:
        lines.append(f"REAL FAILURES ({len(report.confirmed_failures)}) — failed every rerun, this is a genuine break:")
        for name in report.confirmed_failures:
            lines.append(f"  {name}")
        lines.append("")

    if report.flaky_this_run:
        lines.append(f"flaky this run ({len(report.flaky_this_run)}) — failed once, passed on rerun:")
        for name in report.flaky_this_run:
            lines.append(f"  {name}  (flake rate: {history.flake_rate(name):.0%} over {len(history.runs.get(name, []))} runs)")
        lines.append("")

    if not report.initial_failures:
        lines.append("all tests passed on the first run.")

    if report.newly_flagged:
        lines.append(f"tests over the flakiness threshold, tracked historically ({len(report.newly_flagged)}):")
        for name in sorted(report.newly_flagged, key=lambda n: -history.flake_rate(n)):
            lines.append(f"  {name}: {history.flake_rate(name):.0%} flake rate over {len(history.runs[name])} runs")

    return "\n".join(lines) if lines else "no results parsed from test output"


# ------------------------------------------------------------ demo

def demo() -> int:
    print("simulating 12 CI runs of a test suite over two weeks\n")
    print("(one test is genuinely broken, one is flaky ~30% of the time, the rest are solid)\n")

    import random
    rng = random.Random(11)
    history = TestHistory()

    test_names = ["test_login", "test_checkout", "test_search", "test_flaky_upload", "test_broken_export"]

    # simulate 11 historical runs before "today"
    for run_num in range(11):
        for name in test_names:
            if name == "test_broken_export":
                passed = False  # always fails
            elif name == "test_flaky_upload":
                passed = rng.random() > 0.3  # fails ~30% of the time
            else:
                passed = True
            history.record(name, passed)

    print("historical flake rates going into today's run:")
    for name in test_names:
        rate = history.flake_rate(name)
        print(f"  {name:<22} {rate:.0%} fail rate over {len(history.runs[name])} runs")

    print("\n--- today's run ---\n")
    # simulate today's run directly (bypassing subprocess, since there's no real test suite here)
    initial_outcomes = {
        "test_login": True,
        "test_checkout": True,
        "test_search": True,
        "test_flaky_upload": False,  # fails initially...
        "test_broken_export": False,  # ...and this one always fails
    }
    for name, passed in initial_outcomes.items():
        history.record(name, passed)

    initial_failures = [n for n, p in initial_outcomes.items() if not p]
    # rerun only the failures: flaky_upload passes this time, broken_export fails again
    rerun_outcomes = {"test_flaky_upload": True, "test_broken_export": False}
    for name, passed in rerun_outcomes.items():
        history.record(name, passed)

    confirmed = [n for n, p in rerun_outcomes.items() if not p]
    flaky_this_run = [n for n, p in rerun_outcomes.items() if p]
    newly_flagged = [name for name in history.runs if history.is_flaky(name)]

    report = FlakyRunReport(initial_failures, confirmed, flaky_this_run, newly_flagged)
    print(format_report(report, history))

    print(f"\n\nnote: test_broken_export failed BOTH the initial run and the rerun — it is reported")
    print(f"as a real failure that should block the merge. test_flaky_upload failed initially but")
    print(f"passed on rerun, so it does NOT block anything today, but it's still flagged separately")
    print(f"as a tracked flaky test because its historical rate ({history.flake_rate('test_flaky_upload'):.0%}) is well above the 5% threshold —")
    print(f"the two categories ('blocks this merge' vs 'needs someone to eventually fix it') are")
    print(f"reported separately on purpose, since conflating them either blocks merges on noise")
    print(f"or lets a real flake problem hide behind 'it passed on rerun.'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # `dest="subcommand"`, not "cmd" — the `run` subcommand's own --cmd argument
    # would otherwise collide with the subparsers' dest on the same attribute name,
    # silently overwriting "run" with the test-command string on every real invocation.
    sub = ap.add_subparsers(dest="subcommand")
    run_p = sub.add_parser("run")
    run_p.add_argument("--cmd", required=True, help="the test command to run, e.g. 'pytest tests/'")
    run_p.add_argument("--reruns", type=int, default=2)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo or not args.subcommand:
        return demo()

    if args.subcommand == "run":
        history = load_history(HISTORY_PATH)
        report = run_with_reruns(args.cmd, history, args.reruns)
        save_history(history, HISTORY_PATH)
        print(format_report(report, history))
        return 1 if report.confirmed_failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
