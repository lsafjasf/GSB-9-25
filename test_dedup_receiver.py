"""dedup_receiver 自测：边界、误判、乱序、跳跃、回绕、异常标识、统计自洽。

运行: python3 test_dedup_receiver.py -v
"""

import random
import unittest

from dedup_receiver import DedupReceiver, Verdict


class TestBasicDedup(unittest.TestCase):
    def test_first_accept_then_duplicate_dropped(self):
        rx = DedupReceiver(window_size=64)
        self.assertIs(rx.receive("s1", 1, b"hello"), Verdict.ACCEPTED)
        self.assertIs(rx.receive("s1", 1, b"hello"), Verdict.DUPLICATE)
        self.assertIs(rx.receive("s1", 1, b"hello"), Verdict.DUPLICATE)
        self.assertEqual(rx.stats.accepted, 1)
        self.assertEqual(rx.stats.duplicate_dropped, 2)

    def test_exactly_once_delivery(self):
        delivered = []
        rx = DedupReceiver(window_size=64, on_message=lambda s, q, p: delivered.append((s, q, p)))
        for _ in range(5):  # 上游重复投递 5 次
            rx.receive("s1", 7, b"payload")
        self.assertEqual(delivered, [("s1", 7, b"payload")])

    def test_sources_are_independent(self):
        rx = DedupReceiver(window_size=64)
        self.assertIs(rx.receive("a", 1), Verdict.ACCEPTED)
        self.assertIs(rx.receive("b", 1), Verdict.ACCEPTED)  # 同序号不同来源不算重复


class TestWindowBoundary(unittest.TestCase):
    """误判风险的边界用例：窗口 W=8，乱序容忍度 = W-1 = 7 个序号。"""

    def test_oldest_in_window_still_deduplicated(self):
        rx = DedupReceiver(window_size=8)
        rx.receive("s", 0, b"m0")                       # highest = 0
        for q in range(1, 8):
            rx.receive("s", q)                          # highest = 7，窗口覆盖序号 0..7
        # 序号 0 距 highest 恰好 offset = W-1 = 7，仍在窗口内 -> 重复
        self.assertIs(rx.receive("s", 0, b"m0"), Verdict.DUPLICATE)

    def test_duplicate_just_outside_window_is_expired(self):
        """刚好越界的重复：offset == W 时按过期策略丢弃并计数（误判边界）。"""
        rx = DedupReceiver(window_size=8)
        rx.receive("s", 0, b"m0")                       # highest = 0
        rx.receive("s", 8)                              # 跳跃 8 >= W，窗口重置，highest = 8
        # 序号 0 距 highest 为 8 == W，刚好越界 -> EXPIRED（不再认得它是重复）
        self.assertIs(rx.receive("s", 0, b"m0"), Verdict.EXPIRED)
        self.assertEqual(rx.stats.expired_dropped, 1)

    def test_boundary_slot_accepts_out_of_order_fill(self):
        rx = DedupReceiver(window_size=8)
        rx.receive("s", 7)                              # highest = 7，只登记了 7
        self.assertIs(rx.receive("s", 0), Verdict.ACCEPTED)   # offset 7 = W-1，窗口内补位
        self.assertIs(rx.receive("s", 0), Verdict.DUPLICATE)  # 再次到达 -> 重复
        rx.receive("s", 8)                              # highest = 8，序号 0 滑出窗口
        self.assertIs(rx.receive("s", 0), Verdict.EXPIRED)    # offset 8 = W，过期

    def test_out_of_order_within_window_accepted_once(self):
        rx = DedupReceiver(window_size=16)
        rx.receive("s", 10)
        for q in (3, 7, 1, 9, 2):                       # 乱序但在窗口内
            self.assertIs(rx.receive("s", q), Verdict.ACCEPTED)
        for q in (3, 7, 1, 9, 2):                       # 各自的重复投递
            self.assertIs(rx.receive("s", q), Verdict.DUPLICATE)


class TestJumpsAndWraparound(unittest.TestCase):
    def test_large_forward_jump_resets_window(self):
        rx = DedupReceiver(window_size=8)
        rx.receive("s", 0)
        rx.receive("s", 1_000_000)                      # 大幅跳跃：窗口整体前移
        self.assertIs(rx.receive("s", 0), Verdict.EXPIRED)        # 跳跃前的旧序号一律过期
        self.assertIs(rx.receive("s", 999_999), Verdict.ACCEPTED) # 窗口内补位仍可用
        self.assertIs(rx.receive("s", 999_999), Verdict.DUPLICATE) # 补位后再到即重复

    def test_sequence_wraparound(self):
        # 模数空间需满足 W <= M/2，且工程上应 W << M/2 以留出无歧义判定区
        rx = DedupReceiver(window_size=8, modulus=64)
        for q in (62, 63, 0, 1, 2):                     # 回绕 63 -> 0
            self.assertIs(rx.receive("s", q), Verdict.ACCEPTED)
        self.assertIs(rx.receive("s", 63), Verdict.DUPLICATE)     # 回绕后仍认得旧序号
        self.assertIs(rx.receive("s", 62), Verdict.DUPLICATE)
        self.assertIs(rx.receive("s", 3), Verdict.ACCEPTED)
        # 推进到 10 后，序号 0 的向后模距离为 10，落在 [W, M/2] = [8, 32] -> 过期
        for q in range(4, 11):
            rx.receive("s", q)
        self.assertIs(rx.receive("s", 0), Verdict.EXPIRED)

    def test_wrap_rejects_seq_out_of_range(self):
        rx = DedupReceiver(window_size=8, modulus=64)
        self.assertIs(rx.receive("s", 64), Verdict.ABNORMAL)


