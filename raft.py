#!/usr/bin/env python3
"""Raft leader election, simulated: terms, votes, heartbeats, and partitions you cause.

    raft.py --demo
    raft.py --nodes 5 --ticks 400 --partition 120:200 --seed 3

Only the election half of Raft (no log replication), which is where the subtle
parts live: randomised election timeouts break symmetry so split votes resolve,
a candidate needs a strict majority so two leaders cannot coexist in one term,
and a node that sees a higher term steps down immediately — that rule alone is
what makes a healed partition converge.
"""
from __future__ import annotations

import argparse
import random
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass
class Message:
    kind: str
    sender: int
    target: int
    term: int
    granted: bool = False
    deliver_at: int = 0


@dataclass
class Node:
    id: int
    role: Role = Role.FOLLOWER
    term: int = 0
    voted_for: int | None = None
    votes: set = field(default_factory=set)
    timeout: int = 0
    log: list = field(default_factory=list)


class Cluster:
    def __init__(self, size: int, seed: int = 0, min_timeout: int = 15, max_timeout: int = 30,
                 heartbeat: int = 5, latency: tuple[int, int] = (1, 3), drop_rate: float = 0.0):
        self.rng = random.Random(seed)
        self.nodes = [Node(i) for i in range(size)]
        self.min_timeout, self.max_timeout = min_timeout, max_timeout
        self.heartbeat, self.latency, self.drop_rate = heartbeat, latency, drop_rate
        self.queue: list[Message] = []
        self.tick = 0
        self.partitions: list[set[int]] = []
        self.events: list[str] = []
        self.leaders_per_term: dict[int, set[int]] = defaultdict(set)
        for node in self.nodes:
            node.timeout = self.rng.randint(min_timeout, max_timeout)

    def reachable(self, a: int, b: int) -> bool:
        if not self.partitions:
            return True
        for side in self.partitions:
            if (a in side) != (b in side):
                return False
        return True

    def send(self, msg: Message) -> None:
        if not self.reachable(msg.sender, msg.target) or self.rng.random() < self.drop_rate:
            return
        msg.deliver_at = self.tick + self.rng.randint(*self.latency)
        self.queue.append(msg)

    def start_election(self, node: Node) -> None:
        node.role = Role.CANDIDATE
        node.term += 1
        node.voted_for = node.id
        node.votes = {node.id}
        node.timeout = self.rng.randint(self.min_timeout, self.max_timeout)
        self.events.append(f"t{self.tick:>4}  node {node.id} -> candidate, term {node.term}")
        for other in self.nodes:
            if other.id != node.id:
                self.send(Message("vote_request", node.id, other.id, node.term))

    def become_leader(self, node: Node) -> None:
        node.role = Role.LEADER
        self.leaders_per_term[node.term].add(node.id)
        self.events.append(f"t{self.tick:>4}  node {node.id} elected LEADER of term {node.term} "
                           f"with {len(node.votes)}/{len(self.nodes)} votes")
        self.broadcast_heartbeat(node)

    def broadcast_heartbeat(self, node: Node) -> None:
        for other in self.nodes:
            if other.id != node.id:
                self.send(Message("heartbeat", node.id, other.id, node.term))

    def step_down(self, node: Node, term: int) -> None:
        if node.role != Role.FOLLOWER:
            self.events.append(f"t{self.tick:>4}  node {node.id} steps down (saw term {term} > {node.term})")
        node.role = Role.FOLLOWER
        node.term = term
        node.voted_for = None
        node.votes.clear()
        node.timeout = self.rng.randint(self.min_timeout, self.max_timeout)

    def handle(self, msg: Message) -> None:
        node = self.nodes[msg.target]
        if msg.term > node.term:                     # the rule that makes everything converge
            self.step_down(node, msg.term)
        if msg.kind == "vote_request":
            granted = (msg.term == node.term and node.voted_for in (None, msg.sender))
            if granted:
                node.voted_for = msg.sender
                node.timeout = self.rng.randint(self.min_timeout, self.max_timeout)
            self.send(Message("vote_reply", node.id, msg.sender, node.term, granted))
        elif msg.kind == "vote_reply":
            if node.role == Role.CANDIDATE and msg.term == node.term and msg.granted:
                node.votes.add(msg.sender)
                if len(node.votes) > len(self.nodes) // 2:     # strict majority: only one can win
                    self.become_leader(node)
        elif msg.kind == "heartbeat":
            if msg.term >= node.term:
                node.role = Role.FOLLOWER
                node.term = msg.term
                node.timeout = self.rng.randint(self.min_timeout, self.max_timeout)

    def step(self) -> None:
        self.tick += 1
        due = [m for m in self.queue if m.deliver_at <= self.tick]
        self.queue = [m for m in self.queue if m.deliver_at > self.tick]
        for msg in due:
            self.handle(msg)
        for node in self.nodes:
            if node.role == Role.LEADER:
                if self.tick % self.heartbeat == 0:
                    self.broadcast_heartbeat(node)
                continue
            node.timeout -= 1
            if node.timeout <= 0:
                self.start_election(node)

    def leaders(self) -> list[Node]:
        return [n for n in self.nodes if n.role == Role.LEADER]

    def snapshot(self) -> str:
        return "  ".join(f"{n.id}:{n.role.value[0].upper()}{n.term}" for n in self.nodes)


