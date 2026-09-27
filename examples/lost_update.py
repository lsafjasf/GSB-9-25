"""丢失更新（lost update）：counter += 1 被拆成 读 / 切换窗口 / 写。

框架版：在 read 与 write 之间插入切换点，PCT 调度少量种子即可命中。
真实版：同样的逻辑用 threading 跑，竞态窗口只有两条字节码，
        在 GIL 下极难被切换命中 —— 用于证明普通压测难以稳定复现。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racefw import Scheduler, explore, format_trace  # noqa: E402

N = 4                 # 每个线程累加次数
NTHREADS = 2
EXPECTED = N * NTHREADS


# --------------------------------------------------------------------- #
# 框架场景
# --------------------------------------------------------------------- #
def scenario(sched):
    box = {"value": 0}

    def worker():
        for _ in range(N):
            tmp = box["value"]
            yield from sched.preempt("window: read done, write pending")
            box["value"] = tmp + 1

    for i in range(NTHREADS):
        sched.spawn(worker, name=f"worker-{i}")
    sched.env["box"] = box


def bug_fn(sched):
    v = sched.env["box"]["value"]
    return f"lost update: counter={v} < expected={EXPECTED}" if v != EXPECTED else None


def outcome_fn(sched):
    return f"counter={sched.env['box']['value']}"


# --------------------------------------------------------------------- #
# 真实线程对照（同样的读-改-写逻辑，无显式让出）
# --------------------------------------------------------------------- #
def real_once():
    box = {"value": 0}

    def worker():
        for _ in range(N):
            tmp = box["value"]
            box["value"] = tmp + 1

    ts = [threading.Thread(target=worker) for _ in range(NTHREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return box["value"]


def stress(runs=300):
    """普通压测：反复跑真实线程版本，统计丢更新次数。"""
    bad = sum(1 for _ in range(runs) if real_once() != EXPECTED)
    return bad, runs


if __name__ == "__main__":
    res = explore(scenario, runs=60, strategy="pct", bug_fn=bug_fn, outcome_fn=outcome_fn)
    print(res.report("lost_update / framework(pct)"))
    if res.failures:
        seed, kind, s = res.failures[0]
        print(f"\n[bug reproduced] seed={seed} kind={kind}")
        print("[event trace]\n" + format_trace(s))
    bad, total = stress()
    print(f"\n[real-thread stress] anomalies={bad}/{total} "
          f"(rate={bad / total:.2%})")
