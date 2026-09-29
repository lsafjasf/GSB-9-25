"""audit_chain 自测：python3 test_audit_chain.py"""

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audit_chain import (
    AuditLog, GENESIS, GENESIS_HEX,
    CONTENT_MODIFIED, ENTRY_INSERTED, ENTRY_DELETED, TRUNCATED,
    make_record, verify_records,
)


def build_records(n, tag="r"):
    recs, prev = [], GENESIS
    for i in range(n):
        r = make_record(i, prev, {"op": f"{tag}{i}", "amount": i * 10})
        recs.append(r)
        prev = bytes.fromhex(r["digest"])
    return recs


class TestChain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "audit.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    # 1. 空链
    def test_empty_chain(self):
        log = AuditLog(self.path)
        res = log.verify()
        self.assertTrue(res.ok)
        self.assertEqual(res.checked, 0)
        self.assertEqual(res.head, GENESIS_HEX)
        # 空链 + 非创世可信链头 => 视为被截断到空
        res2 = log.verify(expected_head="ab" * 32)
        self.assertFalse(res2.ok)
        self.assertEqual(res2.error, TRUNCATED)
        self.assertEqual(res2.position, 0)

    # 2. 单条记录
    def test_single_record(self):
        log = AuditLog(self.path)
        rec = log.append({"op": "open", "id": 1})
        self.assertEqual(rec["seq"], 0)
        self.assertEqual(rec["prev"], GENESIS_HEX)
        res = log.verify()
        self.assertTrue(res.ok)
        self.assertEqual(res.checked, 1)
        self.assertTrue(log.verify(expected_head=log.head).ok)

    # 3. 逐条追加后立即校验 + 只追加不重写
    def test_append_only_and_immediate_verify(self):
        log = AuditLog(self.path)
        for i in range(200):
            before = log.head
            log.append({"i": i, "中文": "内容", "nested": {"b": 1, "a": [1, 2]}})
            self.assertNotEqual(log.head, before)
            self.assertTrue(log.verify().ok)          # 追加后全量校验
            self.assertTrue(log.verify_from(i).ok)    # 追加后增量校验
        # 历史行未被重写：重载文件逐条比对
        log2 = AuditLog(self.path)
        self.assertEqual(log.records, log2.records)
        with open(self.path, "rb") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 200)
        self.assertTrue(all(line.endswith(b"\n") for line in lines))

    # 4. 中间记录内容被替换
    def test_content_modified_in_middle(self):
        recs = build_records(20)
        recs[7] = dict(recs[7], content={"op": "forged"})
        res = verify_records(recs)
        self.assertEqual(res.error, CONTENT_MODIFIED)
        self.assertEqual(res.position, 7)

    # 5. 内容被修改且攻击者重算了该条摘要（不重算后继）
    def test_content_modified_with_recomputed_digest(self):
        recs = build_records(20)
        forged = make_record(7, bytes.fromhex(recs[7]["prev"]), {"op": "x"})
        recs[7] = forged
        res = verify_records(recs)
        self.assertEqual(res.error, CONTENT_MODIFIED)
        self.assertIn(res.position, (7, 8))  # 在第7/8条之间检出链断裂

    # 6. 复制整条记录到别处（重复条目 => 插入）
    def test_record_copied_elsewhere(self):
        recs = build_records(15)
        recs.insert(10, copy.deepcopy(recs[2]))  # 把第2条复制到位置10
        res = verify_records(recs)
        self.assertEqual(res.error, ENTRY_INSERTED)
        self.assertEqual(res.position, 10)

    # 7. 条目被插入（攻击者伪造一条并接好 prev）
    def test_entry_inserted(self):
        recs = build_records(15)
        forged = make_record(7, bytes.fromhex(recs[6]["digest"]), {"op": "evil"})
        recs.insert(7, forged)
        res = verify_records(recs)
        self.assertEqual(res.error, ENTRY_INSERTED)
        self.assertIn(res.position, (7, 8))

    # 8. 条目被删除
    def test_entry_deleted(self):
        recs = build_records(15)
        del recs[9]
        res = verify_records(recs)
        self.assertEqual(res.error, ENTRY_DELETED)
        self.assertEqual(res.position, 9)

    # 9. 链尾被截断（需可信链头才能检出）
    def test_tail_truncated(self):
        recs = build_records(20)
        true_head = recs[-1]["digest"]
        cut = recs[:14]
        # 不提供可信链头：截断后的链内部自洽，无法检出
        self.assertTrue(verify_records(cut).ok)
        # 提供可信链头：检出截断，位置 = 现存长度
        res = verify_records(cut, expected_head=true_head)
        self.assertEqual(res.error, TRUNCATED)
        self.assertEqual(res.position, 14)

    # 10. 两条链拼接
    def test_two_chains_concatenated(self):
        a = build_records(8, tag="a")
        b = build_records(6, tag="b")  # 独立链，seq 从 0 开始
        res = verify_records(a + b)
        self.assertEqual(res.error, ENTRY_INSERTED)
        self.assertEqual(res.position, 8)  # 拼接点即首处不一致

    # 11. 从任意位置增量校验
    def test_incremental_verify_from(self):
        recs = build_records(100)
        for start in (0, 1, 50, 99):
            known = (bytes.fromhex(recs[start - 1]["digest"])
                     if start > 0 else GENESIS)
            res = verify_records(recs, start=start, known_prev=known)
            self.assertTrue(res.ok, f"start={start}: {res}")
            self.assertEqual(res.checked, 100 - start)
        # 增量校验同样能检出后半段的篡改
        recs[80] = dict(recs[80], content={"op": "bad"})
        known = bytes.fromhex(recs[49]["digest"])
        res = verify_records(recs, start=50, known_prev=known)
        self.assertEqual(res.error, CONTENT_MODIFIED)
        self.assertEqual(res.position, 80)

    # 12. 检查点工作流：追加 -> 保存检查点 -> 之后用检查点检出截断
    def test_checkpoint_workflow(self):
        log = AuditLog(self.path)
        for i in range(30):
            log.append({"i": i})
        length, head = log.checkpoint()
        self.assertEqual(length, 30)
        self.assertTrue(log.verify(expected_head=head).ok)
        # 模拟链尾被截断后重新加载
        with open(self.path, "rb") as f:
            lines = f.readlines()
        with open(self.path, "wb") as f:
            f.writelines(lines[:25])
        log2 = AuditLog(self.path)
        res = log2.verify(expected_head=head)
        self.assertEqual(res.error, TRUNCATED)
        self.assertEqual(res.position, 25)

    # 13. 序列化可重现：同一内容多次编码字节一致
    def test_serialization_deterministic(self):
        a = make_record(0, GENESIS, {"k2": 1, "k1": {"y": [3, 1], "x": "z"}})
        b = make_record(0, GENESIS, {"k1": {"x": "z", "y": [3, 1]}, "k2": 1})
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(a["digest"], b["digest"])

    # 14. 检查点 (条数, 链头)：区分截断与检查点之后的正常变长
    def test_checkpoint_distinguishes_growth_from_truncation(self):
        log = AuditLog(self.path)
        for i in range(10):
            log.append({"i": i})
        length, head = log.checkpoint()
        # 检查点之后正常追加两条：不得误报截断
        log.append({"i": 10})
        log.append({"i": 11})
        res = log.verify(expected_head=head, expected_len=length)
        self.assertTrue(res.ok, res)
        self.assertEqual(res.checked, 12)
        # 真截断（现存 8 条 < 检查点 10 条）：报 truncated，位置 = 现存长度
        with open(self.path, "rb") as f:
            lines = f.readlines()
        with open(self.path, "wb") as f:
            f.writelines(lines[:8])
        res2 = AuditLog(self.path).verify(expected_head=head, expected_len=length)
        self.assertEqual(res2.error, TRUNCATED)
        self.assertEqual(res2.position, 8)
        # 检查点之前的历史被重算改写（条数不变）：报 content_modified，
        # 位置 = 检查点锚定的那一条
        recs = []
        prev = GENESIS
        for i in range(12):
            r = make_record(i, prev, {"op": f"evil{i}"})
            recs.append(r)
            prev = bytes.fromhex(r["digest"])
        res3 = verify_records(recs, expected_head=head, expected_len=length)
        self.assertEqual(res3.error, CONTENT_MODIFIED)
        self.assertEqual(res3.position, 9)

    # 15. 崩溃残留的残缺末行：加载不抛异常，verify 在尾部位置报 content_modified
    def test_torn_last_line_reports_content_modified(self):
        log = AuditLog(self.path)
        for i in range(5):
            log.append({"i": i})
        # 模拟崩溃：一次行写入只完成一半
        with open(self.path, "ab") as f:
            f.write(b'{"seq":5,"prev":"ab')
        log2 = AuditLog(self.path)  # 加载不抛 JSONDecodeError
        res = log2.verify()
        self.assertFalse(res.ok)
        self.assertEqual(res.error, CONTENT_MODIFIED)
        self.assertEqual(res.position, 5)   # 尾部位置（第 6 行，索引 5）
        self.assertIn("第 5 行", res.detail)
        # 按文档恢复：去掉残缺末行后链即恢复一致
        with open(self.path, "rb") as f:
            lines = f.readlines()
        with open(self.path, "wb") as f:
            f.writelines(lines[:5])
        self.assertTrue(AuditLog(self.path).verify().ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
