"""audit_chain 自测（仅标准库 unittest）。

运行：python3 -m unittest test_audit_chain -v
"""

import os
import tempfile
import unittest

from audit_chain import (
    AuditChain, Anchor, CorruptStoreError, GENESIS_HASH,
    MODIFIED, INSERTED, DELETED, TRUNCATED, Record, compute_hash,
)


def build_chain(n):
    c = AuditChain()
    for i in range(n):
        c.append({"op": "write", "row": i, "user": f"u{i % 7}"})
    return c


class TestBasicChain(unittest.TestCase):
    def test_empty_chain_ok(self):
        c = AuditChain()
        r = c.verify()
        self.assertTrue(r.ok)
        self.assertEqual(r.checked, 0)
        # 空链 + 非空锚点 => 截断（退化为整体缺失）
        r = c.verify(anchor=Anchor(count=3, tail_hash=GENESIS_HASH))
        self.assertFalse(r.ok)
        self.assertEqual(r.error, TRUNCATED)
        self.assertEqual(r.position, 0)

    def test_single_record_ok(self):
        c = build_chain(1)
        r = c.verify()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(c.records[0].prev, GENESIS_HASH)
        self.assertEqual(c.records[0].seq, 0)

    def test_append_only_and_selfcheck(self):
        c = build_chain(50)
        self.assertTrue(c.verify().ok)
        self.assertEqual([r.seq for r in c.records], list(range(50)))
        for i in range(1, 50):
            self.assertEqual(c.records[i].prev, c.records[i - 1].hash)

    def test_hash_is_deterministic(self):
        h1 = compute_hash(3, "ab" * 32, {"k": [1, 2], "中": "文"})
        h2 = compute_hash(3, "ab" * 32, {"中": "文", "k": [1, 2]})  # 键序无关
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)


class TestTamperDetection(unittest.TestCase):
    def setUp(self):
        self.chain = build_chain(20)
        self.anchor = self.chain.anchor()

    def tampered(self, mutate):
        c = build_chain(20)
        mutate(c.records)
        return c

    def test_content_modified(self):
        def m(recs):
            r = recs[7]
            recs[7] = Record(r.seq, r.prev, {"op": "DELETE"}, r.hash)  # 只改内容
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, MODIFIED)
        self.assertEqual(r.position, 7)

    def test_middle_record_replaced(self):
        """中间记录被另一条合法外形记录整体替换。"""
        def m(recs):
            victim = recs[10]
            fake = Record(victim.seq, victim.prev, {"op": "forged"},
                          compute_hash(victim.seq, victim.prev, {"op": "forged"}))
            recs[10] = fake  # 攻击者重算了这一条，但链接会断
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, MODIFIED)
        self.assertEqual(r.position, 10)  # 被替换的记录本身

    def test_entry_inserted(self):
        def m(recs):
            src = recs[4]
            extra = Record(src.seq, src.prev, src.data, src.hash)  # 复制第 4 条
            recs.insert(9, extra)
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, INSERTED)
        self.assertEqual(r.position, 9)

    def test_entry_deleted(self):
        def m(recs):
            del recs[6]
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, DELETED)
        self.assertEqual(r.position, 6)

    def test_first_entry_deleted(self):
        def m(recs):
            del recs[0]
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, DELETED)
        self.assertEqual(r.position, 0)

    def test_tail_truncated(self):
        def m(recs):
            del recs[15:]
        c = self.tampered(m)
        # 无锚点：截断本质上不可检出（链仍自洽）
        self.assertTrue(c.verify().ok)
        # 有锚点：检出截断，位置 = 第一个缺失下标
        r = c.verify(anchor=self.anchor)
        self.assertEqual(r.error, TRUNCATED)
        self.assertEqual(r.position, 15)

    def test_record_duplicated_elsewhere(self):
        """复制整条记录（含原摘要）粘贴到链中另一位置。"""
        def m(recs):
            recs.append(recs[3])  # 原样复制第 3 条到末尾
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, INSERTED)
        self.assertEqual(r.position, 20)

    def test_two_chains_concatenated(self):
        """两条各自合法的链直接拼接。"""
        other = build_chain(10)  # 另一条从 GENESIS 开始的链
        def m(recs):
            recs.extend(other.records)
        r = self.tampered(m).verify(anchor=self.anchor)
        self.assertEqual(r.error, INSERTED)
        self.assertEqual(r.position, 20)  # 第二链的起点被识破

    def test_fully_recomputed_chain_caught_by_anchor(self):
        """攻击者改内容并重算整条链：只有锚点能检出。"""
        c = AuditChain()
        for i in range(20):
            data = {"op": "write", "row": i, "user": f"u{i % 7}"}
            if i == 5:
                data = {"op": "EVIL"}
            c.append(data)
        self.assertTrue(c.verify().ok)  # 自洽，局部校验无法发现
        r = c.verify(anchor=self.anchor)
        self.assertEqual(r.error, MODIFIED)


class TestIncrementalVerify(unittest.TestCase):
    def test_segment_ok(self):
        c = build_chain(100)
        r = c.verify(start=40, stop=60)
        self.assertTrue(r.ok)
        self.assertEqual(r.checked, 20)

    def test_segment_detects_local_tamper(self):
        c = build_chain(100)
        r = c.records[55]
        c.records[55] = Record(r.seq, r.prev, "tampered", r.hash)
        r = c.verify(start=50, stop=60)
        self.assertEqual(r.error, MODIFIED)
        self.assertEqual(r.position, 55)

    def test_segment_outside_tamper_passes(self):
        """区间外的篡改不影响该区间的增量校验结果。"""
        c = build_chain(100)
        r0 = c.records[0]
        c.records[0] = Record(r0.seq, r0.prev, "tampered", r0.hash)
        self.assertTrue(c.verify(start=50, stop=60).ok)


class TestPersistence(unittest.TestCase):
    def test_roundtrip_and_append_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            c1 = AuditChain(path)
            for i in range(30):
                c1.append({"i": i})
            size1 = os.path.getsize(path)

            c2 = AuditChain(path)  # 重新加载
            self.assertEqual(len(c2.records), 30)
            self.assertTrue(c2.verify(anchor=c1.anchor()).ok)

            c2.append({"i": 30})  # 继续追加，历史行不变
            with open(path, "rb") as f:
                head = f.read(size1)
            c3 = AuditChain(path)
            self.assertEqual(len(c3.records), 31)
            self.assertEqual(os.path.getsize(path) > size1, True)
            # 历史部分逐字节未变（append-only）
            with open(path, "rb") as f:
                self.assertEqual(f.read(size1), head)

    def test_torn_tail_line_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            c = AuditChain(path)
            c.append({"a": 1})
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"alg":"sha256","seq":1,"prev":"abc')  # 模拟崩溃撕裂行
            with self.assertRaises(CorruptStoreError):
                AuditChain(path)


if __name__ == "__main__":
    unittest.main()
