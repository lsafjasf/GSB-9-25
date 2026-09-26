"""Distribution validation demo: prints per-node counts, shares, and
deviation from the ideal weight-proportional share."""

from backend_selector import LeastConnections, SmoothWeightedRoundRobin


def report(title, selector, expected_shares, n):
    stats = selector.stats()
    print(f"\n== {title} (selections={n}) ==")
    print(f"{'node':<8}{'weight':>7}{'count':>8}{'share':>9}"
          f"{'expected':>9}{'deviation':>11}")
    max_dev = 0.0
    for nid, s in stats.items():
        exp = expected_shares[nid]
        dev = s["share"] - exp
        max_dev = max(max_dev, abs(dev))
        print(f"{nid:<8}{s['weight']:>7}{s['count']:>8}"
              f"{s['share']:>9.4f}{exp:>9.4f}{dev:>+11.6f}")
    print(f"max |deviation| = {max_dev:.6f}")


def wrr_case(spec, n, title):
    sel = SmoothWeightedRoundRobin()
    total_w = sum(w for _, w in spec)
    for nid, w in spec:
        sel.add_node(nid, w)
    for _ in range(n):
        sel.select()
    report(title, sel, {nid: w / total_w for nid, w in spec}, n)


def least_connections_case():
    # 3 nodes, weight 1; a "slow" request holds its connection while
    # others complete -> in-flight pressure shifts subsequent picks.
    sel = LeastConnections()
    for nid in ("a", "b", "c"):
        sel.add_node(nid, 1)
    held = []
    for i in range(300):
        nid = sel.select()
        if i % 10 == 0:          # every 10th request is slow: stays open
            held.append(nid)
        else:
            sel.release(nid)
    stats = sel.stats()
    print("\n== LeastConnections with in-flight pressure (selections=300) ==")
    print(f"{'node':<8}{'count':>8}{'share':>9}{'in-flight':>11}")
    for nid, s in stats.items():
        print(f"{nid:<8}{s['count']:>8}{s['share']:>9.4f}"
              f"{s['active_connections']:>11}")
    print("note: nodes holding more in-flight connections receive fewer")
    print("subsequent picks; counts stay near-uniform (100 each) because")
    print("the strategy actively rebalances away from loaded nodes.")


if __name__ == "__main__":
    wrr_case([("a", 5), ("b", 3), ("c", 2)], 10000,
             "SmoothWRR weights 5:3:2")
    wrr_case([("a", 7), ("b", 3), ("c", 1)], 11000,
             "SmoothWRR weights 7:3:1")
    wrr_case([("big", 1000), ("small", 1)], 10010,
             "SmoothWRR extreme weights 1000:1")
    wrr_case([("a", 5), ("b", 3), ("c", 2)], 999,
             "SmoothWRR 5:3:2, non-multiple round (999 picks)")
    least_connections_case()
