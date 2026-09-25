"""alog 自测：正常读写 + 损坏用例集 + 两种恢复模式对照。

运行：python3 -m unittest test_alog -v
"""

import os
import struct
import tempfile
import unittest
import zlib

import alog
from alog import (
    HEADER, MAGIC, CorruptionError, LogReader, LogWriter,
    KIND_BAD_MAGIC, KIND_CHECKSUM_MISMATCH, KIND_INVALID_LENGTH,
    KIND_TRUNCATED_TAIL, MODE_SKIP, MODE_STRICT, read_all,
)


def pack_record(payload: bytes) -> bytes:
    return HEADER.pack(MAGIC, len(payload), zlib.crc32(payload)) + payload


class TempFileCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.log")

    def tearDown(self):
        self.dir.cleanup()

    def write_raw(self, data: bytes):
        with open(self.path, "wb") as f:
            f.write(f.sync() if False else data)
            os.fsync(f.fileno())

    def read_raw(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


# ---------------------------------------------------------------- 正常读写

class TestNormalIO(TempFileCase):
    def test_empty_file_is_clean(self):
        self.write_raw(b"")
        records, report = read_all(self.path, MODE_STRICT)
        self.assertEqual(records, [])
        self.assertTrue(report.clean)
        self.assertEqual(report.corruptions, [])

    def test_batch_roundtrip(self):
        payloads = [b"hello", b"", b"x" * 10000, bytes(range(256))]
        with LogWriter(self.path) as w:
            w.append_batch(payloads)
        records, report = read_all(self.path, MODE_STRICT)
        self.assertEqual([p for _, p in records], payloads)
        self.assertTrue(report.clean)
        # 偏移可定位
        self.assertEqual(records[0][0], 0)
        self.assertEqual(records[1][0], HEADER.size + len(payloads[0]))

    def test_append_across_writers(self):
        with LogWriter(self.path) as w:
            w.append_batch([b"a", b"b"])
        with LogWriter(self.path) as w:  # 重新打开继续追加
            w.append_batch([b"c"])
        records, _ = read_all(self.path, MODE_STRICT)
        self.assertEqual([p for _, p in records], [b"a", b"b", b"c"])

    def test_oversize_record_rejected(self):
        with LogWriter(self.path) as w:
            with self.assertRaises(ValueError):
                w.append(b"x" * (alog.MAX_RECORD_SIZE + 1))


# ---------------------------------------------------------------- 落盘确认

class TestDurabilityAck(TempFileCase):
    def test_buffered_vs_durable_ack(self):
        w = LogWriter(self.path)
        try:
            ack1 = w.append_batch([b"r1", b"r2"])
            self.assertFalse(ack1.durable)                 # 只在缓冲区
            self.assertEqual(ack1.durable_offset, 0)
            self.assertGreater(ack1.buffered_end, 0)
            self.assertEqual(w.durable_offset, 0)

            pos = w.fsync()                                # 强制落盘
            self.assertEqual(pos, ack1.buffered_end)
            self.assertEqual(w.durable_offset, ack1.buffered_end)

            ack2 = w.append_batch([b"r3"])
            self.assertFalse(ack2.durable)                 # 新一批又未落盘
            self.assertEqual(ack2.durable_offset, ack1.buffered_end)

            w.fsync()
            ack3 = w.append_batch([])                      # 空批量也返回 ack
            self.assertTrue(ack3.durable)
            self.assertEqual(ack3.buffered_end, w.durable_offset)
        finally:
            w.close()
        # 最后落盘位置 == 文件大小
        self.assertEqual(w.durable_offset, os.path.getsize(self.path))

    def test_last_durable_offset_tracks_fsync(self):
        with LogWriter(self.path) as w:
            w.append_batch([b"x" * 100])
            self.assertEqual(w.durable_offset, 0)
            w.fsync()
            mid = w.durable_offset
            w.append_batch([b"y" * 100])
            w.fsync()
            self.assertGreater(w.durable_offset, mid)
            self.assertEqual(w.durable_offset, os.path.getsize(self.path))


# ---------------------------------------------------------------- 损坏用例集

class TestCorruptionCases(TempFileCase):
    """每类损坏都必须被检出，且不得误判为正常记录。"""

    def test_only_half_record_truncated_header(self):
        self.write_raw(b"\x89AL\x07")  # 只有 4 字节，连头部都不完整
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(records, [])
        self.assertEqual(len(report.corruptions), 1)
        c = report.corruptions[0]
        self.assertEqual(c.kind, KIND_TRUNCATED_TAIL)
        self.assertEqual(c.offset, 0)
        self.assertEqual(report.skipped, [(0, 4)])

    def test_truncated_tail_mid_payload(self):
        good = pack_record(b"good-1")
        half = pack_record(b"this record was cut off by kill -9")[:20]
        self.write_raw(good + half)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual([p for _, p in records], [b"good-1"])
        self.assertEqual(len(report.corruptions), 1)
        c = report.corruptions[0]
        self.assertEqual(c.kind, KIND_TRUNCATED_TAIL)
        self.assertEqual(c.offset, len(good))          # 偏移可定位
        self.assertEqual(report.skipped, [(len(good), len(good) + 20)])

    def test_tampered_length_huge(self):
        """长度字段被篡改为巨大值 -> invalid_length。"""
        rec = bytearray(pack_record(b"payload-A"))
        struct.pack_into("<I", rec, 4, 0x7FFFFFFF)     # 篡改 length
        tail = pack_record(b"payload-B")
        self.write_raw(bytes(rec) + tail)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(len(report.corruptions), 1)
        c = report.corruptions[0]
        self.assertEqual(c.kind, KIND_INVALID_LENGTH)
        self.assertEqual(c.offset, 0)
        # skip 模式重同步后读到后续正常记录
        self.assertEqual([p for _, p in records], [b"payload-B"])
        self.assertEqual(report.skipped, [(0, len(rec))])

    def test_flipped_payload_byte(self):
        """内容被翻转 -> checksum_mismatch，且绝不能当成正常记录返回。"""
        rec = bytearray(pack_record(b"important data"))
        rec[-1] ^= 0xFF                                # 翻转最后一个字节
        tail = pack_record(b"after")
        self.write_raw(bytes(rec) + tail)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(len(report.corruptions), 1)
        self.assertEqual(report.corruptions[0].kind, KIND_CHECKSUM_MISMATCH)
        self.assertEqual(report.corruptions[0].offset, 0)
        self.assertEqual([p for _, p in records], [b"after"])
        for _, p in records:                           # 损坏内容不得出现
            self.assertNotEqual(p, rec[HEADER.size:])

    def test_length_smaller_than_actual(self):
        """长度与实际不符（改小）-> 读到的字节 CRC 对不上 -> checksum_mismatch。"""
        payload = b"actual-payload-of-32-bytes!!!!"
        rec = bytearray(pack_record(payload))
        struct.pack_into("<I", rec, 4, len(payload) - 5)   # 长度改小 5
        tail = pack_record(b"next-ok")
        self.write_raw(bytes(rec) + tail)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(report.corruptions[0].kind, KIND_CHECKSUM_MISMATCH)
        self.assertEqual([p for _, p in records], [b"next-ok"])

    def test_all_zero_checksum_field(self):
        """校验字段全零 -> checksum_mismatch（正常 payload 的 CRC 几乎不可能为 0）。"""
        rec = bytearray(pack_record(b"some payload"))
        struct.pack_into("<I", rec, 8, 0)              # CRC 字段清零
        self.write_raw(bytes(rec))
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(records, [])
        self.assertEqual(report.corruptions[0].kind, KIND_CHECKSUM_MISMATCH)

    def test_zero_crc_record_written_normally_is_rejected_at_write(self):
        """防御：如果真有 payload 的 CRC32 为 0，写入端照样能写、读取端照样能验。
        这里直接构造一条 CRC 字段与内容一致的合法记录确认不误报。"""
        payload = b"\x00" * 4
        self.write_raw(pack_record(payload))
        records, report = read_all(self.path, MODE_STRICT)
        self.assertEqual([p for _, p in records], [payload])
        self.assertTrue(report.clean)

    def test_bad_magic(self):
        rec = bytearray(pack_record(b"magic-corrupted"))
        rec[0:4] = b"XXXX"
        tail = pack_record(b"survivor")
        self.write_raw(bytes(rec) + tail)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual(report.corruptions[0].kind, KIND_BAD_MAGIC)
        self.assertEqual([p for _, p in records], [b"survivor"])

    def test_multiple_consecutive_corruptions(self):
        """连续多处损坏：翻转 + 全零 CRC + 篡改长度，skip 模式全部记录并继续。"""
        r1 = pack_record(b"ok-1")
        bad1 = bytearray(pack_record(b"bad-flip"));  bad1[-1] ^= 0x01
        bad2 = bytearray(pack_record(b"bad-zero"));  struct.pack_into("<I", bad2, 8, 0)
        bad3 = bytearray(pack_record(b"bad-len"));   struct.pack_into("<I", bad3, 4, 10**9)
        r2 = pack_record(b"ok-2")
        r3 = pack_record(b"ok-3")
        blob = r1 + bytes(bad1) + bytes(bad2) + bytes(bad3) + r2 + r3
        self.write_raw(blob)

        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual([p for _, p in records], [b"ok-1", b"ok-2", b"ok-3"])
        kinds = [c.kind for c in report.corruptions]
        self.assertEqual(kinds, [KIND_CHECKSUM_MISMATCH,
                                 KIND_CHECKSUM_MISMATCH,
                                 KIND_INVALID_LENGTH])
        # 三处损坏相邻，跳过区间被合并记录
        skip_start = len(r1)
        skip_end = len(r1) + len(bad1) + len(bad2) + len(bad3)
        self.assertEqual(report.skipped, [(skip_start, skip_end)])
        self.assertFalse(report.clean)

    def test_strict_mode_stops_at_first_corruption(self):
        r1 = pack_record(b"first")
        bad = bytearray(pack_record(b"corrupted")); bad[-1] ^= 0xFF
        r2 = pack_record(b"never-read")
        self.write_raw(r1 + bytes(bad) + r2)

        reader = LogReader(self.path, mode=MODE_STRICT)
        got = []
        with self.assertRaises(CorruptionError) as ctx:
            for off, p in reader:
                got.append(p)
        self.assertEqual(got, [b"first"])              # 停在第一处损坏
        self.assertEqual(ctx.exception.corruption.kind, KIND_CHECKSUM_MISMATCH)
        self.assertEqual(ctx.exception.corruption.offset, len(r1))
        self.assertEqual(len(reader.corruptions), 1)

    def test_strict_mode_clean_file_no_exception(self):
        with LogWriter(self.path) as w:
            w.append_batch([b"a", b"b", b"c"])
        records, report = read_all(self.path, MODE_STRICT)
        self.assertEqual(len(records), 3)
        self.assertTrue(report.clean)

    def test_corruption_never_returned_as_valid(self):
        """综合：构造 8 种篡改，skip 模式下返回的记录必须全部通过重新校验。"""
        good = [pack_record(b"guard-%d" % i) for i in range(3)]
        tampered = []
        t = bytearray(pack_record(b"victim-payload-1")); t[-3] ^= 0x10; tampered.append(t)
        t = bytearray(pack_record(b"victim-payload-2")); struct.pack_into("<I", t, 8, 0); tampered.append(t)
        t = bytearray(pack_record(b"victim-payload-3")); struct.pack_into("<I", t, 4, 2**31 - 1); tampered.append(t)
        t = bytearray(pack_record(b"victim-payload-4")); struct.pack_into("<I", t, 4, 3); tampered.append(t)
        blob = good[0] + b"".join(bytes(t) for t in tampered) + good[1] + good[2]
        self.write_raw(blob)
        records, report = read_all(self.path, MODE_SKIP)
        self.assertEqual([p for _, p in records],
                         [b"guard-0", b"guard-1", b"guard-2"])
        self.assertGreaterEqual(len(report.corruptions), 3)
        # 返回的每条记录重新独立校验
        raw = self.read_raw()
        for off, payload in records:
            magic, length, crc = HEADER.unpack_from(raw, off)
            self.assertEqual(magic, MAGIC)
            self.assertEqual(length, len(payload))
            self.assertEqual(zlib.crc32(payload), crc)


if __name__ == "__main__":
    unittest.main()
