"""DedupReceiver 自测：边界、误判、乱序/跳跃/回绕、异常标识、统计自洽。

运行：python3 -m unittest test_dedup_receiver -v
"""

import unittest

from dedup_receiver import DedupReceiver


def msg(mid, seq, payload="p"):
    return {"id": mid, "seq": seq, "payload": payload}


class TestWindowDedup(unittest.TestCase):
    def setUp(self):
        self.handled = []
        self.rx = DedupReceiver(window_size=8, max_streams=4, seq_bits=32,
                                handler=self.handled.append)

    def test_in_order_and_exact_duplicate(self):
        self.assertEqual(self.rx.receive(msg("s", 1)), "processed_new_stream")
        self.assertEqual(self.rx.receive(msg("s", 2)), "processed")
        self.assertEqual(self.rx.receive(msg("s", 2)), "duplicate")
        self.assertEqual(self.rx.receive(msg("s", 1)), "duplicate")
        self.assertEqual(len(self.handled), 2)

    def test_out_of_order_within_window(self):
        self.rx.receive(msg("s", 10))
        self.assertEqual(self.rx.receive(msg("s", 7)), "processed")  # 乱序新消息
        self.assertEqual(self.rx.receive(msg("s", 7)), "duplicate")  # 再次到达判重
        self.assertEqual(self.rx.receive(msg("s", 9)), "processed")

    def test_boundary_inside_window(self):
        # 落后距离 == window_size - 1：仍在窗口内，重复可判定
        self.rx.receive(msg("s", 100))
        self.rx.receive(msg("s", 100 - 7))  # 距离 7 < 8，窗口内
        self.assertEqual(self.rx.receive(msg("s", 100 - 7)), "duplicate")

    def test_boundary_just_outside_window(self):
        # 刚好越界：落后距离 == window_size，判 expired（误判风险的边界）
        self.rx.receive(msg("s", 100))
        self.assertEqual(self.rx.receive(msg("s", 100 - 8)), "expired")
        self.assertEqual(self.rx.stats["expired"], 1)
        # 越界消息不会被处理，也不会污染窗口
        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.rx.receive(msg("s", 100 - 8)), "expired")
        self.assertEqual(self.rx.stats["expired"], 2)

    def test_late_duplicate_after_advance(self):
        # 构造"刚好越界的重复"：先处理 seq=1，再推进 window_size 条新消息，
        # 此时 seq=1 的迟到重复落后距离 == window_size -> expired 而非 duplicate
        self.rx.receive(msg("s", 1))
        for seq in range(2, 2 + 8):  # 推进 8 条，max_seq = 9
            self.rx.receive(msg("s", seq))
        self.assertEqual(self.rx.receive(msg("s", 1)), "expired")
        # 而落后距离 7 的 seq=2 仍是窗口内重复
        self.assertEqual(self.rx.receive(msg("s", 2)), "duplicate")

    def test_big_jump_clears_window(self):
        self.rx.receive(msg("s", 1))
        self.rx.receive(msg("s", 5))
        self.assertEqual(self.rx.receive(msg("s", 1000)), "processed")
        self.assertEqual(self.rx.stats["jumps"], 1)
        # 跳跃前的所有序号都已过期
        self.assertEqual(self.rx.receive(msg("s", 5)), "expired")
        self.assertEqual(self.rx.receive(msg("s", 1)), "expired")

    def test_incremental_evict_with_out_of_order(self):
        # 乱序插入后持续推进窗口，过期记录应被精确淘汰（增量淘汰正确性）
        self.rx.receive(msg("s", 10))
        self.rx.receive(msg("s", 5))              # 乱序落入窗口内空隙
        for seq in range(11, 20):                 # 推进到 max_seq = 19（跳过 14 留空隙）
            if seq != 14:
                self.rx.receive(msg("s", seq))
        # 窗口覆盖落后距离 < 8 的序号，即 [12, 19]
        self.assertEqual(self.rx.receive(msg("s", 5)), "expired")
        self.assertEqual(self.rx.receive(msg("s", 10)), "expired")
        self.assertEqual(self.rx.receive(msg("s", 12)), "duplicate")  # 未被误淘汰
        self.assertEqual(self.rx.receive(msg("s", 14)), "processed")  # 窗口内空隙
        _, entries = self.rx.state_size()
        self.assertLessEqual(entries, 8)          # 窗口记录数不超过 window_size

    def test_conflict_same_id_seq_different_payload(self):
        self.rx.receive(msg("s", 1, "alpha"))
        self.assertEqual(self.rx.receive(msg("s", 1, "alpha")), "duplicate")
        self.assertEqual(self.rx.receive(msg("s", 1, "beta")), "conflict")
        # 冲突消息不处理、不计重复
        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.rx.stats["conflict"], 1)
        self.assertEqual(self.rx.stats["duplicate"], 1)


