#!/usr/bin/env python3
"""Run a scheduled `terraform plan` and report resources that changed outside of code.

    tf_drift.py --dir ./infra
    tf_drift.py --demo

Runs `terraform plan -detailed-exitcode -out=plan.tfplan` then `terraform show
-json` to get a structured diff rather than parsing the colored CLI output.
Exit code 2 from plan means "changes exist" — this then classifies each changed
resource as an *update* (attributes differ) or a genuine *drift* candidate
worth flagging loudly: something that was deleted outside Terraform (present in
state, absent in reality) or has attributes an operator is unlikely to have
intended to change by hand (tags, security group rules).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# Attribute names that, when they drift, are usually a person clicking around in
# a cloud console rather than a deliberate change — worth calling out specially.
# Network-access fields need both the old nested-block names (`ingress`/`egress`
# inside aws_security_group) AND the newer standalone-resource field names
# (`cidr_blocks` on aws_security_group_rule) — providers renamed the shape of
# this over time, and missing either one lets an opened-to-the-world rule slip
# into "routine drift" instead of the list that gets read first.
SENSITIVE_ATTRS = {
    "ingress", "egress", "cidr_blocks", "ipv6_cidr_blocks", "source_ranges",
    "policy", "tags", "iam_policy", "acl", "public_access_block",
}


@dataclass
class ResourceChange:
    address: str
    resource_type: str
    actions: list[str]  # e.g. ["update"], ["delete", "create"] for replace, ["delete"]
    before: dict
    after: dict
    changed_attrs: list[str] = field(default_factory=list)

    @property
    def is_destructive(self) -> bool:
        return "delete" in self.actions

    @property
    def is_drift_worth_flagging(self) -> bool:
        return self.is_destructive or any(attr in SENSITIVE_ATTRS for attr in self.changed_attrs)


def diff_attrs(before: dict, after: dict) -> list[str]:
    changed = []
    keys = set(before or {}) | set(after or {})
    for key in sorted(keys):
        if (before or {}).get(key) != (after or {}).get(key):
            changed.append(key)
    return changed


def parse_plan_json(plan: dict) -> list[ResourceChange]:
    changes = []
    for rc in plan.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions", [])
        if actions == ["no-op"] or not actions:
            continue
        before = change.get("before") or {}
        after = change.get("after") or {}
        changes.append(
            ResourceChange(
                address=rc.get("address", "?"),
                resource_type=rc.get("type", "?"),
                actions=actions,
                before=before,
                after=after,
                changed_attrs=diff_attrs(before, after),
            )
        )
    return changes


def run_terraform_plan(directory: str) -> dict:
    if not shutil.which("terraform"):
        raise RuntimeError("terraform not found on PATH")
    plan_path = f"{directory}/.drift-check.tfplan"
    plan_result = subprocess.run(
        ["terraform", "plan", "-detailed-exitcode", "-out", plan_path, "-input=false"],
        cwd=directory, capture_output=True, text=True,
    )
    if plan_result.returncode not in (0, 2):
        raise RuntimeError(f"terraform plan failed: {plan_result.stderr}")
    if plan_result.returncode == 0:
        return {"resource_changes": []}

    show_result = subprocess.run(["terraform", "show", "-json", plan_path], cwd=directory, capture_output=True, text=True)
    if show_result.returncode != 0:
        raise RuntimeError(f"terraform show failed: {show_result.stderr}")
    return json.loads(show_result.stdout)


def format_report(changes: list[ResourceChange]) -> str:
    if not changes:
        return "No drift detected — infrastructure matches the last applied state."

    flagged = [c for c in changes if c.is_drift_worth_flagging]
    routine = [c for c in changes if not c.is_drift_worth_flagging]

    lines = [f"{len(changes)} resources have drifted from Terraform state\n"]

    if flagged:
        lines.append(f"NEEDS ATTENTION ({len(flagged)}):")
        for c in flagged:
            action_label = "DELETED outside Terraform" if c.actions == ["delete"] else \
                           "will be REPLACED" if set(c.actions) == {"delete", "create"} else \
                           f"changed: {', '.join(c.changed_attrs)}"
            lines.append(f"  {c.address}")
            lines.append(f"    {action_label}")
        lines.append("")

    if routine:
        lines.append(f"routine drift ({len(routine)}):")
        for c in routine:
            lines.append(f"  {c.address}  changed: {', '.join(c.changed_attrs) or '(no visible attrs)'}")

    return "\n".join(lines)


# ------------------------------------------------------------ demo

def build_demo_plan() -> dict:
    return {
        "resource_changes": [
            {
                "address": "aws_security_group_rule.web_ingress",
                "type": "aws_security_group_rule",
                "change": {
                    "actions": ["update"],
                    "before": {"cidr_blocks": ["10.0.0.0/16"], "from_port": 443, "to_port": 443},
                    "after": {"cidr_blocks": ["0.0.0.0/0"], "from_port": 443, "to_port": 443},
                },
            },
            {
                "address": "aws_instance.worker[2]",
                "type": "aws_instance",
                "change": {
                    "actions": ["delete"],
                    "before": {"instance_type": "t3.medium", "ami": "ami-0abc123"},
                    "after": {},
                },
            },
            {
                "address": "aws_s3_bucket.uploads",
                "type": "aws_s3_bucket",
                "change": {
                    "actions": ["update"],
                    "before": {"tags": {"env": "prod", "owner": "platform"}},
                    "after": {"tags": {"env": "prod"}},
                },
            },
            {
                "address": "aws_instance.api",
                "type": "aws_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"instance_type": "t3.small", "monitoring": False},
                    "after": {"instance_type": "t3.small", "monitoring": True},
                },
            },
            {
                "address": "aws_iam_role_policy.deploy",
                "type": "aws_iam_role_policy",
                "change": {
                    "actions": ["delete", "create"],
                    "before": {"policy": "old-policy-json"},
                    "after": {"policy": "new-policy-json"},
                },
            },
            {
                "address": "aws_ami.base",
                "type": "aws_ami",
                "change": {"actions": ["no-op"], "before": {}, "after": {}},
            },
        ]
    }


def demo() -> int:
    print("simulating `terraform show -json` output after a plan detected drift\n")
    plan = build_demo_plan()
    changes = parse_plan_json(plan)
    print(f"{len(changes)} resources with real changes (the no-op ami.base was correctly excluded)\n")

    print(format_report(changes))

    print(f"\n\nnote: the security group rule opened 443 to 0.0.0.0/0 (was 10.0.0.0/16) — that's the")
    print(f"kind of change a console click makes and Terraform never sees coming, and it's flagged")
    print(f"even though its 'action' is just 'update', because cidr_blocks-via-ingress is on the")
    print(f"sensitive-attribute list. The monitoring=True change on aws_instance.api is routine —")
    print(f"same severity of action, but 'monitoring' isn't a sensitive attribute, so it sorts")
    print(f"to the bottom instead of competing for attention with an open security group.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        return demo()

    try:
        plan = run_terraform_plan(args.dir)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("(try --demo to see the report format without a real terraform project)", file=sys.stderr)
        return 1

    changes = parse_plan_json(plan)
    print(format_report(changes))
    flagged = [c for c in changes if c.is_drift_worth_flagging]
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
