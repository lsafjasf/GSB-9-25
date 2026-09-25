"""selftest.py — feature_flags 的自测与基准（仅标准库，直接 python3 运行）。

覆盖：
  1. 优先级链（rule > env > default > fallback）
  2. 分桶稳定性 + 不同灰度比例下的实际命中分布
  3. 快照隔离 + 并发读取安全
  4. 冲突消解留痕、缺配置、0/100 比例、非法时间窗口
  5. 十万次求值耗时（冷缓存 vs 热缓存）
"""

import threading
import time
from datetime import datetime, timezone

from feature_flags import FlagStore, compile_config

BASE_CONFIG = {
    "salt": "selftest",
    "flags": {
        "new_checkout": {
            "default": False,
            "env": {"staging": True},
            "rules": [
                {"id": "gray-25", "value": True, "percentage": 25,
                 "window": {"start": "2026-01-01T00:00:00+00:00",
                            "end": "2027-01-01T00:00:00+00:00"}},
            ],
        },
        "conflict_flag": {
            "default": False,
            "rules": [
                {"id": "first-on", "value": True, "percentage": 100},
                {"id": "second-off", "value": False, "percentage": 100},
            ],
        },
        "bad_window": {
            "default": "d",
            "rules": [
                {"id": "broken", "value": "x", "percentage": 100,
                 "window": {"start": "2027-01-01T00:00:00+00:00",
                            "end": "2026-01-01T00:00:00+00:00"}},
            ],
        },
        "zero_pct": {"default": False,
                     "rules": [{"id": "r0", "value": True, "percentage": 0}]},
        "full_pct": {"default": False,
                     "rules": [{"id": "r100", "value": True, "percentage": 100}]},
    },
}

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc).timestamp()  # 窗口内

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# ------------------------------------------------------------ 1. 优先级链

def test_priority():
    store = FlagStore(BASE_CONFIG, env="prod")
    snap = store.snapshot(now=NOW)

    e1 = snap.evaluate("new_checkout", user_id="u-1")          # rule 层
    e2 = snap.evaluate("new_checkout", user_id=None)           # 无 user -> env/default
    store_staging = FlagStore(BASE_CONFIG, env="staging")
    e3 = store_staging.snapshot(now=NOW).evaluate("new_checkout", user_id=None)
    e4 = snap.evaluate("missing_flag", user_id="u-1", fallback="fb")

    check("rule 层命中且给出规则链", e1.layer == "rule" and any("gray-25" in s for s in e1.trace))
    check("无 user_id 时灰度不命中，回落 default",
          e2.layer == "default" and e2.value is False)
    check("env 覆盖优先于 default", e3.layer == "env" and e3.value is True)
    check("缺失开关返回 fallback 并留痕",
          e4.layer == "fallback" and e4.value == "fb"
          and any("未配置" in s for s in e4.trace))
    print("---- 规则链示例 ----")
    print(e1.explain())


# ------------------------------------------------------------ 2. 分桶

def test_bucket_distribution():
    store = FlagStore({"salt": "dist", "flags": {
        f"f{p}": {"default": False,
                  "rules": [{"id": "r", "value": True, "percentage": p}]}
        for p in (1, 10, 25, 50, 75, 90, 99)}}, env="prod")
    snap = store.snapshot(now=NOW)

    n = 100_000
    users = [f"user-{i}" for i in range(n)]
    print(f"\n---- 分桶分布（{n} 个用户，万分桶）----")
    print(f"{'目标比例':>8} {'实际命中':>10} {'偏差(pp)':>10}")
    max_dev = 0.0
    for p in (1, 10, 25, 50, 75, 90, 99):
        hits = sum(1 for u in users if snap.is_enabled(f"f{p}", u))
        actual = hits / n * 100
        dev = actual - p
        max_dev = max(max_dev, abs(dev))
        print(f"{p:>7.0f}% {actual:>9.2f}% {dev:>+9.2f}")
    check("各比例实际命中偏差 < 1 个百分点", max_dev < 1.0, f"max={max_dev:.2f}pp")

    # 稳定性：同一 user+flag 跨 store 实例、跨快照结果一致
    s2 = FlagStore({"salt": "dist", "flags": BASE_CONFIG["flags"]}, env="prod")
    b1 = [FlagStore({"salt": "dist", "flags": {"f": {"default": 0, "rules": [
        {"id": "r", "value": 1, "percentage": 37}]}}}, env="prod")
          .snapshot(now=NOW).is_enabled("f", f"u{i}") for i in range(500)]
    b2 = [FlagStore({"salt": "dist", "flags": {"f": {"default": 0, "rules": [
        {"id": "r", "value": 1, "percentage": 37}]}}}, env="prod")
          .snapshot(now=NOW).is_enabled("f", f"u{i}") for i in range(500)]
    check("分桶稳定：跨实例 500 个用户结果完全一致", b1 == b2)


# ------------------------------------------------------------ 3. 快照隔离与并发