class TestWraparound(unittest.TestCase):
    def test_seq_wraparound(self):
        handled = []
        rx = DedupReceiver(window_size=4, max_streams=2, seq_bits=8,
                           handler=handled.append)
        for seq in (254, 255, 0, 1, 2):  # 跨回绕点继续前进
            self.assertIn(rx.receive(msg("w", seq)),
                          ("processed_new_stream", "processed"))
        self.assertEqual(len(handled), 5)
        # 回绕前的近期序号仍在窗口内，可判重
        self.assertEqual(rx.receive(msg("w", 255)), "duplicate")
        self.assertEqual(rx.receive(msg("w", 0)), "duplicate")
        # 回绕后落后距离 >= window_size 的序号过期：
        # max_seq=2, 254 落后距离 = (2-254)&255 = 4 >= 4 -> expired
        self.assertEqual(rx.receive(msg("w", 254)), "expired")
        self.assertEqual(rx.receive(msg("w", 253)), "expired")


class TestUndeterminedIdentity(unittest.TestCase):
    def setUp(self):
        self.handled = []
        self.undetermined = []
        self.rx = DedupReceiver(window_size=8, max_streams=4,
                                handler=self.handled.append,
                                undetermined_handler=self.undetermined.append)

    def test_missing_or_invalid_id(self):
        for bad in (None, "", 123, b"bytes"):
            self.assertEqual(self.rx.receive(msg(bad, 1)), "undetermined")
        self.assertEqual(self.rx.stats["undetermined"], 4)
        # 无法判定的消息不进入正常处理流程，交给兜底回调
        self.assertEqual(len(self.handled), 0)
        self.assertEqual(len(self.undetermined), 4)

    def test_missing_or_invalid_seq(self):
        self.assertEqual(self.rx.receive({"id": "s"}), "undetermined")
        self.assertEqual(self.rx.receive(msg("s", -1)), "undetermined")
        self.assertEqual(self.rx.receive(msg("s", "3")), "undetermined")
        self.assertEqual(self.rx.receive(msg("s", True)), "undetermined")
        self.assertEqual(self.rx.stats["undetermined"], 4)
        self.assertEqual(len(self.handled), 0)
        # 异常序号不污染正常流状态
        self.assertEqual(self.rx.receive(msg("s", 3)), "processed_new_stream")

    def test_redelivery_not_double_processed(self):
        # 复现旧版缺陷：缺标识的消息直接交给处理器，重投一次就被处理两次。
        # 现在无法判定的消息与正常处理分开：重投只重复计数，不重复处理。
        bad = {"id": None, "seq": 5, "payload": "no-id"}
        self.assertEqual(self.rx.receive(bad), "undetermined")
        self.assertEqual(self.rx.receive(dict(bad)), "undetermined")  # 重投
        self.assertEqual(len(self.handled), 0)       # 正常处理器一次都没看到
        self.assertEqual(len(self.undetermined), 2)  # 兜底回调每次都能收到
        self.assertEqual(self.rx.stats["undetermined"], 2)
        self.assertEqual(self.rx.stats["processed"], 0)

    def test_undetermined_without_fallback_handler(self):
        rx = DedupReceiver(window_size=8, max_streams=4,
                           handler=self.handled.append)
        self.assertEqual(rx.receive(msg(None, 1)), "undetermined")
        self.assertEqual(rx.stats["undetermined"], 1)
        self.assertEqual(len(self.handled), 0)


class TestBoundedMemory(unittest.TestCase):
    def test_stream_lru_eviction(self):
        rx = DedupReceiver(window_size=4, max_streams=3)
        for i in range(3):
            rx.receive(msg(f"s{i}", 1))
        self.assertEqual(rx.receive(msg("s3", 1)), "processed_new_stream")
        self.assertEqual(rx.stats["evictions"], 1)
        self.assertEqual(rx.state_size()[0], 3)  # 流数量不超过上限
        # 被淘汰的流重新到达：状态已丢失，按新流处理（误判为"新消息"）
        self.assertEqual(rx.receive(msg("s0", 1)), "processed_new_stream")

    def test_state_size_independent_of_volume(self):
        rx = DedupReceiver(window_size=16, max_streams=8)
        for n in range(20000):
            rx.receive(msg(f"s{n % 8}", n // 8))
        streams, entries = rx.state_size()
        self.assertLessEqual(streams, 8)
        self.assertLessEqual(entries, 8 * 16)


class TestStatsConsistency(unittest.TestCase):
    def test_stats_partition_received(self):
        rx = DedupReceiver(window_size=8, max_streams=4, seq_bits=32)
        scenario = (
            [msg("a", s) for s in range(1, 30)]          # 正常推进
            + [msg("a", 28), msg("a", 28, "other")]      # 窗口内重复 + 冲突
            + [msg("a", 200)]                            # 跳跃
            + [msg("a", 1), msg("a", 2)]                 # 过期
            + [msg(None, 1), {"id": "b"}]                # 异常标识
            + [msg("b", 1), msg("b", 1)]                 # 另一流正常+重复
        )
        for m in scenario:
            rx.receive(m)
        s = rx.stats
        self.assertEqual(s["received"], len(scenario))
        self.assertEqual(s["received"],
                         s["processed"] + s["duplicate"]
                         + s["expired"] + s["conflict"]
                         + s["undetermined"])
        self.assertTrue(rx.check_consistency())
        self.assertEqual(s["duplicate"], 2)
        self.assertEqual(s["conflict"], 1)
        self.assertEqual(s["expired"], 2)
        self.assertEqual(s["undetermined"], 2)


if __name__ == "__main__":
    unittest.main()
