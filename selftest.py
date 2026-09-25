"""selftest.py — feature_flags 自测与基准（仅标准库，直接 python3 selftest.py 运行）。

覆盖：
  1. 优先级与规则链可解释性
  2. 分桶稳定性 + 不同灰度比例下的实际命中分布
  3. 一致性快照隔离 + 并发读取安全
  4. 规则冲突确定消解与留痕
  5. 边界行为：配置缺失 / 比例 0 与 100 / 非法时间窗口
  6. 十万次求值耗时基准
"""

import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from feature_flags import (
    BUCKET_COUNT, ConfigError, FlagStore, stable_bucket,
)

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def make_store(**flag_kwargs):
    store = FlagStore()
    store.load([flag_kwargs])
    return store


# ---------------------------------------------------------------------------
print("== 1. 优先级与规则链 ==")
# ---------------------------------------------------------------------------
store = FlagStore()
store.load([{
    "key": "checkout_v2",
    "default": False,
    "env_overrides": {"prod": True},
    "rollouts": [{"rule_id": "r1", "value": "grey", "percentage": 100}],
    "time_window": {
        "start": (NOW - timedelta(hours=1)).isoformat(),
        "end": (NOW + timedelta(hours=1)).isoformat(),
    },
}])
snap = store.snapshot()

r = snap.evaluate("checkout_v2", user_id="u1", env="prod", now=NOW)
check("env_override 优先于 rollout", r.value is True and r.reason == "env_override")
r = snap.evaluate("checkout_v2", user_id="u1", env="staging", now=NOW)
check("rollout 优先于 default", r.value == "grey" and r.reason == "rollout")
store2 = make_store(key="f", default="d")
r = store2.snapshot().evaluate("f", user_id="u1", env="x", now=NOW)
check("无命中时落到 default", r.value == "d" and r.reason == "default")

outside = NOW + timedelta(hours=2)
r = snap.evaluate("checkout_v2", user_id="u1", env="prod", now=outside)
check("时间窗口是最高优先级 gate", r.value is False and r.reason == "outside_time_window")
check("规则链完整留痕", [s.rule for s in r.trace] == ["time_window"])
r = snap.evaluate("checkout_v2", user_id="u1", env="staging", now=NOW)
check("规则链记录每一层判定", [s.rule for s in r.trace] == ["time_window", "env_override", "rollout"])
print("---- 规则链示例 ----")
print(r.explain())

# ---------------------------------------------------------------------------
print("== 2. 分桶稳定性与分布 ==")
# ---------------------------------------------------------------------------
b0 = stable_bucket("flag_a", "user_42")
check("同一用户同一开关分桶稳定", all(stable_bucket("flag_a", "user_42") == b0 for _ in range(1000)))
check("不同开关分桶相互独立（抽样不全等）",
      len({stable_bucket(k, "user_42") for k in ["f1", "f2", "f3", "f4", "f5"]}) > 1)

