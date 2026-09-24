"""压力测试：10 万次请求，注入乱序 + 丢包 + 重放。

校验：
1. 统计自洽：succeeded + timed_out + failed + cancelled == submitted == 总数
2. 客户端统计与调用方观察到的结果一致
3. 无悬挂状态，内存回落（tracemalloc 当前占用回到基线附近）

运行：python3 stress.py [total]
"""
import asyncio
import gc
import resource
import sys
import tracemalloc

from rrc import Client, RequestTimeout, SimulatedChannel

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
WORKERS = 2000
MAX_INFLIGHT = 1000


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


async def main() -> None:
    # 乱序：0~2ms 随机延迟；丢包 2%；重放 0.5%；长尾延迟 0.5%（>超时，产生迟到响应）
    channel = SimulatedChannel(
        latency=(0.0, 0.002), loss=0.02, replay=0.005,
        slow=0.005, slow_latency=(1.5, 2.0), seed=42,
    )
    # 墓碑表需覆盖最大迟到窗口（吞吐 ~2.5w/s × 长尾 2s），否则迟到响应会被归为 unknown
    client = Client(channel, max_inflight=MAX_INFLIGHT, on_full="queue",
                    default_timeout=1.0, tombstone_size=65536)

    # 预热一轮，让解释器/分配器进入稳态
    warm = {"ok": 0, "timeout": 0, "fail": 0}
    await run_round(client, 5000, warm)
    client.check_consistent()

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    rss_before = rss_mb()

    outcomes = {"ok": 0, "timeout": 0, "fail": 0}
    await run_round(client, TOTAL, outcomes)

    # --- 校验 1：统计自洽 ---
    s = client.stats
    submitted_round = s["submitted"] - 5000
    assert outcomes["ok"] + outcomes["timeout"] + outcomes["fail"] == TOTAL
    assert submitted_round == TOTAL
    assert s["succeeded"] - warm["ok"] == outcomes["ok"]
    assert s["timed_out"] - warm["timeout"] == outcomes["timeout"]
    client.check_consistent()
    assert s["unknown"] == 0, "不应出现未知 id"

    # --- 校验 2：无悬挂 + 内存回落 ---
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    after = tracemalloc.take_snapshot()
    growth = sum(st.size_diff for st in after.compare_to(baseline, "filename"))
    assert client.inflight == 0
    assert len(client._tombstones) <= client.tombstone_size
    # 增长应几乎全部来自有界墓碑表；清空后必须回落到基线附近
    client._tombstones.clear()
    gc.collect()
    settled = tracemalloc.take_snapshot()
    residual = sum(st.size_diff for st in settled.compare_to(baseline, "filename"))
    tracemalloc.stop()
    assert residual < 10 * 1024 * 1024, f"内存未回落: +{residual / 1e6:.1f} MB"

    print(f"total requests      : {TOTAL}")
    print(f"  succeeded         : {outcomes['ok']}")
    print(f"  timed out (loss)  : {outcomes['timeout']}")
    print(f"  failed            : {outcomes['fail']}")
    print(f"  sum == total      : {outcomes['ok'] + outcomes['timeout'] + outcomes['fail'] == TOTAL}")
    print(f"duplicate responses : {s['duplicate'] - 0} (replayed, discarded)")
    print(f"late responses      : {s['late']} (after timeout, discarded)")
    print(f"unknown ids         : {s['unknown']}")
    print(f"peak inflight       : {s['peak_inflight']} (limit {MAX_INFLIGHT})")
    print(f"channel sent/recv   : {channel.sent_count}/{channel.delivered_count}, "
          f"dropped {channel.dropped_count}")
    print(f"dangling entries    : {client.inflight}")
    print(f"tracemalloc current : {current / 1e6:.1f} MB (peak {peak / 1e6:.1f} MB)")
    print(f"heap growth vs base : {growth / 1e6:+.2f} MB (含墓碑表, 有界)")
    print(f"heap after cleanup  : {residual / 1e6:+.2f} MB (回落校验)")
    print(f"RSS before/after    : {rss_before:.0f} MB / {rss_mb():.0f} MB (peak)")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
