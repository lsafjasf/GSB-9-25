"""selftest.py — feature_flags 的自测与基准（仅标准库，直接 python3 运行）。

覆盖：
  1. 优先级链（rule > env > default > fallback）
  2. 分桶稳定性 + 不同灰度比例下的实际命中分布
  3. 快照隔离 + 并发读取安全
  4. 冲突消解留痕、缺配置、0/100 比例、非法时间窗口
  5. 十万次求值耗时（冷缓存 vs 热缓存）
  6. 规则集导出/导入（跨环境搬运，非法包被拒收）
  7. 两版本差异对比（新增/删除/参数变化逐条可断言）
  8. 时间阶梯灰度：档位推进、分桶稳定、放量集合单调、越界拒收
"""

import json
import threading
import time
from datetime import datetime, timezone

from feature_flags import (
    BUCKET_COUNT, FlagStore, compile_config, diff_configs,
    export_bundle, export_json, import_bundle,
)

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



# ------------------------------------------------------------ 6. 导入导出

ROLLOUT_CONFIG = {
    "salt": "rollout-demo",
    "flags": {
        "new_payment": {
            "default": False,
            "rules": [
                {"id": "staged", "value": True, "rollout": [
                    {"time": "2026-10-01T00:00:00+00:00", "percentage": 0},
                    {"time": "2026-10-02T00:00:00+00:00", "percentage": 10},
                    {"time": "2026-10-04T00:00:00+00:00", "percentage": 50},
                    {"time": "2026-10-08T00:00:00+00:00", "percentage": 100},
                ]},
            ],
        },
    },
}

T_STAGE0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0


def test_import_export():
    bundle = export_bundle(ROLLOUT_CONFIG, version="v1", source_env="staging")
    check("导出包带 format 与 meta",
          bundle["format"] == "feature-flags/ruleset/v1"
          and bundle["meta"]["version"] == "v1"
          and bundle["meta"]["source_env"] == "staging"
          and bundle["meta"]["flag_count"] == 1)

    # JSON 落盘 -> 另一环境读出导入（模拟跨环境搬运）
    payload = export_json(ROLLOUT_CONFIG, version="v1", source_env="staging")
    reloaded = json.loads(payload)
    imported, meta = import_bundle(reloaded)
    check("JSON 往返：导入配置与原配置完全一致", imported == ROLLOUT_CONFIG)
    check("导入 meta 保留版本与来源环境",
          meta.get("version") == "v1" and meta.get("source_env") == "staging")

    # 导入后可直接驱动生产 store，且求值结果与原配置一致
    prod_store = FlagStore(imported, env="prod")
    src_store = FlagStore(ROLLOUT_CONFIG, env="prod")
    same = all(
        prod_store.snapshot(now=T_STAGE0 + 2 * DAY).evaluate(
            "new_payment", f"u{i}").value
        == src_store.snapshot(now=T_STAGE0 + 2 * DAY).evaluate(
            "new_payment", f"u{i}").value
        for i in range(500))
    check("跨环境搬运后 500 用户求值结果一致", same)

    # 裸 config / JSON 字符串也可直接导入
    cfg2, _ = import_bundle(ROLLOUT_CONFIG)
    cfg3, _ = import_bundle(payload)
    check("导入接受裸 config 与 JSON 字符串",
          cfg2 == ROLLOUT_CONFIG and cfg3 == ROLLOUT_CONFIG)

    # 非法包被拒收
    rejects = [
        ('{"format":"feature-flags/ruleset/v9","config":{}}', "format 不支持"),
        ("not-json-at-all", "JSON 非法"),
        ({"format": "feature-flags/ruleset/v1", "config": []}, "config 非对象"),
        ({"flags": {"f": {"rules": [{"id": "r", "value": True,
          "rollout": [{"time": "2026-10-01T00:00:00Z",
                       "percentage": 150}]}]}}}, "阶梯比例越界"),
    ]
    for bad, label in rejects:
        try:
            import_bundle(bad)
            check(f"非法包被拒收（{label}）", False)
        except ValueError:
            check(f"非法包被拒收（{label}）", True)


# ------------------------------------------------------------ 7. 版本差异对比

