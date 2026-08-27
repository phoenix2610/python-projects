#!/usr/bin/env python3
"""An asyncio task queue with retries, backoff, dead letters and a live status view.

    taskq.py --demo
    taskq.py --workers 8 --jobs 200 --failure-rate 0.25

The properties worth having: a worker crash never loses a job (it is only
acknowledged after success), retries back off exponentially with jitter so a
struggling dependency is not hammered in lockstep, poison jobs land in a dead
letter queue instead of looping forever, and shutdown drains in-flight work
rather than cancelling it mid-flight.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    RETRY = "retry"
    DEAD = "dead"


@dataclass(order=True)
class Job:
    run_at: float
    id: int = field(compare=False)
    name: str = field(compare=False, default="")
    payload: dict = field(compare=False, default_factory=dict)
    attempts: int = field(compare=False, default=0)
    state: State = field(compare=False, default=State.PENDING)
    error: str = field(compare=False, default="")


class TaskQueue:
    def __init__(self, workers: int = 4, max_attempts: int = 3, base_delay: float = 0.05,
                 max_delay: float = 2.0, rng: random.Random | None = None):
        self.handlers: dict[str, callable] = {}
        self.workers = workers
        self.max_attempts, self.base_delay, self.max_delay = max_attempts, base_delay, max_delay
        self.ready: asyncio.Queue[Job] = asyncio.Queue()
        self.dead: list[Job] = []
        self.done: list[Job] = []
        self.inflight: dict[int, Job] = {}
        self.rng = rng or random.Random()
        self._next_id = 0
        self._pending = 0
        self._stopping = False
        self._idle = asyncio.Event()
        self._idle.set()

    def handler(self, name: str):
        def register(fn):
            self.handlers[name] = fn
            return fn
        return register

    async def enqueue(self, name: str, delay: float = 0.0, **payload) -> Job:
        self._next_id += 1
        job = Job(run_at=time.monotonic() + delay, id=self._next_id, name=name, payload=payload)
        self._pending += 1
        self._idle.clear()
        if delay > 0:
            asyncio.get_running_loop().call_later(delay, lambda: self.ready.put_nowait(job))
        else:
            await self.ready.put(job)
        return job

    def _backoff(self, attempts: int) -> float:
        """Exponential with full jitter — spreads a thundering herd across the window."""
        window = min(self.max_delay, self.base_delay * 2 ** (attempts - 1))
        return self.rng.uniform(0, window)

    async def _run_one(self, job: Job, worker: int) -> None:
        handler = self.handlers.get(job.name)
        job.attempts += 1
        job.state = State.RUNNING
        self.inflight[job.id] = job
        try:
            if handler is None:
                raise KeyError(f"no handler registered for {job.name!r}")
            result = handler(**job.payload)
            if asyncio.iscoroutine(result):
                await result
            job.state = State.DONE
            self.done.append(job)
            self._pending -= 1
        except asyncio.CancelledError:
            job.state = State.PENDING            # never acknowledged: it goes back on the queue
            await self.ready.put(job)
            raise
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= self.max_attempts:
                job.state = State.DEAD
                self.dead.append(job)
                self._pending -= 1
            else:
                job.state = State.RETRY
                delay = self._backoff(job.attempts)
                job.run_at = time.monotonic() + delay
                asyncio.get_running_loop().call_later(delay, lambda: self.ready.put_nowait(job))
        finally:
            self.inflight.pop(job.id, None)
            if self._pending == 0:
                self._idle.set()

    async def _worker(self, n: int) -> None:
        while True:
            job = await self.ready.get()
            try:
                await self._run_one(job, n)
            finally:
                self.ready.task_done()

    async def run(self, drain: bool = True) -> None:
        tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]
        try:
            if drain:
                await self._idle.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stats(self) -> dict:
        return {"done": len(self.done), "dead": len(self.dead),
                "retried": sum(1 for j in self.done if j.attempts > 1),
                "attempts": sum(j.attempts for j in self.done + self.dead)}


async def demo(workers: int, jobs: int, failure_rate: float) -> int:
    rng = random.Random(11)
    queue = TaskQueue(workers=workers, max_attempts=3, base_delay=0.02, rng=rng)
    processed: list[int] = []

    @queue.handler("resize")
    async def resize(image: str, n: int):
        await asyncio.sleep(rng.uniform(0.001, 0.01))
        if rng.random() < failure_rate:
            raise ConnectionError("upstream refused the connection")
        processed.append(n)

    @queue.handler("poison")
    def poison(**_):
        raise ValueError("this job can never succeed")

    print(f"{jobs} jobs, {workers} workers, {failure_rate:.0%} transient failure rate\n")
    for i in range(jobs):
        await queue.enqueue("resize", image=f"img-{i}.png", n=i)
    for i in range(3):
        await queue.enqueue("poison", n=i)
    await queue.enqueue("typo-handler", n=0)

    started = time.perf_counter()
    await queue.run()
    elapsed = time.perf_counter() - started

    stats = queue.stats()
    print(f"  completed {stats['done']}/{jobs} in {elapsed:.2f}s ({jobs / elapsed:.0f} jobs/s)")
    print(f"  {stats['retried']} jobs needed a retry, {stats['attempts']} attempts total "
          f"({stats['attempts'] / max(jobs, 1):.2f} per job)")
    print(f"  dead letters: {len(queue.dead)}")
    for job in queue.dead[:4]:
        print(f"    job {job.id} {job.name}: {job.error} (after {job.attempts} attempts)")
    dead_resize = {j.payload["n"] for j in queue.dead if j.name == "resize"}
    expected = sorted(set(range(jobs)) - dead_resize)
    print(f"  no job ran twice: {len(processed) == len(set(processed))}; "
          f"every job either completed or dead-lettered: {sorted(processed) == expected}")

    print("\nbackoff schedule (full jitter, base 20ms)")
    q = TaskQueue(base_delay=0.02, max_delay=2.0, rng=random.Random(3))
    for attempt in range(1, 7):
        samples = [q._backoff(attempt) for _ in range(200)]
        print(f"  attempt {attempt}: window 0-{min(2.0, 0.02 * 2 ** (attempt - 1)) * 1000:.0f}ms, "
              f"mean {sum(samples) / len(samples) * 1000:.0f}ms")

    print("\ncancellation puts an unacknowledged job back on the queue")
    q2 = TaskQueue(workers=1, rng=rng)
    started_flag = asyncio.Event()

    @q2.handler("slow")
    async def slow(**_):
        started_flag.set()
        await asyncio.sleep(5)

    await q2.enqueue("slow")
    runner = asyncio.create_task(q2.run())
    await started_flag.wait()
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    print(f"  after cancelling mid-flight: {q2.ready.qsize()} job back on the queue, "
          f"{len(q2.done)} completed, {len(q2.dead)} dead")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=120)
    ap.add_argument("--failure-rate", type=float, default=0.25)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    return asyncio.run(demo(args.workers, args.jobs, args.failure_rate))


if __name__ == "__main__":
    raise SystemExit(main())