class TestAbnormalAndConflict(unittest.TestCase):
    def test_missing_or_illegal_identifier(self):
        rx = DedupReceiver(window_size=8)
        self.assertIs(rx.receive(None, 1), Verdict.ABNORMAL)      # 来源缺失
        self.assertIs(rx.receive("", 1), Verdict.ABNORMAL)        # 来源为空
        self.assertIs(rx.receive("s", None), Verdict.ABNORMAL)    # 序号缺失
        self.assertIs(rx.receive("s", -1), Verdict.ABNORMAL)      # 负序号
        self.assertIs(rx.receive("s", 1.5), Verdict.ABNORMAL)     # 非整数序号
        self.assertIs(rx.receive("s", True), Verdict.ABNORMAL)    # bool 不算序号
        self.assertEqual(rx.stats.abnormal_id, 6)
        self.assertEqual(rx.stats.accepted, 0)

    def test_same_id_different_content_is_conflict(self):
        """标识重复但内容不同：确定性行为 = 先到生效，后到按冲突丢弃并计数。"""
        rx = DedupReceiver(window_size=8)
        self.assertIs(rx.receive("s", 1, b"version-A"), Verdict.ACCEPTED)
        self.assertIs(rx.receive("s", 1, b"version-B"), Verdict.CONFLICT)
        self.assertIs(rx.receive("s", 1, b"version-A"), Verdict.DUPLICATE)  # 内容一致仍是普通重复
        self.assertEqual(rx.stats.conflict_dropped, 1)
        self.assertEqual(rx.stats.duplicate_dropped, 1)


class TestStatsInvariant(unittest.TestCase):
    def test_invariant_under_random_workload(self):
        rng = random.Random(20260925)
        rx = DedupReceiver(window_size=64, max_sources=32)
        for _ in range(200_000):
            src = f"src-{rng.randrange(50)}"
            seq = rng.randrange(2000)
            payload = str(rng.random()).encode() if rng.random() < 0.05 else b""
            rx.receive(src, seq, payload)
        s = rx.stats
        self.assertTrue(s.check_invariant(), s.as_dict())
        self.assertEqual(s.received, 200_000)
        self.assertGreater(s.duplicate_dropped, 0)
        self.assertGreater(s.expired_dropped, 0)
        self.assertLessEqual(rx.tracked_sources, 32)  # LRU 上限生效


class TestMisjudgmentRate(unittest.TestCase):
    """误判率仿真：过期丢弃概率 = P(乱序深度 >= W)。

    模型：发送方依次发 0..N-1，每条消息延迟 d 个发送节拍后到达。
    只要 d < W 就不会误判；d >= W 时该消息到达时已滑出窗口 -> 被过期丢弃。
    """

    @staticmethod
    def _simulate(n, window_size, delay_fn, seed=7):
        rng = random.Random(seed)
        rx = DedupReceiver(window_size=window_size)
        pending = {}
        for i in range(n):
            pending.setdefault(i + delay_fn(rng), []).append(i)  # 发送 seq=i，延迟后到达
            for seq in pending.pop(i, []):                       # 投递本时刻到达的全部消息
                rx.receive("s", seq)
        for t in sorted(pending):
            for seq in pending[t]:
                rx.receive("s", seq)
        return rx.stats

    def test_delay_below_window_zero_misjudgment(self):
        # 乱序深度最大 63 < W=64：理论误判率 0
        stats = self._simulate(50_000, 64, lambda rng: rng.randrange(64))
        self.assertEqual(stats.expired_dropped, 0)
        self.assertEqual(stats.accepted, 50_000)

    def test_delay_beyond_window_matches_theory(self):
        # 1% 的消息延迟 70 >= W=64，其余延迟 <= 10：理论误判率 ≈ 1%
        n = 100_000
        stats = self._simulate(n, 64, lambda rng: 70 if rng.random() < 0.01 else rng.randrange(11))
        rate = stats.expired_dropped / n
        self.assertGreater(rate, 0.005)
        self.assertLess(rate, 0.02)
        self.assertTrue(stats.check_invariant())


if __name__ == "__main__":
    unittest.main()