V1 = {
    "salt": "app",
    "flags": {
        "new_checkout": {
            "default": False,
            "env": {"staging": True},
            "rules": [
                {"id": "gray", "value": True, "percentage": 10,
                 "window": {"start": "2026-09-01T00:00:00+00:00",
                            "end": "2026-12-01T00:00:00+00:00"}},
            ],
        },
        "old_search": {"default": True},
    },
}

V2 = {
    "salt": "app",
    "flags": {
        "new_checkout": {
            "default": True,
            "env": {"staging": True, "prod": False},
            "rules": [
                {"id": "gray", "value": True, "percentage": 50,
                 "window": {"start": "2026-09-01T00:00:00+00:00",
                            "end": "2026-12-01T00:00:00+00:00"}},
                {"id": "vip", "value": True, "percentage": 100},
            ],
        },
        "dark_mode": {"default": False},
    },
}


def test_version_diff():
    diff = diff_configs(V1, V2)
    print("\n---- 版本差异 v1 -> v2 ----")
    print(diff.report())

    added_flags = {e.flag for e in diff.added if e.kind == "flag"}
    added_rules = {(e.flag, e.rule) for e in diff.added if e.kind == "rule"}
    removed_flags = {e.flag for e in diff.removed if e.kind == "flag"}
    changes = {(c.flag, c.scope, c.param): (c.old, c.new)
               for c in diff.changed}

    check("新增 flag 逐条：dark_mode", added_flags == {"dark_mode"})
    check("新增规则逐条：vip", added_rules == {("new_checkout", "vip")})
    check("删除 flag 逐条：old_search", removed_flags == {"old_search"})
    check("参数变化：default False -> True",
          changes[("new_checkout", "flag", "default")] == (False, True))
    check("参数变化：灰度比例 10% -> 50%",
          changes[("new_checkout", "rule:gray", "percentage")] == (10, 50))
    check("参数变化：新增 env=prod 覆盖",
          changes[("new_checkout", "env:prod", "value")][1] is False)
    check("结论可断言：1 新增 flag + 1 新增规则 + 1 删除 flag + 3 参数变化",
          len(diff.added) == 2 and len(diff.removed) == 1
          and len(diff.changed) == 3 and diff.total == 6)

    # 对比方向敏感：v2 -> v1 的增删对调
    rev = diff_configs(V2, V1)
    check("反向对比：增删对调",
          {e.flag for e in rev.removed if e.kind == "flag"} == {"dark_mode"}
          and {e.flag for e in rev.added if e.kind == "flag"}
          == {"old_search"})

    # 无差异与确定性
    check("相同版本无差异", diff_configs(V1, V1).empty
          and diff_configs(V1, V1).total == 0)
    reports = {diff_configs(V1, V2).report() for _ in range(20)}
    check("结论确定：20 次对比报告完全一致", len(reports) == 1)

    # 阶梯差异也能逐条列出
    v3 = {"salt": "app", "flags": {"f": {"default": False, "rules": [
        {"id": "r", "value": True, "percentage": 10}]}}}
    v4 = {"salt": "app", "flags": {"f": {"default": False, "rules": [
        {"id": "r", "value": True, "rollout": [
            {"time": "2026-10-01T00:00:00Z", "percentage": 10},
            {"time": "2026-10-02T00:00:00Z", "percentage": 100}]}]}}}
    d2 = diff_configs(v3, v4)
    params = {(c.flag, c.param) for c in d2.changed}
    check("静态比例 -> 时间阶梯：percentage 与 rollout 两条参数变化",
          ("f", "percentage") in params and ("f", "rollout") in params)


# ------------------------------------------------------------ 8. 时间阶梯灰度

