#!/usr/bin/env python3
"""Query a Kubernetes cluster for restart loops and post a ranked daily summary.

    pod_restarts.py --context prod --namespace default
    pod_restarts.py --demo

Shells out to `kubectl get pods -o json` (no kubernetes client library) and
ranks pods by restart count, but the useful part isn't the count — it's the
reason. A pod that OOMKilled needs more memory; one that hit a liveness probe
timeout needs a slower probe or a faster startup; a plain crash needs someone
to read the logs. Grouping by (namespace, reason) instead of listing every pod
turns "43 pods restarted" into "3 real problems, each affecting N pods."
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ContainerStatus:
    name: str
    restart_count: int
    ready: bool
    last_reason: str | None
    last_message: str | None
    last_exit_code: int | None
    last_finished_at: str | None


@dataclass
class PodRestartInfo:
    namespace: str
    pod_name: str
    node: str | None
    containers: list[ContainerStatus] = field(default_factory=list)

    @property
    def total_restarts(self) -> int:
        return sum(c.restart_count for c in self.containers)

    @property
    def worst_reason(self) -> str:
        with_reason = [c for c in self.containers if c.last_reason]
        if not with_reason:
            return "Unknown"
        return max(with_reason, key=lambda c: c.restart_count).last_reason or "Unknown"


REASON_ADVICE = {
    "OOMKilled": "container exceeded its memory limit — raise the limit or find the leak",
    "Error": "process exited non-zero — check application logs for the stack trace",
    "CrashLoopBackOff": "container keeps crashing immediately after start — check the entrypoint",
    "Completed": "container finished normally (exit 0) — probably a Job, not a real restart",
    "DeadlineExceeded": "liveness/readiness probe timed out — probe may be too aggressive, or app too slow to start",
    "ContainerCannotRun": "the container failed to start at all — check the image and command",
}


def parse_pods_json(raw: dict) -> list[PodRestartInfo]:
    pods = []
    for item in raw.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        spec = item.get("spec", {})
        pod = PodRestartInfo(
            namespace=metadata.get("namespace", "default"),
            pod_name=metadata.get("name", "unknown"),
            node=spec.get("nodeName"),
        )
        for cs in status.get("containerStatuses", []):
            last_state = cs.get("lastState", {})
            terminated = last_state.get("terminated", {})
            pod.containers.append(
                ContainerStatus(
                    name=cs.get("name", "?"),
                    restart_count=cs.get("restartCount", 0),
                    ready=cs.get("ready", False),
                    last_reason=terminated.get("reason"),
                    last_message=terminated.get("message"),
                    last_exit_code=terminated.get("exitCode"),
                    last_finished_at=terminated.get("finishedAt"),
                )
            )
        pods.append(pod)
    return pods


def fetch_pods(context: str | None, namespace: str | None) -> list[PodRestartInfo]:
    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl not found on PATH")
    cmd = ["kubectl", "get", "pods", "-o", "json"]
    if context:
        cmd += ["--context", context]
    if namespace:
        cmd += ["-n", namespace]
    else:
        cmd += ["--all-namespaces"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl failed: {result.stderr.strip()}")
    return parse_pods_json(json.loads(result.stdout))


@dataclass
class ReasonGroup:
    namespace: str
    reason: str
    pods: list[PodRestartInfo]
    total_restarts: int


def group_by_reason(pods: list[PodRestartInfo], min_restarts: int = 1) -> list[ReasonGroup]:
    groups: dict[tuple[str, str], list[PodRestartInfo]] = defaultdict(list)
    for pod in pods:
        if pod.total_restarts < min_restarts:
            continue
        groups[(pod.namespace, pod.worst_reason)].append(pod)

    out = [ReasonGroup(ns, reason, group, sum(p.total_restarts for p in group)) for (ns, reason), group in groups.items()]
    out.sort(key=lambda g: -g.total_restarts)
    return out


def format_report(groups: list[ReasonGroup], top_n: int = 10) -> str:
    if not groups:
        return "No pod restarts to report — everything's stable."

    lines = [f"Pod Restart Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    total_pods = sum(len(g.pods) for g in groups)
    total_restarts = sum(g.total_restarts for g in groups)
    lines.append(f"{total_pods} pods with restarts, {total_restarts} restarts total, "
                 f"{len(groups)} distinct problem groups\n")

    for group in groups[:top_n]:
        advice = REASON_ADVICE.get(group.reason, "no known pattern for this reason — check logs directly")
        lines.append(f"[{group.namespace}] {group.reason}  —  {len(group.pods)} pod(s), {group.total_restarts} restarts")
        lines.append(f"  {advice}")
        worst = sorted(group.pods, key=lambda p: -p.total_restarts)[:3]
        for pod in worst:
            lines.append(f"    {pod.pod_name}  ({pod.total_restarts} restarts, node {pod.node or '?'})")
        if len(group.pods) > 3:
            lines.append(f"    ...and {len(group.pods) - 3} more")
        lines.append("")

    if len(groups) > top_n:
        lines.append(f"...and {len(groups) - top_n} more problem groups not shown")

    return "\n".join(lines)


# ------------------------------------------------------------ demo

def build_demo_pods_json() -> dict:
    def pod(namespace, name, node, containers):
        return {
            "metadata": {"namespace": namespace, "name": name},
            "spec": {"nodeName": node},
            "status": {"containerStatuses": containers},
        }

    def container(name, restarts, reason=None, exit_code=None, message=None):
        cs = {"name": name, "restartCount": restarts, "ready": restarts == 0}
        if reason:
            cs["lastState"] = {"terminated": {"reason": reason, "exitCode": exit_code, "message": message, "finishedAt": "2026-08-26T04:00:00Z"}}
        return cs

    items = [
        pod("payments", "payments-api-7d9f-abc12", "node-1", [container("api", 14, "OOMKilled", 137)]),
        pod("payments", "payments-api-7d9f-xyz34", "node-2", [container("api", 11, "OOMKilled", 137)]),
        pod("payments", "payments-api-7d9f-qwe56", "node-1", [container("api", 9, "OOMKilled", 137)]),
        pod("payments", "payments-worker-2c1a-abc", "node-3", [container("worker", 3, "Error", 1, "panic: nil pointer")]),
        pod("web", "web-frontend-99a-abc", "node-2", [container("nginx", 2, "Error", 1)]),
        pod("web", "web-frontend-99a-def", "node-3", [container("nginx", 1, "Error", 1)]),
        pod("web", "web-frontend-99a-ghi", "node-1", [container("nginx", 0)]),  # healthy, no restarts
        pod("batch", "nightly-etl-job-4f2", "node-2", [container("etl", 1, "Completed", 0)]),
        pod("monitoring", "prometheus-0", "node-1", [container("prometheus", 6, "DeadlineExceeded", None, "liveness probe failed")]),
        pod("checkout", "checkout-api-8b3-abc", "node-3", [container("api", 22, "CrashLoopBackOff", 2, "config missing")]),
    ]
    return {"items": items}


def demo() -> int:
    print("simulating `kubectl get pods --all-namespaces -o json` output\n")
    raw = build_demo_pods_json()
    pods = parse_pods_json(raw)
    print(f"{len(pods)} pods found across the cluster\n")

    groups = group_by_reason(pods, min_restarts=1)
    print(format_report(groups))

    print("\n\nnote: 3 separate payments-api pods each individually restarted OOMKilled")
    print("(14, 11, 9 times) — grouped, that's one problem ('payments-api needs more memory'")
    print("affecting 3 pods, 34 restarts) instead of three unrelated-looking alert lines.")
    print("the nightly-etl-job with reason 'Completed' correctly sorts to the bottom — it's")
    print("a Job finishing normally, not an actual failure, even though restartCount is nonzero.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", help="kubectl context")
    ap.add_argument("--namespace", "-n", help="restrict to one namespace (default: all)")
    ap.add_argument("--min-restarts", type=int, default=3, help="ignore pods below this restart count")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        return demo()

    try:
        pods = fetch_pods(args.context, args.namespace)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("(try --demo to see the report format without a real cluster)", file=sys.stderr)
        return 1

    groups = group_by_reason(pods, args.min_restarts)
    print(format_report(groups, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
