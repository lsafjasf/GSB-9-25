"""Half-open recovery demo: fully scripted, reproducible output.

Walks one node through HEALTHY -> OPEN -> HALF_OPEN (probes) -> HEALTHY,
prints the exact selection timeline (proving the node is never picked
while out of rotation), and then verifies the advertised re-participation
upper bounds:

- SmoothWeightedRoundRobin: picked within ceil(T / w) select() calls.
- LeastConnections: picked on the next select() (bound = 1).
"""

from backend_selector import (
    HALF_OPEN,
    HEALTHY,
    OPEN,
    LeastConnections,
    SmoothWeightedRoundRobin,
)

COOLDOWN = 5.0
REQUIRED_SUCCESSES = 2


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def timeline():
    print("=" * 68)
    print("Half-open recovery timeline "
          f"(cooldown={COOLDOWN}, required_successes={REQUIRED_SUCCESSES})")
    print("=" * 68)
    clock = Clock()
    sel = SmoothWeightedRoundRobin(
        cooldown=COOLDOWN, required_successes=REQUIRED_SUCCESSES,
        clock=clock)
    sel.add_node("a", 10)
    sel.add_node("b", 1)

    picks_during_outage = []
    sel.drain("b")  # health check fails at t=0
    print(f"t={clock.t:>4}: health check of b FAILED -> OPEN "
          f"(cooldown_until={COOLDOWN:.0f})")
    for i in range(4):
        nid = sel.select()
        picks_during_outage.append(nid)
    print(f"t={clock.t:>4}: 4 production selects while b is OPEN: "
          f"{picks_during_outage}")
    assert "b" not in picks_during_outage, "OPEN node must never be selected"

    clock.advance(4.0)
    got = sel.select_probe()
    print(f"t={clock.t:>4}: probe during cooldown -> {got!r} (still OPEN)")
    assert got is None

    clock.advance(1.0)
    probe = sel.select_probe()
    print(f"t={clock.t:>4}: cooldown elapsed, probe -> {probe!r} "
          f"state={sel.state_of('b')}")
    second = sel.select_probe()
    print(f"t={clock.t:>4}: second probe while one is in flight -> "
          f"{second!r} (per-node gate)")
    assert second is None
    traffic = [sel.select() for _ in range(4)]
    print(f"t={clock.t:>4}: 4 production selects while b HALF_OPEN: "
          f"{traffic}")
    assert "b" not in traffic, "HALF_OPEN node never serves normal traffic"

    state = sel.report_probe("b", True)
    print(f"t={clock.t:>4}: probe #1 success -> state={state}")
    clock.advance(0.2)
    sel.select_probe()
    state = sel.report_probe("b", False)
    print(f"t={clock.t:>4}: probe #2 FAILURE -> state={state}, "
          "cooldown restarts")

    clock.advance(COOLDOWN)
    results = []
    for i in range(REQUIRED_SUCCESSES):
        node_id = sel.select_probe()
        results.append(sel.report_probe(node_id, True))
        if results[-1] != HEALTHY:
            clock.advance(0.2)
    print(f"t={clock.t:>4.1f}: {REQUIRED_SUCCESSES} consecutive probe "
          f"successes -> states={results}")
    assert sel.state_of("b") == HEALTHY

    bound = sel.recovery_bound("b")
    print(f"t={clock.t:>4.1f}: b fully recovered; recovery_bound(b) = "
          f"ceil(T/w) = ceil(11/1) = {bound}")
    seq = [sel.select() for _ in range(bound)]
    pos = seq.index("b") + 1
    print(f"t={clock.t:>4.1f}: next {bound} selects: {seq}")
    print(f"t={clock.t:>4.1f}: b reappears on selection #{pos} "
          f"(<= bound {bound})")
    assert pos <= bound
    return pos, bound


def bound_table():
    print()
    print("=" * 68)
    print("Re-participation upper bounds (measured from full recovery)")
    print("=" * 68)
    print(f"{'strategy':<28}{'weights':<16}{'bound':<10}{'observed'}")
    cases = [
        (SmoothWeightedRoundRobin, [("a", 10), ("b", 1)], "b"),
        (SmoothWeightedRoundRobin, [("a", 5), ("b", 3), ("c", 2)], "c"),
        (SmoothWeightedRoundRobin, [("a", 1), ("b", 1), ("c", 1)], "c"),
        (LeastConnections, [("a", 10), ("b", 1)], "b"),
        (LeastConnections, [("a", 2), ("b", 1), ("c", 3)], "c"),
    ]
    for cls, spec, victim in cases:
        sel = cls(clock=Clock())
        for nid, w in spec:
            sel.add_node(nid, w)
        # warm up, remove the victim, generate other traffic, then recover
        for _ in range(2 * sum(w for _, w in spec)):
            nid = sel.select()
            sel.release(nid)
        sel.drain(victim)
        for _ in range(9):
            nid = sel.select()
            sel.release(nid)
        sel.restore(victim)
        bound = sel.recovery_bound(victim)
        seq = []
        for i in range(bound):
            seq.append(sel.select())
        observed = seq.index(victim) + 1
        wstr = ":".join(str(w) for _, w in spec)
        print(f"{cls.__name__:<28}{wstr:<16}{bound:<10}{observed}")
        assert observed <= bound


def zero_weight_check():
    print()
    print("=" * 68)
    print("Weight-0 nodes never participate (even mid half-open cycle)")
    print("=" * 68)
    clock = Clock()
    sel = SmoothWeightedRoundRobin(
        cooldown=COOLDOWN, required_successes=1, clock=clock)
    sel.add_node("a", 1)
    sel.add_node("z", 1)
    sel.set_weight("z", 0)
    sel.drain("z")
    clock.advance(COOLDOWN)
    print(f"probe on drained weight-0 z -> {sel.select_probe()!r}")
    assert sel.select_probe() is None
    picks = {sel.select() for _ in range(20)}
    print(f"20 selects with z weight 0 -> only {sorted(picks)}")
    assert picks == {"a"}
    print("weight-0 node excluded from probes and normal selects: OK")


if __name__ == "__main__":
    pos, bound = timeline()
    bound_table()
    zero_weight_check()
    print()
    print("ALL DEMO ASSERTIONS PASSED "
          f"(observed re-participation {pos} <= bound {bound})")