N = 100_000
users = [f"user_{i}" for i in range(N)]
buckets = [stable_bucket("dist_flag", u) for u in users]
print(f"---- 分布数据（{N} 用户，万分桶）----")
print(f"{'目标比例':>8} {'实际命中':>10} {'偏差':>9}")
for pct in (1, 10, 25, 50, 75, 90, 99, 100):
    hit = sum(1 for b in buckets if b < pct * (BUCKET_COUNT // 100)) / N * 100
    print(f"{pct:>7.0f}% {hit:>9.2f}% {hit - pct:>+8.2f}pp")
    check(f"比例 {pct}% 实际命中偏差 < 0.5pp", abs(hit - pct) < 0.5, f"actual={hit:.3f}")
# 均匀性：十个千分区间计数应接近 N/10
deciles = [sum(1 for b in buckets if d * 1000 <= b < (d + 1) * 1000) for d in range(10)]
check("十个千分区间均匀（每区偏差 < 3%）",
      all(abs(c - N / 10) < N / 10 * 0.03 for c in deciles), str(deciles))
print(f"  千分区间计数: {deciles}")

# ---------------------------------------------------------------------------
print("== 3. 一致性快照隔离与并发安全 ==")
# ---------------------------------------------------------------------------
store = FlagStore()
v1 = store.load([{"key": "f", "default": "old"}])
snap_old = store.snapshot()
v2 = store.load([{"key": "f", "default": "new"}])
snap_new = store.snapshot()
check("快照记录各自版本", snap_old.version == v1 and snap_new.version == v2 and v1 != v2)
check("配置更新后旧快照不受影响",
      snap_old.evaluate("f").value == "old" and snap_new.evaluate("f").value == "new")
check("同一快照内多次求值一致",
      len({snap_old.evaluate("f").value for _ in range(1000)}) == 1)

errors = []
def reader(snap, expected, stop):
    try:
        while not stop.is_set():
            r = snap.evaluate("f")
            assert r.value == expected and r.version == snap.version, (r.value, expected)
    except AssertionError as exc:
        errors.append(exc)

stop = threading.Event()
threads = []
for snap, expected in ((snap_old, "old"), (snap_new, "new")):
    for _ in range(4):
        t = threading.Thread(target=reader, args=(snap, expected, stop))
        t.start()
        threads.append(t)
for i in range(200):  # 读的同时高频更新配置
    store.load([{"key": "f", "default": f"v{i}"}])
stop.set()
for t in threads:
    t.join()
check("并发读取快照期间高频更新：无错乱、无异常", not errors, str(errors[:1]))

# ---------------------------------------------------------------------------
print("== 4. 规则冲突确定消解 ==")
# ---------------------------------------------------------------------------
conflict_cfg = {
    "key": "f",
    "default": "d",
    "rollouts": [
        {"rule_id": "rule_b", "value": "B", "percentage": 100, "priority": 5},
        {"rule_id": "rule_a", "value": "A", "percentage": 100, "priority": 5},
        {"rule_id": "rule_z", "value": "Z", "percentage": 100, "priority": 1},
    ],
}
snap = make_store(**conflict_cfg).snapshot()
r1 = snap.evaluate("f", user_id="u1", now=NOW)
r2 = snap.evaluate("f", user_id="u1", now=NOW)
check("同优先级相反结论 → 按 rule_id 字典序确定消解", r1.value == "A")
check("消解结果跨求值确定一致", r2.value == "A")
conflicts = [s for s in r1.trace if s.outcome == "conflict"]
check("冲突留痕（含胜出与被压制规则）",
      len(conflicts) == 1 and "rule_a" in conflicts[0].detail and "rule_b" in conflicts[0].detail)
check("高优先级压过低优先级", all("rule_z" not in s.detail or s.outcome != "hit" for s in r1.trace))
print("---- 冲突留痕示例 ----")
print(r1.explain())

# ---------------------------------------------------------------------------
print("== 5. 边界行为 ==")
# ---------------------------------------------------------------------------
snap = make_store(key="exists", default=1).snapshot()
r = snap.evaluate("missing_flag", fallback="fb")
check("配置缺失 → 返回 fallback 并留痕", r.value == "fb" and r.reason == "flag_not_found")

r = make_store(key="f", default="d",
               rollouts=[{"rule_id": "r", "value": "on", "percentage": 0}]).snapshot() \
    .evaluate("f", user_id="u1")
check("比例 0 → 永不命中", r.value == "d" and r.reason == "default")
r = make_store(key="f", default="d",
               rollouts=[{"rule_id": "r", "value": "on", "percentage": 100}]).snapshot() \
    .evaluate("f", user_id="anyone")
check("比例 100 → 必定命中", r.value == "on" and r.reason == "rollout")

r = make_store(key="f", default="d",
               time_window={"start": "2026-10-01T00:00:00+00:00",
                            "end": "2026-09-01T00:00:00+00:00"}).snapshot() \
    .evaluate("f", now=NOW)
check("非法窗口(start>end) → 默认值的明确行为", r.value == "d" and r.reason == "invalid_time_window")
r = make_store(key="f", default="d",
               time_window={"start": "not-a-date"}).snapshot().evaluate("f", now=NOW)
check("非法窗口(无法解析) → 默认值的明确行为", r.value == "d" and r.reason == "invalid_time_window")
try:
    make_store(key="f", default="d", rollouts=[{"rule_id": "r", "percentage": 150}])
    check("比例越界 → 加载期拒绝", False)
except ConfigError:
    check("比例越界 → 加载期拒绝", True)

# ---------------------------------------------------------------------------
print("== 6. 十万次求值基准 ==")
# ---------------------------------------------------------------------------
store = FlagStore()
store.load([{
    "key": "bench",
    "default": False,
    "env_overrides": {"prod": True},
    "rollouts": [{"rule_id": "r1", "value": "on", "percentage": 37.5}],
    "time_window": {"start": "2026-01-01T00:00:00+00:00", "end": "2027-01-01T00:00:00+00:00"},
}])
snap = store.snapshot()
bench_users = [f"user_{i}" for i in range(10_000)]  # 1 万用户循环，命中分桶缓存

def bench(label):
    t0 = time.perf_counter()
    for i in range(100_000):
        snap.evaluate("bench", user_id=bench_users[i % len(bench_users)],
                      env="staging", now=NOW)
    dt = time.perf_counter() - t0
    print(f"  {label}: 100,000 次求值耗时 {dt:.3f}s，"
          f"均值 {dt / 100_000 * 1e6:.2f}µs/次，{100_000 / dt:,.0f} 次/s")
    return dt

cold = bench("冷缓存(首次) ")
warm = bench("热缓存(第二次)")
print(f"  整体加速比: {cold / warm:.2f}x（求值含规则链构建，分桶仅占部分耗时）")

# 分桶微基准：隔离展示 lru_cache 的效果
from feature_flags import _cached_bucket
_cached_bucket.cache_clear()
t0 = time.perf_counter()
for i in range(100_000):  # 10 万个不同用户 → 全部缓存未命中
    stable_bucket("bench", f"uniq_{i}")
miss_t = time.perf_counter() - t0
t0 = time.perf_counter()
for i in range(100_000):  # 同样 10 万个用户再来一遍 → 全部命中缓存
    stable_bucket("bench", f"uniq_{i}")
hit_t = time.perf_counter() - t0
print(f"  分桶微基准: 未命中 {miss_t * 1e6 / 100_000:.2f}µs/次(SHA-256)，"
      f"命中 {hit_t * 1e6 / 100_000:.2f}µs/次(lru_cache)，加速 {miss_t / hit_t:.1f}x")

print(f"\n结果: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
