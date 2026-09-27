"""重复初始化：check-then-act 非原子导致的单例被初始化两次。

框架版：在「检查标志」与「执行初始化」之间插入切换点。
真实版：无切换点的同样逻辑，窗口极小，压测几乎观察不到。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racefw import explore, format_trace  # noqa: E402


# --------------------------------------------------------------------- #
# 框架场景
# --------------------------------------------------------------------- #
def scenario(sched):
    state = {"inited": False, "init_count": 0}

    def worker():
        if not state["inited"]:
            yield from sched.preempt("checked flag, before initialize()")
            state["init_count"] += 1
            state["inited"] = True

    sched.spawn(worker, name="worker-0")
    sched.spawn(worker, name="worker-1")
    sched.env["state"] = state


def bug_fn(sched):
    c = sched.env["state"]["init_count"]
    return f"double init: init_count={c} > 1" if c > 1 else None


def outcome_fn(sched):
    return f"init_count={sched.env['state']['init_count']}"


# --------------------------------------------------------------------- #
# 真实线程对照
# --------------------------------------------------------------------- #
def real_once():
    state = {"inited": False, "count": 0}

    def worker():
        if not state["inited"]:
            state["count"] += 1
            state["inited"] = True

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return state["count"]


def stress(runs=300):
    bad = sum(1 for _ in range(runs) if real_once() > 1)
    return bad, runs


if __name__ == "__main__":
    res = explore(scenario, runs=20, strategy="pct", bug_fn=bug_fn, outcome_fn=outcome_fn)
    print(res.report("double_init / framework(pct)"))
    if res.failures:
        seed, kind, s = res.failures[0]
        print(f"\n[bug reproduced] seed={seed} kind={kind}")
        print("[event trace]\n" + format_trace(s))
    bad, total = stress()
    print(f"\n[real-thread stress] double-init={bad}/{total} "
          f"(rate={bad / total:.2%})")