def test_rollout_ladder():
    store = FlagStore(ROLLOUT_CONFIG, env="prod")
    users = [f"user-{i}" for i in range(20_000)]
    print("\n---- 时间阶梯放量（20000 用户，万分桶）----")
    print(f"{'日期':>12} {'档位':>6} {'比例':>7} {'实际放量':>10} {'新增用户':>10}")

    plan_points = [
        (0, 0, "0%（未放量）"), (1, 10, "10%"), (3, 50, "50%"),
        (7, 100, "100% 全量"), (9, 100, "100% 全量（窗口后保持）"),
    ]
    prev_enabled: set = set()
    expected_pcts = {0: 0.0, 1: 10.0, 3: 50.0, 7: 100.0, 9: 100.0}
    max_dev = 0.0
    for day, pct, label in plan_points:
        snap = store.snapshot(now=T_STAGE0 + day * DAY)
        enabled = {u for u in users if snap.is_enabled("new_payment", u)}
        actual = len(enabled) / len(users) * 100
        added = len(enabled - prev_enabled)
        plan = snap.rollout_plan("new_payment")
        print(f"10-{1 + day:02d}    {plan.active_index:>6} "
              f"{(plan.active_percentage or 0):>6.0f}% "
              f"{actual:>9.2f}% {added:>10}")
        check(f"第 {label} 档生效（day={day}）",
              plan.active_percentage == expected_pcts[day]
              and (plan.started if day >= 0 else True))
        if 0 < pct < 100:
            max_dev = max(max_dev, abs(actual - pct))
        check(f"档位单调：上一档用户不被收回（day={day}）",
              prev_enabled <= enabled)
        prev_enabled = enabled
    check("中间档实际放量偏差 < 1 个百分点", max_dev < 1.0, f"max={max_dev:.2f}pp")
    check("最终档全量；首档前为空", len(prev_enabled) == len(users))

    # 分桶稳定性：同一用户跨档位/跨快照/跨实例 bucket 永不漂移
    snap = store.snapshot(now=T_STAGE0 + DAY)
    buckets_a = [snap.bucket("new_payment", u) for u in users[:1000]]
    store2 = FlagStore(ROLLOUT_CONFIG, env="prod")
    buckets_b = [store2.snapshot(now=T_STAGE0 + 9 * DAY).bucket(
        "new_payment", u) for u in users[:1000]]
    check("分桶稳定：跨档位、跨快照、跨 store 实例 1000 用户 bucket 完全一致",
          buckets_a == buckets_b
          and all(0 <= b < BUCKET_COUNT for b in buckets_a))

    # 个体轨迹：取一个落在 10% 桶内的用户，演示 False -> True 只翻转一次
    sample = next(u for u in users
                  if snap.bucket("new_payment", u) < 1000)
    states = [store.snapshot(now=T_STAGE0 + d * DAY).is_enabled(
        "new_payment", sample) for d in range(10)]
    flips = sum(1 for a, b in zip(states, states[1:]) if a != b)
    check(f"单个用户阶梯推进中至多翻转一次（{sample}: {states}）",
          flips <= 1 and states[0] is False and states[-1] is True)
    print("---- 阶梯求值轨迹示例 ----")
    print(store.snapshot(now=T_STAGE0 + 3 * DAY).evaluate(
        "new_payment", sample).explain())

    # 越界配置被拒绝
    def reject(raw_rollout, label):
        bad = {"flags": {"f": {"rules": [
            {"id": "r", "value": True, "rollout": raw_rollout}]}}}
        try:
            compile_config(bad)
            check(f"阶梯越界配置被拒绝（{label}）", False)
        except ValueError:
            check(f"阶梯越界配置被拒绝（{label}）", True)

    reject([{"time": "2026-10-01T00:00:00Z", "percentage": 101}], "比例 >100")
    reject([{"time": "2026-10-01T00:00:00Z", "percentage": -1}], "比例 <0")
    reject([{"time": "2026-10-02T00:00:00Z", "percentage": 50},
            {"time": "2026-10-01T00:00:00Z", "percentage": 80}], "时间倒序")
    reject([{"time": "2026-10-01T00:00:00Z", "percentage": 50},
            {"time": "2026-10-02T00:00:00Z", "percentage": 49}], "比例回退")


if __name__ == "__main__":
    test_priority()
    test_bucket_distribution()
    test_snapshot_isolation()
    test_conflicts_and_edges()
    test_benchmark()
    test_import_export()
    test_version_diff()
    test_rollout_ladder()
    failed = [n for n, ok in _results if not ok]
    print(f"\n==== {len(_results) - len(failed)}/{len(_results)} 项通过 ====")
    raise SystemExit(1 if failed else 0)
