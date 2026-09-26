"""Reproducible simulations for AgingPriorityQueue.

Run ``python3 simulate.py`` to print load comparison data, a saturated
fixed-capacity starvation comparison, and upper-bound validation.
"""

from dataclasses import dataclass
import math
import random

from aging_queue import AgingPriorityQueue, DequeuedTask


@dataclass
class Stats:
    count: int = 0
    total_wait: int = 0
    max_wait: int = 0

    def add(self, result: DequeuedTask) -> None:
        self.count += 1
        self.total_wait += result.waited_dequeues
        self.max_wait = max(self.max_wait, result.waited_dequeues)

    @property
    def average(self) -> float:
        return self.total_wait / self.count if self.count else 0.0


def make_stats(priorities: tuple) -> dict:
    return {priority: Stats() for priority in priorities}


def record(stats: dict, result: DequeuedTask) -> None:
    stats[result.priority].add(result)


def poisson_count(rng: random.Random, rate: float) -> int:
    threshold = math.exp(-rate)
    count = 0
    product = 1.0
    while product > threshold:
        count += 1
        product *= rng.random()
    return count - 1


def stable_load_simulation(
    load: float, aging_period: int | None, horizon: int = 60000, seed: int = 700926
) -> dict:
    queue = AgingPriorityQueue(aging_period=aging_period)
    rng = random.Random(seed)
    stats = make_stats((0, 1, 2))
    population = [0] * 20 + [1] * 30 + [2] * 50

    for _ in range(horizon):
        arrivals = poisson_count(rng, load)
        for _ in range(arrivals):
            queue.enqueue("task", rng.choice(population))
        if not queue.empty():
            record(stats, queue.dequeue())

    while not queue.empty():
        record(stats, queue.dequeue())

    return stats


def saturated_fixed_size_simulation(
    aging_period: int | None, slots: int = 20000, capacity: int = 20, seed: int = 926700
) -> tuple:
    queue = AgingPriorityQueue(aging_period=aging_period)
    rng = random.Random(seed)
    stats = make_stats((0, 1, 2))
    bound_violations = []

    queue.enqueue("initial-high", 0)
    for index in range(capacity - 1):
        queue.enqueue(f"initial-low-{index}", 2)

    replacement_population = [0] * 90 + [1] * 5 + [2] * 5

    for _ in range(slots):
        if not queue.empty():
            result = queue.dequeue()
            record(stats, result)
            if aging_period:
                bound = result.priority * aging_period + capacity
                if result.waited_dequeues > bound:
                    bound_violations.append((result.sequence, result.waited_dequeues, bound))
            queue.enqueue("replacement", rng.choice(replacement_population))

    while not queue.empty():
        result = queue.dequeue()
        record(stats, result)
        if aging_period:
            bound = result.priority * aging_period + capacity
            if result.waited_dequeues > bound:
                bound_violations.append((result.sequence, result.waited_dequeues, bound))

    return stats, bound_violations


def print_stats_table(title: str, rows: list) -> None:
    print(title)
    print("load | mode | high avg/max | medium avg/max | low avg/max")
    print("-----|------|--------------|----------------|-------------")
    for load, label, stats in rows:
        load_text = f"{load:.2f}" if isinstance(load, float) else str(load)
        cells = []
        for priority in (0, 1, 2):
            item = stats[priority]
            cells.append(f"{item.average:8.2f}/{item.max_wait:<5d}")
        print(f"{load_text:>4} | {label:<5} | {cells[0]} | {cells[1]} | {cells[2]}")
    print()


def main() -> int:
    loads = (0.20, 0.50, 0.80, 0.90, 0.95)
    rows = []
    for load in loads:
        rows.append((load, "aging", stable_load_simulation(load, 32)))
        rows.append((load, "none", stable_load_simulation(load, None)))

    print_stats_table(
        "Stable load: priority 0 is high; wait counts the task's own dequeue.",
        rows,
    )

    aging_stats, violations = saturated_fixed_size_simulation(16)
    strict_stats, _ = saturated_fixed_size_simulation(None)
    print_stats_table(
        "Saturated fixed capacity N=20: aging_period=16, maximum static priority P=2.",
        [
            ("N20", "aging", aging_stats),
            ("N20", "none", strict_stats),
        ],
    )
    analytical_bound = 2 * 16 + 20
    print(f"Aging upper bound used by simulation: P*T + N = 2*16 + 20 = {analytical_bound}")
    print(f"Observed bound violations: {len(violations)}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
