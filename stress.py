"""压力测试：默认 10 万次请求，注入乱序 + 丢包 + 重放 + 长尾延迟。

校验：
1. 统计自洽：succeeded + timed_out + failed + cancelled == submitted == 总数
2. 分类对账：每条进入客户端的响应被且只被分类一次——
   delivered + injected == matched + duplicate + late + expired + unknown
   且 late == late_timeout + late_cancelled + late_failed（子原因可加）
3. 场景 A（大墓碑）：expired == 0，unknown == 注入的幽灵数
4. 场景 B（小墓碑 + 短 TTL）：expired > 0，对账不变式仍成立
5. 内存有界且可复算：墓碑表占用 <= 推导上界；清表后残留回落到基线附近

运行：python3 stress.py [total] [tombstone_size] [tombstone_ttl]
"""
import gc
import resource
import sys
import time
import tracemalloc
import asyncio

from rrc import Client, RequestTimeout, SimulatedChannel

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
TOMB_SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 65536
TOMB_TTL = float(sys.argv[3]) if len(sys.argv) > 3 else None
WORKERS = 2000
MAX_INFLIGHT = 1000
GHOSTS = 3  # 注入的未知 id 数
DRAIN = 2.2  # 排干等待：覆盖最大长尾延迟 2.0s，让在途响应全部落地后再对账


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


async def run_round(client: Client, total: int, outcomes: dict) -> None:
    it = iter(range(total))

    async def worker() -> None:
        for i in it:
            try:
                r = await client.call(f"req-{i}")
                assert r == f"req-{i}", f"corrupted match: {r!r} != req-{i}"
                outcomes["ok"] += 1
            except RequestTimeout:
                outcomes["timeout"] += 1
            except Exception:
                outcomes["fail"] += 1

    await asyncio.gather(*(worker() for _ in range(WORKERS)))


def check_reconciliation(client: Client, channel: SimulatedChannel, base: dict,
                         injected: int, label: str) -> None:
    """分类对账：进入客户端的每条响应被且只被分类一次。"""
    s = client.stats
    matched = (s["succeeded"] - base["succeeded"]) + (s["failed"] - base["failed"])
    classified = (matched + s["duplicate"] + s["late"] + s["expired"] + s["unknown"]
                  - base["dup"] - base["late"] - base["expired"] - base["unknown"])
    arrived = (channel.delivered_count - base["delivered"]) + injected
    assert classified == arrived, f"[{label}] 对账失败: {classified} != {arrived}"
    assert s["late"] == s["late_timeout"] + s["late_cancelled"] + s["late_failed"]
    client.check_consistent()


def snap(client: Client, channel: SimulatedChannel) -> dict:
    s = client.stats
    return {"succeeded": s["succeeded"], "failed": s["failed"],
            "dup": s["duplicate"], "late": s["late"], "expired": s["expired"],
            "unknown": s["unknown"], "delivered": channel.delivered_count}


def mem_report(client: Client, baseline, rate: float, label: str) -> None:
    """输出两份内存数字：墓碑表占用（含推导上界）与清表后残留。"""
    gc.collect()
    growth = sum(st.size_diff for st in
                 tracemalloc.take_snapshot().compare_to(baseline, "filename"))
    tomb_bytes = client.tombstone_memory()
    bound = client.tombstone_bound(rate)
    entry_cost = client.tombstone_entry_cost()
    assert len(client._tombstones) <= client.tombstone_size
    assert tomb_bytes <= bound, f"[{label}] 墓碑表超出推导上界: {tomb_bytes} > {bound}"
    client._tombstones.clear()
    gc.collect()
    residual = sum(st.size_diff for st in
                   tracemalloc.take_snapshot().compare_to(baseline, "filename"))
    assert residual < 10 * 1024 * 1024, f"[{label}] 内存未回落: +{residual / 1e6:.1f} MB"
    print(f"[{label}] tombstone entries   : {len(client._tombstones)} (cleared)")
    print(f"[{label}] 单条墓碑实测成本    : {entry_cost} B "
          f"(OrderedDict 节点 + int 键 + (终态, 过期时刻) 元组)")
    print(f"[{label}] 墓碑表占用(实测)    : {tomb_bytes / 1e6:.3f} MB "
          f"(tracemalloc 堆增长 {growth / 1e6:+.2f} MB)")
    print(f"[{label}] 墓碑表占用(上界)    : {bound / 1e6:.3f} MB "
          f"= 空表 + min(size={client.tombstone_size}, "
          f"ttl×rate) × {entry_cost} B")
    print(f"[{label}] 清表后残留          : {residual / 1e6:+.2f} MB (回落校验)")


