"""racefw.explore — 调度搜索与覆盖统计。

对同一场景用不同种子反复运行，统计：
- 探索了多少种不同交错（按调度决策序列去重）；
- 产生了多少种不同结果；
- 哪些种子触发了缺陷（附完整事件序列，可直接复现）。
"""
import collections

from .core import DeadlockError, Scheduler


class ExploreResult:
    def __init__(self):
        self.runs = 0
        self.outcomes = collections.Counter()   # 结果 -> 次数
        self.schedule_sigs = set()              # 不同交错（决策序列）
        self.trace_hashes = set()               # 不同事件轨迹
        self.failures = []                      # [(seed, kind, scheduler)]

    @property
    def distinct_interleavings(self):
        return len(self.schedule_sigs)

    def report(self, label=""):
        lines = []
        head = f"[coverage] {label}" if label else "[coverage]"
        lines.append(f"{head} runs={self.runs} "
                     f"distinct_interleavings={self.distinct_interleavings} "
                     f"distinct_traces={len(self.trace_hashes)} "
                     f"distinct_outcomes={len(self.outcomes)}")
        for outcome, cnt in self.outcomes.most_common():
            lines.append(f"    outcome {outcome}: {cnt}x")
        if self.failures:
            seeds = [seed for seed, _, _ in self.failures[:8]]
            lines.append(f"    failures: {len(self.failures)}x "
                         f"(first seeds: {seeds})")
        else:
            lines.append("    failures: 0")
        return "\n".join(lines)


def _default_outcome(sched):
    return tuple(sorted((k, repr(v)) for k, v in sched.env.items()))


def explore(scenario, runs=100, strategy="pct", seed0=1, bug_fn=None,
            outcome_fn=None, keep_traces=True, **sched_kw):
    """用 seeds [seed0, seed0+runs) 探索场景。

    scenario(sched): 搭建场景（spawn 线程、设置 sched.env）
    bug_fn(sched) -> str | None: 运行结束后判定缺陷，返回描述或 None
    outcome_fn(sched) -> hashable: 结果归类（默认取 sched.env 的 repr）
    """
    res = ExploreResult()
    for i in range(runs):
        seed = seed0 + i
        s = Scheduler(seed=seed, strategy=strategy, **sched_kw)
        scenario(s)
        failure = None
        try:
            s.run()
        except DeadlockError:
            failure = "deadlock"
        if failure is None and bug_fn is not None:
            failure = bug_fn(s)
        outcome = outcome_fn(s) if outcome_fn else _default_outcome(s)
        res.runs += 1
        res.outcomes[outcome] += 1
        res.schedule_sigs.add(s.schedule_signature)
        if keep_traces:
            res.trace_hashes.add(hash(tuple(
                (e.tid, e.kind, e.resource, e.location) for e in s.events)))
        if failure:
            res.failures.append((seed, failure, s))
    return res
