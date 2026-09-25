"""window_aggregator 自测（仅标准库 unittest）。

运行：python3 -m unittest -v test_window_aggregator
"""

import random
import tracemalloc
import unittest

from window_aggregator import WindowAggregator, WindowResult


def collect(agg):
    """返回 (emits, finals)：emits 为 (result, is_update) 列表，finals 为定稿结果列表。"""
    emits, finals = [], []
    agg.on_emit = lambda r, is_update: emits.append((r, is_update))
    agg.on_finalize = lambda r: finals.append(r)
    return emits, finals


def batch_reference(events, window_size, step):
    """与流式无关的批量参考实现：直接按定义统计每个窗口的 (count, total)。"""
    ref = {}
    for t, v in events:
        k_max = t // step
        k_min = -((window_size - 1 - t) // step)
        for k in range(k_min, k_max + 1):
            start = k * step
            c, s = ref.get(start, (0, 0))
            ref[start] = (c + 1, s + v)
    return ref


def finals_to_dict(finals):
    return {(r.start, r.end): (r.count, r.total) for r in finals}


class TestBasicSemantics(unittest.TestCase):
    def test_empty_input(self):
        agg = WindowAggregator(10, grace=2)
        emits, finals = collect(agg)
        agg.flush()  # 空输入 flush 不应产生任何输出
        self.assertEqual(emits, [])
        self.assertEqual(finals, [])
        self.assertEqual(agg.dropped_count, 0)
        self.assertEqual(agg.event_count, 0)
        self.assertIsNone(agg.watermark)

    def test_single_event_fixed_window(self):
        agg = WindowAggregator(10)  # step 缺省 = window_size -> 固定窗口
        emits, finals = collect(agg)
        agg.add(7, value=5)
        self.assertEqual(emits, [])  # 水位线 7 < end 10，尚未输出
        agg.flush()
        self.assertEqual(
            emits, [(WindowResult(0, 10, 1, 5), False)]
        )
        self.assertEqual(finals, [WindowResult(0, 10, 1, 5)])
        self.assertEqual(agg.live_window_count, 0)

    def test_single_event_sliding_window(self):
        # size=10, step=5：t=7 属于窗口 [0,10) 与 [5,15)
        agg = WindowAggregator(10, step=5)
        emits, _ = collect(agg)
        agg.add(7, value=2)
        agg.flush()
        got = {r.start: (r.count, r.total) for r, _ in emits}
        self.assertEqual(got, {0: (1, 2), 5: (1, 2)})

    def test_boundary_belongs_to_window_starting_there(self):
        # 半开区间 [start, end)：t=10 属于 [10,20)，不属于 [0,10)
        agg = WindowAggregator(10)
        _, finals = collect(agg)
        agg.add(9)
        agg.add(10)  # 恰好落在边界
        agg.add(19)
        agg.flush()
        self.assertEqual(
            finals_to_dict(finals),
            {(0, 10): (1, 1), (10, 20): (2, 2)},
        )

    def test_negative_time(self):
        agg = WindowAggregator(10, step=5)
        _, finals = collect(agg)
        agg.add(-1)  # 属于 [-10,0) 与 [-5,5)
        agg.flush()
        self.assertEqual(
            finals_to_dict(finals), {(-10, 0): (1, 1), (-5, 5): (1, 1)}
        )

    def test_non_divisible_size_and_step(self):
        # size=10, step=3 不整除：t=9 属于起点 0,3,6,9 四个窗口
        agg = WindowAggregator(10, step=3)
        _, finals = collect(agg)
        agg.add(9)
        agg.flush()
        self.assertEqual(
            finals_to_dict(finals),
            {(0, 10): (1, 1), (3, 13): (1, 1), (6, 16): (1, 1), (9, 19): (1, 1)},
        )

    def test_step_larger_than_size(self):
        # 跳跃窗口 size=4, step=10：t=5 不属于任何窗口，t=2 属于 [0,4)
        agg = WindowAggregator(4, step=10, grace=10)
        _, finals = collect(agg)
        agg.add(5)
        agg.add(2)
        agg.flush()
        self.assertEqual(finals_to_dict(finals), {(0, 4): (1, 1)})

    def test_invalid_params(self):
        with self.assertRaises(ValueError):
            WindowAggregator(0)
        with self.assertRaises(ValueError):
            WindowAggregator(10, step=0)
        with self.assertRaises(ValueError):
            WindowAggregator(10, grace=-1)
        with self.assertRaises(TypeError):
            WindowAggregator(10.0)
        agg = WindowAggregator(10)
        with self.assertRaises(TypeError):
            agg.add(1.5)


class TestLatenessAndGrace(unittest.TestCase):
    def test_late_event_within_grace_updates_emitted_window(self):
        agg = WindowAggregator(10, grace=5)
        emits, finals = collect(agg)
        agg.add(5, value=1)
        agg.add(12, value=1)  # 水位线 12 >= end 10 -> [0,10) 首次输出
        first = [e for e in emits if not e[1]]
        self.assertEqual(first, [(WindowResult(0, 10, 1, 1), False)])
        self.assertEqual(finals, [])

        agg.add(8, value=3)  # 8 >= 12-5，宽限内迟到 -> 触发更新通知
        updates = [e for e in emits if e[1]]
        self.assertEqual(updates, [(WindowResult(0, 10, 2, 4), True)])

        agg.add(20, value=1)  # 水位线 20 >= 10+5 -> [0,10) 定稿释放
        self.assertIn(WindowResult(0, 10, 2, 4), finals)

    def test_late_event_beyond_grace_dropped_and_counted(self):
        agg = WindowAggregator(10, grace=5)
        _, finals = collect(agg)
        agg.add(20)
        # 水位线 20，丢弃线 = 20-5 = 15
        self.assertFalse(agg.add(14))  # 14 < 15 -> 丢弃
        self.assertFalse(agg.add(0))
        self.assertTrue(agg.add(15))   # 恰好等于丢弃线 -> 接受
        self.assertEqual(agg.dropped_count, 2)
        self.assertEqual(agg.event_count, 2)
        agg.flush()
        self.assertEqual(finals_to_dict(finals), {(10, 20): (1, 1), (20, 30): (1, 1)})

    def test_all_events_late(self):
        agg = WindowAggregator(10, grace=3)
        emits, finals = collect(agg)
        agg.add(10000)  # 先把水位线抬高
        for t in range(0, 100):  # 全部远晚于丢弃线
            self.assertFalse(agg.add(t))
        self.assertEqual(agg.dropped_count, 100)
        self.assertEqual(agg.event_count, 1)
        agg.flush()
        # 只有 t=10000 所在的窗口有结果
        self.assertEqual(finals_to_dict(finals), {(10000, 10010): (1, 1)})
        self.assertTrue(all(not is_up for _, is_up in emits))

    def test_zero_grace(self):
        agg = WindowAggregator(10, grace=0)
        collect(agg)
        agg.add(10)
        self.assertTrue(agg.add(10))   # 等于水位线，接受
        self.assertFalse(agg.add(9))   # 任何严格更早的都丢弃
        self.assertEqual(agg.dropped_count, 1)


class TestOrderIndependence(unittest.TestCase):
    """同一批事件以不同顺序喂入（乱序不超过 grace），最终窗口统计必须一致，
    且等于与流式无关的批量参考结果。"""

    def run_case(self, window_size, step, grace, events, seed):
        rnd = random.Random(seed)
        # 按时间分桶（桶宽 grace+1），桶间有序、桶内随机洗牌 -> 保证乱序 <= grace，无丢弃
        buckets = {}
        for ev in events:
            buckets.setdefault(ev[0] // (grace + 1), []).append(ev)
        orders = []
        for _ in range(3):
            shuffled = []
            for b in sorted(buckets):
                bucket = list(buckets[b])
                rnd.shuffle(bucket)
                shuffled.extend(bucket)
            orders.append(shuffled)
        orders.append(sorted(events))  # 完全有序作为对照

        expected = batch_reference(events, window_size, step)
        results = []
        for order in orders:
            agg = WindowAggregator(window_size, step=step, grace=grace)
            _, finals = collect(agg)
            for t, v in order:
                self.assertTrue(agg.add(t, v))  # 不应有丢弃
            agg.flush()
            self.assertEqual(agg.dropped_count, 0)
            results.append(finals_to_dict(finals))
        for got in results:
            self.assertEqual(
                got,
                {(s, s + window_size): cs for s, cs in expected.items()},
            )

    def test_fixed_window(self):
        rnd = random.Random(42)
        events = [(rnd.randint(0, 500), rnd.randint(1, 9)) for _ in range(300)]
        self.run_case(10, 10, 20, events, seed=1)

    def test_sliding_window(self):
        rnd = random.Random(43)
        events = [(rnd.randint(0, 500), rnd.randint(1, 9)) for _ in range(300)]
        self.run_case(10, 5, 20, events, seed=2)

    def test_non_divisible(self):
        rnd = random.Random(44)
        events = [(rnd.randint(0, 200), rnd.randint(1, 9)) for _ in range(200)]
        self.run_case(10, 3, 7, events, seed=3)

    def test_all_permutations_small(self):
        # 小事件集全排列穷举
        import itertools

        events = [(0, 1), (1, 2), (2, 3), (5, 4), (9, 5), (10, 6)]
        expected = None
        for perm in itertools.permutations(events):
            agg = WindowAggregator(10, grace=10)
            _, finals = collect(agg)
            for t, v in perm:
                agg.add(t, v)
            agg.flush()
            got = finals_to_dict(finals)
            if expected is None:
                expected = got
            self.assertEqual(got, expected)


class TestMemoryBound(unittest.TestCase):
    def test_live_windows_never_exceed_bound(self):
        for size, step, grace in [(10, 10, 0), (10, 5, 4), (10, 3, 7), (4, 10, 2)]:
            agg = WindowAggregator(size, step=step, grace=grace)
            bound = agg.memory_bound()
            max_live = 0
            n = 200_000
            for t in range(n):
                agg.add(t)
                if t % 1000 == 0 or t == n - 1:
                    max_live = max(max_live, agg.live_window_count)
                    self.assertLessEqual(
                        agg.live_window_count, bound,
                        msg="size=%d step=%d grace=%d" % (size, step, grace),
                    )
            agg.flush()
            self.assertEqual(agg.live_window_count, 0)
            print(
                "  [内存数据] size=%d step=%d grace=%d: 事件 %d 个, "
                "上界 %d, 实测最大存活窗口 %d"
                % (size, step, grace, n, bound, max_live)
            )

    def test_memory_does_not_grow_with_event_volume(self):
        # tracemalloc 验证：长时间运行后内存占用不随事件总量增长
        agg = WindowAggregator(10, step=5, grace=4)
        for t in range(50_000):  # 预热进入稳态
            agg.add(t)
        tracemalloc.start()
        snap1 = tracemalloc.take_snapshot()
        for t in range(50_000, 450_000):  # 再喂 8 倍事件
            agg.add(t)
        snap2 = tracemalloc.take_snapshot()
        tracemalloc.stop()
        growth = sum(
            stat.size_diff
            for stat in snap2.compare_to(snap1, "filename")
            if stat.size_diff > 0
        )
        print(
            "  [内存数据] 40 万事件稳态运行，Python 堆净增长 %d 字节（阈值 256KB）"
            % growth
        )
        self.assertLess(growth, 256 * 1024)
        self.assertLessEqual(agg.live_window_count, agg.memory_bound())


if __name__ == "__main__":
    unittest.main(verbosity=2)