async def scenario_a() -> None:
    print("=== 场景 A：大墓碑表（容量覆盖最大迟到窗口） ===")
    channel = SimulatedChannel(
        latency=(0.0, 0.002), loss=0.02, replay=0.005,
        slow=0.005, slow_latency=(1.5, 2.0), seed=42,
    )
    client = Client(channel, max_inflight=MAX_INFLIGHT, on_full="queue",
                    default_timeout=1.0, tombstone_size=TOMB_SIZE,
                    tombstone_ttl=TOMB_TTL)

    warm = {"ok": 0, "timeout": 0, "fail": 0}
    await run_round(client, 5000, warm)
    client.check_consistent()

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    rss_before = rss_mb()
    base = snap(client, channel)

    outcomes = {"ok": 0, "timeout": 0, "fail": 0}
    t0 = time.monotonic()
    await run_round(client, TOTAL, outcomes)
    rate = TOTAL / (time.monotonic() - t0)
    await asyncio.sleep(DRAIN)  # 等待尾部长尾响应到达，分类计数方完整

    # 注入幽灵响应：id 大于已发序号 => 必须计 unknown 而非 expired
    for k in range(GHOSTS):
        channel.inject({"id": client._seq + 10**6 + k, "payload": "ghost"})

    s = client.stats
    assert outcomes["ok"] + outcomes["timeout"] + outcomes["fail"] == TOTAL
    assert s["submitted"] - 5000 == TOTAL
    assert s["succeeded"] - base["succeeded"] == outcomes["ok"]
    assert s["timed_out"] - (warm["timeout"]) == outcomes["timeout"]
    if TOMB_TTL is None and TOMB_SIZE >= 65536:
        # 默认配置：墓碑窗口覆盖最大迟到延迟，重放/长尾分别计 duplicate/late
        assert s["duplicate"] > 0 and s["late"] > 0
        assert s["expired"] == 0, "大墓碑窗口内不应出现过期响应"
    assert s["duplicate"] + s["late"] + s["expired"] > 0, "重放/长尾响应应被分类"
    assert s["unknown"] == GHOSTS, f"unknown 应等于注入幽灵数 {GHOSTS}"
    check_reconciliation(client, channel, base, GHOSTS, "A")

    assert client.inflight == 0
    mem_report(client, baseline, rate=rate, label="A")
    tracemalloc.stop()

    print(f"total requests      : {TOTAL}")
    print(f"  succeeded         : {outcomes['ok']}")
    print(f"  timed out (loss)  : {outcomes['timeout']}")
    print(f"  failed            : {outcomes['fail']}")
    print(f"  sum == total      : {outcomes['ok'] + outcomes['timeout'] + outcomes['fail'] == TOTAL}")
    print(f"duplicate responses : {s['duplicate']} (重放副本, 已丢弃)")
    print(f"late responses      : {s['late']} "
          f"(timeout={s['late_timeout']}, cancelled={s['late_cancelled']}, "
          f"failed={s['late_failed']})")
    print(f"expired responses   : {s['expired']} (墓碑被 TTL/容量驱逐)")
    print(f"unknown ids         : {s['unknown']} (注入幽灵 {GHOSTS})")
    print(f"peak inflight       : {s['peak_inflight']} (limit {MAX_INFLIGHT})")
    print(f"channel sent/recv   : {channel.sent_count}/{channel.delivered_count}, "
          f"dropped {channel.dropped_count}, replayed {channel.replayed_count}, "
          f"slow {channel.slow_count}")
    print(f"dangling entries    : {client.inflight}")
    print(f"RSS peak            : {rss_before:.0f} MB -> {rss_mb():.0f} MB")
    print("SCENARIO A PASSED")


async def scenario_b() -> None:
    print("\n=== 场景 B：小墓碑 + 短 TTL（迟到响应转为 expired） ===")
    total_b = max(TOTAL // 5, 10_000)
    channel = SimulatedChannel(
        latency=(0.0, 0.002), loss=0.02, replay=0.005,
        slow=0.005, slow_latency=(1.5, 2.0), seed=7,
    )
    client = Client(channel, max_inflight=MAX_INFLIGHT, on_full="queue",
                    default_timeout=1.0, tombstone_size=256, tombstone_ttl=0.3)

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    base = snap(client, channel)

    outcomes = {"ok": 0, "timeout": 0, "fail": 0}
    t0 = time.monotonic()
    await run_round(client, total_b, outcomes)
    rate = total_b / (time.monotonic() - t0)
    await asyncio.sleep(DRAIN)  # 排干：迟到的长尾响应全部落地，转为 expired

    s = client.stats
    assert outcomes["ok"] + outcomes["timeout"] + outcomes["fail"] == total_b
    assert s["expired"] > 0, "短 TTL 下迟到的长尾响应应被分类为 expired"
    assert s["unknown"] == 0, "未注入幽灵，不应出现未知 id"
    check_reconciliation(client, channel, base, 0, "B")
    mem_report(client, baseline, rate=rate, label="B")
    tracemalloc.stop()

    print(f"total requests      : {total_b}")
    print(f"duplicate/late/expired/unknown : "
          f"{s['duplicate']}/{s['late']}/{s['expired']}/{s['unknown']}")
    print(f"tombstone bound     : size=256, ttl=0.3s "
          f"(条目 <= min(256, 吞吐x0.3), 与请求总量无关)")
    print("SCENARIO B PASSED")


async def main() -> None:
    await scenario_a()
    await scenario_b()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