def run(cluster: Cluster, ticks: int, partition: tuple[int, int] | None, side: set[int] | None) -> None:
    for _ in range(ticks):
        if partition and cluster.tick == partition[0]:
            cluster.partitions = [side]
            cluster.events.append(f"t{cluster.tick:>4}  PARTITION: {sorted(side)} | "
                                  f"{sorted(set(n.id for n in cluster.nodes) - side)}")
        if partition and cluster.tick == partition[1]:
            cluster.partitions = []
            cluster.events.append(f"t{cluster.tick:>4}  partition healed")
        cluster.step()


def demo() -> int:
    print("five nodes, no failures — one leader emerges from a cold start\n")
    cluster = Cluster(5, seed=7)
    run(cluster, 120, None, None)
    for line in cluster.events[:8]:
        print(f"  {line}")
    print(f"\n  final state: {cluster.snapshot()}")
    print(f"  leaders now: {[n.id for n in cluster.leaders()]}")

    print("\nsplit vote: identical timeouts make an election fail, then jitter resolves it")
    tight = Cluster(4, seed=2, min_timeout=20, max_timeout=20)      # no jitter at all
    run(tight, 60, None, None)
    elections = sum(1 for e in tight.events if "candidate" in e)
    print(f"  with zero jitter: {elections} elections in 60 ticks, "
          f"leaders now {[n.id for n in tight.leaders()]}")
    loose = Cluster(4, seed=2, min_timeout=15, max_timeout=30)
    run(loose, 60, None, None)
    print(f"  with jitter:      {sum(1 for e in loose.events if 'candidate' in e)} elections, "
          f"leaders now {[n.id for n in loose.leaders()]}")

    print("\npartition: the majority side keeps a leader, the minority cannot elect one")
    split = Cluster(5, seed=5)
    run(split, 300, (100, 200), {0, 1})
    for line in [e for e in split.events if "PARTITION" in e or "healed" in e or "LEADER" in e][:10]:
        print(f"  {line}")
    print(f"\n  after healing: {split.snapshot()}")
    print(f"  leaders: {[n.id for n in split.leaders()]}")

    print("\nsafety check across 200 randomly seeded runs")
    violations = 0
    terms_checked = 0
    for seed in range(200):
        c = Cluster(5, seed=seed, drop_rate=0.05)
        run(c, 200, (60, 130), {0, 1})
        for term, leaders in c.leaders_per_term.items():
            terms_checked += 1
            if len(leaders) > 1:
                violations += 1
    print(f"  {terms_checked} terms elected a leader; terms with two leaders: {violations}")
    print("  (a strict majority makes two leaders in one term impossible — that is Raft's core claim)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=5)
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--drop-rate", type=float, default=0.0)
    ap.add_argument("--partition", default=None, metavar="START:END")
    ap.add_argument("--side", default="0,1", help="node ids on one side of the partition")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        return demo()
    window = None
    if args.partition:
        start, _, end = args.partition.partition(":")
        window = (int(start), int(end))
    cluster = Cluster(args.nodes, seed=args.seed, drop_rate=args.drop_rate)
    run(cluster, args.ticks, window, {int(x) for x in args.side.split(",")})
    for line in cluster.events:
        print(f"  {line}")
    print(f"\n  final: {cluster.snapshot()}")
    print(f"  terms with more than one leader: "
          f"{[t for t, ls in cluster.leaders_per_term.items() if len(ls) > 1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
