"""racefw 自测：确定性、缺陷复现、覆盖统计、原语正确性、真实并发对照。

运行：python3 tests/selftest.py   （或 python3 -m tests.selftest）
"""
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racefw import (Condition, Lock, Scheduler, Semaphore,  # noqa: E402
                    explore, format_trace)
from racefw import DeadlockError  # noqa: E402
from examples import deadlock as ex_deadlock  # noqa: E402
from examples import double_init as ex_double_init  # noqa: E402
from examples import lost_update as ex_lost  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{PASS if cond else FAIL}] {name}" + (f"  -- {extra}" if extra else ""))


# --------------------------------------------------------------------- #
# 1. 确定性：同种子 + 同场景 => 相同交错、相同结论
# --------------------------------------------------------------------- #
def test_determinism():
    traces, outcomes = [], []
    for _ in range(2):
        s = Scheduler(seed=42, strategy="pct")
        ex_lost.scenario(s)
        s.run()
        traces.append(format_trace(s))
        outcomes.append(s.env["box"]["value"])
    ok = traces[0] == traces[1] and outcomes[0] == outcomes[1]
    check("determinism: same seed => same trace & outcome", ok,
          f"outcome={outcomes[0]}, events={len(traces[0].splitlines())}")
    s2 = Scheduler(seed=43, strategy="pct")
    ex_lost.scenario(s2)
    s2.run()
    check("determinism: different seed may differ (sanity)",
          format_trace(s2) != traces[0] or s2.env["box"]["value"] != outcomes[0])


# --------------------------------------------------------------------- #
# 2. 缺陷复现：丢失更新 / 死锁 / 重复初始化
# --------------------------------------------------------------------- #
def test_bug_reproduction():
    res_lost = explore(ex_lost.scenario, runs=60, strategy="pct",
                       pct_change_points=3, expected_steps=12,
                       bug_fn=ex_lost.bug_fn, outcome_fn=ex_lost.outcome_fn)
    check("reproduce: lost update found by framework", len(res_lost.failures) > 0,
          f"failures={len(res_lost.failures)}/{res_lost.runs}, "
          f"first_seed={res_lost.failures[0][0] if res_lost.failures else '-'}")
    print(res_lost.report("  lost_update"))

    res_dl = explore(ex_deadlock.scenario, runs=30, strategy="pct",
                     pct_change_points=2, expected_steps=10,
                     outcome_fn=ex_deadlock.outcome_fn)
    check("reproduce: deadlock found by framework", len(res_dl.failures) > 0,
          f"failures={len(res_dl.failures)}/{res_dl.runs}, "
          f"first_seed={res_dl.failures[0][0] if res_dl.failures else '-'}")
    print(res_dl.report("  deadlock"))

    res_di = explore(ex_double_init.scenario, runs=20, strategy="pct",
                     pct_change_points=2, expected_steps=6,
                     bug_fn=ex_double_init.bug_fn, outcome_fn=ex_double_init.outcome_fn)
    check("reproduce: double init found by framework", len(res_di.failures) > 0,
          f"failures={len(res_di.failures)}/{res_di.runs}, "
          f"first_seed={res_di.failures[0][0] if res_di.failures else '-'}")
    print(res_di.report("  double_init"))

    # 打印一条完整事件序列，证明可据此还原交错
    seed, kind, s = res_lost.failures[0]
    print(f"\n--- sample event trace (lost_update, seed={seed}, {kind}) ---")
    print(format_trace(s))
    print("--- end of trace ---\n")
    return res_lost


# --------------------------------------------------------------------- #
# 3. 覆盖统计：探索策略与交错计数
# --------------------------------------------------------------------- #
def test_coverage(res_lost):
    check("coverage: multiple distinct interleavings explored",
          res_lost.distinct_interleavings > 10,
          f"distinct={res_lost.distinct_interleavings}/{res_lost.runs} runs")
    res_rand = explore(ex_lost.scenario, runs=60, strategy="random",
                       bug_fn=ex_lost.bug_fn, outcome_fn=ex_lost.outcome_fn)
    print(res_rand.report("  lost_update / random strategy"))
    check("coverage: random strategy also finds the bug",
          len(res_rand.failures) > 0,
          f"failures={len(res_rand.failures)}/{res_rand.runs}")


