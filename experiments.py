"""Experiments: distribution quality vs vnode count, and migration ratios.

Run:  python3 experiments.py
"""

import math

from consistent_hash import ConsistentHash

KEYS = [f"key-{i}" for i in range(100_000)]


def shares(ch):
    counts = {n: 0 for n in ch.nodes}
    for k in KEYS:
        counts[ch.get_node(k)] += 1
    total = len(KEYS)
    return {n: c / total for n, c in counts.items()}


def stats(share_map, weights):
    total_w = sum(weights.values())
    deviations = [abs(share_map[n] - weights[n] / total_w) for n in share_map]
    mean_sq = sum(d * d for d in deviations) / len(deviations)
    return max(deviations), math.sqrt(mean_sq)


def experiment_distribution():
    print("=" * 72)
    print("Experiment 1: vnode count vs distribution deviation")
    print("10 equal-weight nodes, 100k keys, ideal share = 10.00% each")
    print("=" * 72)
    print(f"{'vnodes/node':>12} {'ring pts':>9} {'max dev':>9} {'std dev':>9}  per-node shares (%)")
    for base in [10, 25, 50, 100, 160, 320, 640]:
        ch = ConsistentHash(base_vnodes=base)
        for i in range(10):
            ch.add_node(f"n{i}")
        s = shares(ch)
        max_dev, std = stats(s, {n: 1.0 for n in ch.nodes})
        share_str = " ".join(f"{s[f'n{i}'] * 100:5.2f}" for i in range(10))
        print(f"{base:>12} {ch.ring_size:>9} {max_dev * 100:>8.2f}% {std * 100:>8.2f}%  {share_str}")
    print()


def experiment_weighted_distribution():
    print("=" * 72)
    print("Experiment 2: weighted nodes (base_vnodes=160), 100k keys")
    print("=" * 72)
    weights = {"small": 0.5, "mid": 1.0, "big": 2.0, "huge": 4.0}
    ch = ConsistentHash(base_vnodes=160)
    for n, w in weights.items():
        ch.add_node(n, weight=w)
    s = shares(ch)
    total_w = sum(weights.values())
    print(f"{'node':>6} {'weight':>6} {'ideal':>7} {'actual':>7} {'dev':>7}")
    for n in sorted(weights):
        ideal = weights[n] / total_w
        print(f"{n:>6} {weights[n]:>6.1f} {ideal * 100:>6.2f}% {s[n] * 100:>6.2f}% "
              f"{abs(s[n] - ideal) * 100:>6.2f}%")
    print()


def experiment_migration():
    print("=" * 72)
    print("Experiment 3: migration ratio on add/remove (base_vnodes=160, 100k keys)")
    print("=" * 72)

    def mapping(ch):
        return {k: ch.get_node(k) for k in KEYS}

    print("-- add one node to N equal nodes (theory: 1/(N+1) of keys move) --")
    print(f"{'N':>4} {'theory':>8} {'actual':>8} {'ratio':>6} {'foreign moves':>14}")
    for n in [4, 8, 16, 32]:
        ch = ConsistentHash(base_vnodes=160)
        for i in range(n):
            ch.add_node(f"n{i}")
        before = mapping(ch)
        ch.add_node("new")
        after = mapping(ch)
        moved = sum(1 for k in KEYS if before[k] != after[k])
        foreign = sum(1 for k in KEYS if before[k] != after[k] and after[k] != "new")
        theory = 1 / (n + 1)
        actual = moved / len(KEYS)
        print(f"{n:>4} {theory * 100:>7.2f}% {actual * 100:>7.2f}% {actual / theory:>6.2f} {foreign:>14}")

    print()
    print("-- remove one node from N equal nodes (theory: 1/N of keys move) --")
    print(f"{'N':>4} {'theory':>8} {'actual':>8} {'ratio':>6} {'foreign moves':>14}")
    for n in [5, 9, 17, 33]:
        ch = ConsistentHash(base_vnodes=160)
        for i in range(n):
            ch.add_node(f"n{i}")
        before = mapping(ch)
        ch.remove_node(f"n{n - 1}")
        after = mapping(ch)
        moved = sum(1 for k in KEYS if before[k] != after[k])
        foreign = sum(
            1 for k in KEYS
            if before[k] != after[k] and before[k] != f"n{n - 1}"
        )
        theory = 1 / n
        actual = moved / len(KEYS)
        print(f"{n:>4} {theory * 100:>7.2f}% {actual * 100:>7.2f}% {actual / theory:>6.2f} {foreign:>14}")
    print()


def experiment_weight_change():
    print("=" * 72)
    print("Experiment 4: weight change migration (8 nodes, base_vnodes=160)")
    print("theory: |new_share - old_share| of keys move, all to/from changed node")
    print("=" * 72)
    print(f"{'w_old':>6} {'w_new':>6} {'theory':>8} {'actual':>8} {'ratio':>6} {'foreign moves':>14}")
    for w_old, w_new in [(1.0, 2.0), (1.0, 1.5), (1.0, 0.5), (2.0, 1.0), (1.0, 4.0)]:
        ch = ConsistentHash(base_vnodes=160)
        ch.add_node("target", weight=w_old)
        for i in range(7):
            ch.add_node(f"n{i}")
        before = {k: ch.get_node(k) for k in KEYS}
        ch.set_weight("target", w_new)
        after = {k: ch.get_node(k) for k in KEYS}
        moved = sum(1 for k in KEYS if before[k] != after[k])
        foreign = sum(
            1 for k in KEYS
            if before[k] != after[k] and "target" not in (before[k], after[k])
        )
        old_share = w_old / (w_old + 7)
        new_share = w_new / (w_new + 7)
        theory = abs(new_share - old_share)
        actual = moved / len(KEYS)
        print(f"{w_old:>6.1f} {w_new:>6.1f} {theory * 100:>7.2f}% {actual * 100:>7.2f}% "
              f"{actual / theory:>6.2f} {foreign:>14}")
    print()


if __name__ == "__main__":
    experiment_distribution()
    experiment_weighted_distribution()
    experiment_migration()
    experiment_weight_change()