def test_snapshot_isolation():
    store = FlagStore({"salt": "s", "flags": {"f": {"default": "old"}}}, env="prod")
    snap_old = store.snapshot(now=NOW)
    store.update({"salt": "s", "flags": {"f": {"default": "new"}}})
    snap_new = store.snapshot(now=NOW)
    ok = (snap_old.evaluate("f").value == "old"
          and snap_new.evaluate("f").value == "new"
          and snap_old.evaluate("f").value == "old")  # 再次确认旧快照不变
    check("快照隔离：update 后旧快照仍看到旧配置", ok)

    # 并发：8 线程读快照 + 1 线程高频 update，读到的值必须属于某个已发布版本
    store2 = FlagStore({"salt": "s", "flags": {"f": {"default": "v0"}}}, env="prod")
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            snap = store2.snapshot()
            v1 = snap.evaluate("f").value
            v2 = snap.evaluate("f").value
            if v1 != v2 or not (v1.startswith("v") and v1[1:].isdigit()):
                errors.append((v1, v2))

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            store2.update({"salt": "s", "flags": {"f": {"default": f"v{i % 50}"}}})

    threads = ([threading.Thread(target=reader) for _ in range(8)]
               + [threading.Thread(target=writer)])
    for t in threads: t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads: t.join()
    check("并发安全：8 读 + 1 写 1s 内无不一致读取", not errors,
          f"errors={len(errors)}")


# ------------------------------------------------------------ 4. 冲突与边界

def test_conflicts_and_edges():
    store = FlagStore(BASE_CONFIG, env="prod")
    snap = store.snapshot(now=NOW)

    e = snap.evaluate("conflict_flag", user_id="u-9")
    check("同优先级冲突：先定义者胜且留痕",
          e.value is True and len(e.conflicts) == 1
          and "first-on" in e.conflicts[0] and "second-off" in e.conflicts[0])

    e = snap.evaluate("bad_window", user_id="u-9")
    check("非法时间窗口：规则永不生效并留痕",
          e.layer == "default" and e.value == "d"
          and any("时间窗口非法" in s for s in e.trace)
          and any("bad_window" in w for w in snap.compile_warnings))

    check("percentage=0 永不命中",
          snap.evaluate("zero_pct", user_id="u-1").value is False)
    check("percentage=100 恒命中（无需 user_id）",
          snap.evaluate("full_pct").value is True)

    try:
        compile_config({"flags": {"f": {"default": 0, "rules": [
            {"id": "r", "value": 1, "percentage": 150}]}}})
        check("percentage 越界抛 ValueError", False)
    except ValueError:
        check("percentage 越界抛 ValueError", True)

    # 时间窗口边界：快照固定 now，窗口结束后同一快照结果不变
    cfg = {"salt": "s", "flags": {"timed": {
        "default": False,
        "rules": [{"id": "w", "value": True, "percentage": 100,
                   "window": {"start": "2026-09-25T00:00:00+00:00",
                              "end": "2026-09-25T13:00:00+00:00"}}]}}}
    store2 = FlagStore(cfg, env="prod")
    snap_in = store2.snapshot(now=NOW)          # 12:00 窗口内
    snap_out = store2.snapshot(now=NOW + 7200)  # 14:00 窗口外
    check("时间窗口生效：窗口内 True / 窗口外 False",
          snap_in.is_enabled("timed") and not snap_out.is_enabled("timed"))


# ------------------------------------------------------------ 5. 十万次求值基准

def test_benchmark():
    flags = {}
    for i in range(20):
        flags[f"bench_{i}"] = {
            "default": False,
            "env": {"staging": True},
            "rules": [
                {"id": "gray", "value": True, "percentage": 30,
                 "window": {"start": "2026-01-01T00:00:00+00:00",
                            "end": "2027-01-01T00:00:00+00:00"}},
                {"id": "always", "value": False, "percentage": 100},
            ],
        }
    store = FlagStore({"salt": "bench", "flags": flags}, env="prod")
    snap = store.snapshot(now=NOW)

    n = 100_000
    users = [f"user-{i % 10_000}" for i in range(n)]  # 1 万用户 x 10 次
    keys = [f"bench_{i % 20}" for i in range(n)]

    t0 = time.perf_counter()
    for k, u in zip(keys, users):
        snap.evaluate(k, u)
    cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k, u in zip(keys, users):
        snap.evaluate(k, u)
    warm = time.perf_counter() - t0

    print(f"\n---- 十万次求值耗时（20 个开关 x 1 万用户）----")
    print(f"冷缓存（首次，含 sha1 分桶）: {cold:.3f}s  {cold / n * 1e6:.2f}us/次")
    print(f"热缓存（分桶命中缓存）      : {warm:.3f}s  {warm / n * 1e6:.2f}us/次")
    check("十万次求值完成且热缓存不慢于冷缓存", warm <= cold * 1.05)


if __name__ == "__main__":
    test_priority()
    test_bucket_distribution()
    test_snapshot_isolation()
    test_conflicts_and_edges()
    test_benchmark()
    failed = [n for n, ok in _results if not ok]
    print(f"\n==== {len(_results) - len(failed)}/{len(_results)} 项通过 ====")
    raise SystemExit(1 if failed else 0)
