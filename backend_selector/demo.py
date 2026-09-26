"""Distribution verification demo. Run: python3 demo.py"""

from backend_selector import LeastConnections, WeightedRoundRobin


def report(title, sel, weights, n):
    for _ in range(n):
        sel.select()
    total_w = sum(weights.values())
    stats = sel.stats()
    print(f"\n== {title} (n={n}) ==")
    print(f"{'node':<8}{'weight':>7}{'expected':>10}{'actual':>10}{'dev':>10}")
    max_dev = 0.0
    for nid in sorted(weights):
        expected = weights[nid] / total_w
        count, share = stats.get(nid, (0, 0.0))
        dev = abs(share - expected)
        max_dev = max(max_dev, dev)
        print(f"{nid:<8}{weights[nid]:>7}{expected:>10.4f}{share:>10.4f}{dev:>10.6f}")
    print(f"max deviation: {max_dev:.6f}")


def main():
    # 1) weighted round robin, mixed weights
    w = {"a": 5, "b": 3, "c": 2}
    s = WeightedRoundRobin()
    for nid, wt in w.items():
        s.add(nid, wt)
    report("WeightedRoundRobin 5:3:2", s, w, 100_000)

    # 2) extreme weight ratio
    w = {"heavy": 10000, "light": 1}
    s = WeightedRoundRobin()
    for nid, wt in w.items():
        s.add(nid, wt)
    report("WeightedRoundRobin 10000:1", s, w, 100_000)

    # 3) dynamic change: drain one node mid-stream, then restore
    w = {"a": 1, "b": 2, "c": 3}
    s = WeightedRoundRobin()
    for nid, wt in w.items():
        s.add(nid, wt)
    for _ in range(30_000):
        s.select()
    s.set_alive("c", False)
    for _ in range(30_000):
        s.select()
    s.set_alive("c", True)
    for _ in range(30_000):
        s.select()
    stats = s.stats()
    print("\n== Dynamic drain/restore (3 x 30000 picks) ==")
    for nid in ("a", "b", "c"):
        count, share = stats[nid]
        # expected: a,b get 1/6+1/3+1/6 of phases; c gets 3/6+0+3/6
        print(f"  {nid}: count={count} share={share:.4f}")

    # 4) least connections with held connections reflects in-flight
    s = LeastConnections()
    s.add("x", 1)
    s.add("y", 1)
    s.add("z", 2)
    picks = [s.select() for _ in range(16)]  # hold all open
    print("\n== LeastConnections weights 1:1:2, 16 held picks ==")
    print("  sequence:", " ".join(picks))
    print("  in_flight:", s.in_flight())


if __name__ == "__main__":
    main()
