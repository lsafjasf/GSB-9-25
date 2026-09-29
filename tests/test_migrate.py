"""迁移工具测试：读入一致性检查、批量迁移、对拍、中断重入、失败重试。"""
import json
import os
import shutil
import struct
import tempfile
import unittest

from src import codec, migrate
from src.codec import FIELDS, MISSING_KEY, UNKNOWN_KEY

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), "rb") as fh:
        return fh.read()


def _v0_bytes():
    return _fixture("user_v0_no_email.bin")


class MigrateTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="migrate_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _put(self, name, data):
        with open(os.path.join(self.dir, name), "wb") as fh:
            fh.write(data)

    def _get(self, name):
        with open(os.path.join(self.dir, name), "rb") as fh:
            return fh.read()

    def _populate(self):
        """混合线上存量：v0、v1 完整、v1 含未知字段、坏 magic、截断、非法版本。"""
        self._put("a_v0.bin", _v0_bytes())
        self._put("b_v1_full.bin", _fixture("user_v1_full.bin"))
        self._put("c_v1_unknown.bin", _fixture("user_v1_with_unknown.bin"))
        self._put("d_bad_magic.bin", b"XX\x01\x00\x00")
        self._put("e_truncated.bin", _v0_bytes()[:-3])
        self._put("f_bad_version.bin", b"RC\x63\x00\x00")


class CheckRecordTest(MigrateTestBase):
    def test_check_accepts_v0_and_v1(self):
        self.assertEqual(migrate.check_record(_v0_bytes()), 0)
        self.assertEqual(migrate.check_record(_fixture("user_v1_full.bin")), 1)

    def test_check_failure_reasons(self):
        with self.assertRaises(migrate.CheckError):
            migrate.check_record(b"XX\x01\x00\x00")          # bad magic
        with self.assertRaises(migrate.CheckError):
            migrate.check_record(b"RC")                       # 头部不足
        with self.assertRaises(migrate.CheckError):
            migrate.check_record(b"RC\x63\x00\x00")           # 版本不支持
        with self.assertRaises(migrate.CheckError):
            migrate.check_record(_v0_bytes()[:-3])            # 截断
        with self.assertRaises(migrate.CheckError):
            migrate.check_record(_v0_bytes() + b"\x00")       # 尾部脏字节


class PrecheckTest(MigrateTestBase):
    def test_check_only_does_not_write(self):
        self._populate()
        before = {n: self._get(n) for n in os.listdir(self.dir) if n.endswith(".bin")}
        report = migrate.run(self.dir, check_only=True)
        self.assertEqual(report["migrated_count"], 1)   # 仅 a_v0 待迁移
        self.assertEqual(report["skipped_count"], 2)    # 两条 v1
        self.assertEqual(report["failed_count"], 3)     # 三条坏数据
        for n, data in before.items():                  # 一个字节都不动
            self.assertEqual(self._get(n), data)

    def test_failed_records_have_reasons(self):
        self._populate()
        report = migrate.run(self.dir, check_only=True)
        for name in ("d_bad_magic.bin", "e_truncated.bin", "f_bad_version.bin"):
            self.assertIn(name, report["failed"])
            self.assertTrue(report["failed"][name])


class MigrateRunTest(MigrateTestBase):
    def test_migrate_upgrades_v0_and_bumps_version(self):
        self._populate()
        report = migrate.run(self.dir)
        self.assertEqual(report["migrated"], ["a_v0.bin"])
        self.assertEqual(report["skipped_count"], 2)
        self.assertEqual(report["failed_count"], 3)
        data = self._get("a_v0.bin")
        self.assertEqual(data[2], codec.VERSION)  # 版本号已升级
        record = codec.decode_record(data)
        self.assertEqual(record[MISSING_KEY], [])  # 缺失字段已显式补齐
        self.assertEqual(record["email"], "")

    def test_post_migration_read_matches_pre_migration(self):
        """对拍：迁移后新代码读入结果与迁移前一致（业务字段+未知字段）。"""
        self._populate()
        before = codec.decode_record(self._get("a_v0.bin"))
        migrate.run(self.dir)
        after = codec.decode_record(self._get("a_v0.bin"))
        for field in FIELDS:
            self.assertEqual(after[field.name], before[field.name], field.name)
        self.assertEqual(after[UNKNOWN_KEY], before[UNKNOWN_KEY])

    def test_unknown_fields_survive_migration(self):
        # v0 记录里混入未知字段，迁移后必须原样保留
        v0 = bytearray(_v0_bytes())
        (count,) = struct.unpack(">H", v0[3:5])
        v0[3:5] = struct.pack(">H", count + 1)
        v0 += bytes([0x63, 0x05]) + struct.pack(">H", 5) + b"extra"
        self._put("g_v0_unknown.bin", bytes(v0))
        report = migrate.run(self.dir)
        self.assertEqual(report["migrated"], ["g_v0_unknown.bin"])
        record = codec.decode_record(self._get("g_v0_unknown.bin"))
        self.assertEqual(record[UNKNOWN_KEY], [(0x63, 0x05, b"extra")])

    def test_interrupt_and_resume(self):
        """--limit 模拟中断：分多轮跑完，结果与一次跑完一致，且可重入。"""
        for i in range(3):
            self._put("v0_%d.bin" % i, _v0_bytes())
        first = migrate.run(self.dir, limit=2)
        self.assertEqual(first["migrated_count"], 2)
        second = migrate.run(self.dir, limit=2)  # 续跑：剩余 1 条
        self.assertEqual(second["migrated_count"], 1)
        third = migrate.run(self.dir)            # 重入：全部跳过
        self.assertEqual(third["migrated_count"], 0)
        self.assertEqual(third["skipped_count"], 3)
        for i in range(3):
            self.assertEqual(self._get("v0_%d.bin" % i)[2], codec.VERSION)

    def test_rerun_is_idempotent(self):
        self._populate()
        migrate.run(self.dir)
        snapshot = {n: self._get(n) for n in os.listdir(self.dir) if n.endswith(".bin")}
        report = migrate.run(self.dir)
        self.assertEqual(report["migrated_count"], 0)
        for n, data in snapshot.items():
            self.assertEqual(self._get(n), data)

    def test_retry_single_failed_record(self):
        self._populate()
        report = migrate.run(self.dir)
        self.assertIn("d_bad_magic.bin", report["failed"])
        # “运维修复数据”后用 --only 单独重试
        self._put("d_bad_magic.bin", _v0_bytes())
        retry = migrate.run(self.dir, only=["d_bad_magic.bin"])
        self.assertEqual(retry["migrated"], ["d_bad_magic.bin"])
        self.assertEqual(self._get("d_bad_magic.bin")[2], codec.VERSION)
        # 其他失败记录本轮未覆盖，原因保留在报告中
        self.assertIn("e_truncated.bin", retry["failed"])

    def test_report_file_written(self):
        self._populate()
        migrate.run(self.dir)
        with open(os.path.join(self.dir, migrate.REPORT_NAME), encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["migrated_count"], 1)
        self.assertEqual(report["failed_count"], 3)


if __name__ == "__main__":
    unittest.main()