# --------------------------------------------------------------------- #
# 4. 原语正确性：信号量限流 + 条件变量生产者-消费者，多种子下无缺陷
# --------------------------------------------------------------------- #
def test_primitives():
    def producer_consumer(sched):
        cap = 3
        items = []
        sem = Semaphore(sched, "slots", value=cap)
        mutex = Lock(sched, "mu")
        not_empty = Condition(sched, mutex, "not_empty")
        done = {"count": 0}
        total = 6

        def producer():
            for i in range(total // 2):
                yield from sem.acquire()
                yield from mutex.acquire()
                items.append(i)
                yield from not_empty.notify()
                yield from mutex.release()

        def consumer():
            while True:
                yield from mutex.acquire()
                while not items:
                    if done["count"] >= total:
                        yield from mutex.release()
                        return
                    yield from not_empty.wait()
                items.pop()
                done["count"] += 1
                yield from sem.release()
                yield from mutex.release()

        sched.spawn(producer, name="producer-0")
        sched.spawn(producer, name="producer-1")
        sched.spawn(consumer, name="consumer")
        sched.env["items"] = items
        sched.env["done"] = done
        sched.env["sem"] = sem

    bad = 0
    for seed in range(1, 41):
        s = Scheduler(seed=seed, strategy="pct")
        producer_consumer(s)
        try:
            s.run()
            if s.env["done"]["count"] != 6 or s.env["sem"].value != 3:
                bad += 1
        except Exception:
            bad += 1
    check("primitives: semaphore+condition producer/consumer correct over 40 seeds",
          bad == 0, f"bad={bad}/40")


# --------------------------------------------------------------------- #
# 5. 事件位置：切换点必须绑定场景文件的真实源码行
# --------------------------------------------------------------------- #
def test_event_locations():
    """断言 switch/finish/block/pct(降级) 的位置指向场景文件的具体行。"""
    this_file = os.path.basename(os.path.abspath(__file__))
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        scenario_lines = f.readlines()

    def blocking_scenario(sched):
        lock = Lock(sched, "L")

        def slow():
            yield from lock.acquire()
            yield from sched.preempt("holding the lock")
            yield from lock.release()

        def quick():
            yield from lock.acquire()
            yield from lock.release()

        sched.spawn(slow, name="slow")
        sched.spawn(quick, name="quick")

    loc_re = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+)$")
    kinds = ("switch", "finish", "block", "pct")
    seen = set()
    bad_locations = []
    block_lines_ok = True

    for seed in range(1, 81):
        s = Scheduler(seed=seed, strategy="pct",
                      pct_change_points=4, expected_steps=10)
        blocking_scenario(s)
        try:
            s.run()
        except DeadlockError:
            pass
        for ev in s.events:
            if ev.kind not in kinds:
                continue
            seen.add(ev.kind)
            m = loc_re.match(ev.location or "")
            if not m:
                bad_locations.append((ev.kind, ev.location))
                continue
            file, line = m.group("file"), int(m.group("line"))
            if file != this_file or not (1 <= line <= len(scenario_lines)):
                bad_locations.append((ev.kind, ev.location))
            if ev.kind == "block":
                # 阻塞点必须精确落在场景里发起阻塞调用的那一行
                block_lines_ok = block_lines_ok and (
                    "yield from lock.acquire()" in scenario_lines[line - 1])

    check("event-location: switch/finish/block/pct all observed in 80 seeds",
          seen == set(kinds), f"seen={sorted(seen)}")
    check("event-location: every event points at a concrete scenario line",
          not bad_locations, f"bad={bad_locations[:5]}")
    check("event-location: block points at the blocking acquire() line",
          block_lines_ok)


# --------------------------------------------------------------------- #
# 6. 真实并发对照：真实线程观察到的合法结果 ⊆ 框架结果集合
# --------------------------------------------------------------------- #
def test_real_vs_framework():
    res = explore(ex_lost.scenario, runs=200, strategy="pct",
                  pct_change_points=3, expected_steps=12,
                  bug_fn=ex_lost.bug_fn, outcome_fn=ex_lost.outcome_fn)
    fw_outcomes = set(res.outcomes)          # 如 {"counter=2", ..., "counter=8"}
    real_outcomes = set()
    R = 300
    for _ in range(R):
        real_outcomes.add(f"counter={ex_lost.real_once()}")
    missing = real_outcomes - fw_outcomes
    check("real-vs-framework: real outcomes subset of framework outcomes",
          not missing,
          f"real={sorted(real_outcomes)} framework={sorted(fw_outcomes)}")
    check("real-vs-framework: framework covers the full legal range 4..8",
          fw_outcomes == {f"counter={v}" for v in range(4, 9)},
          f"framework={sorted(fw_outcomes)}")

    # 普通压测 vs 框架：复现难度对比
    bad_lost, total_lost = ex_lost.stress(300)
    bad_dl, total_dl = ex_deadlock.stress(100)
    bad_di, total_di = ex_double_init.stress(300)
    print(f"[stress-vs-framework] lost_update: real stress anomalies "
          f"{bad_lost}/{total_lost} vs framework failures "
          f"{len(res.failures)}/{res.runs}")
    print(f"[stress-vs-framework] deadlock:    real stress deadlocks "
          f"{bad_dl}/{total_dl} (framework: deterministic, no hang)")
    print(f"[stress-vs-framework] double_init: real stress anomalies "
          f"{bad_di}/{total_di}")
    check("stress-vs-framework: framework reproduces far more reliably",
          len(res.failures) > bad_lost,
          f"framework={len(res.failures)} failures, real stress={bad_lost}")


if __name__ == "__main__":
    test_determinism()
    res_lost = test_bug_reproduction()
    test_coverage(res_lost)
    test_primitives()
    test_event_locations()
    test_real_vs_framework()
    failed = [n for n, ok in results if not ok]
    print(f"\n==== {len(results) - len(failed)}/{len(results)} checks passed ====")
    if failed:
        print("FAILED:", *failed, sep="\n  - ")
        sys.exit(1)
    print("ALL CHECKS PASSED")
