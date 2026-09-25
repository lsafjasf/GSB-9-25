"""Self-tests for appendlog: round-trip, ack semantics, corruption taxonomy."""

import os
import struct
import tempfile
import unittest
import zlib

import appendlog as al


def rec(payload: bytes) -> bytes:
    return al.encode_record(payload)


def corrupt_crc_zero(payload: bytes) -> bytes:
    """Record whose CRC field is forced to all zeros."""
    return al.HEADER_STRUCT.pack(al.MAGIC, len(payload), 0) + payload


def tampered_length(payload: bytes, new_len: int) -> bytes:
    """Record whose length field is replaced with a bogus value."""
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return al.HEADER_STRUCT.pack(al.MAGIC, new_len, crc) + payload


def flip_payload_byte(payload: bytes) -> bytes:
    """Valid header, but one payload byte flipped -> CRC must fail."""
    bad = bytearray(payload)
    bad[len(bad) // 2] ^= 0xFF
    return al.HEADER_STRUCT.pack(
        al.MAGIC, len(payload), zlib.crc32(payload) & 0xFFFFFFFF
    ) + bytes(bad)


class TempLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.log")

    def tearDown(self):
        self.dir.cleanup()

    def write_bytes(self, data: bytes) -> None:
        with open(self.path, "wb") as f:
            f.write(data)

    def scan_skip(self):
        return al.LogReader(self.path, mode=al.SKIP).scan()


class TestWriterAck(TempLogTest):
    def test_buffered_vs_durable_ack(self):
        with al.LogWriter(self.path) as w:
            self.assertEqual(w.durable_upto, 0)
            ack1 = w.append_batch([b"aaa", b"bbb"])
            # Buffered advanced, durable still at the open-time position.
            self.assertEqual(ack1.buffered_upto, 2 * (al.HEADER_SIZE + 3))
            self.assertEqual(ack1.durable_upto, 0)
            self.assertEqual(w.buffered_upto, ack1.buffered_upto)

            durable = w.sync()
            self.assertEqual(durable, ack1.buffered_upto)
            self.assertEqual(w.durable_upto, ack1.buffered_upto)

            ack2 = w.append_batch([b"ccc"])
            self.assertEqual(ack2.durable_upto, ack1.buffered_upto)
            self.assertGreater(ack2.buffered_upto, ack2.durable_upto)
            w.sync()
            self.assertEqual(w.durable_upto, ack2.buffered_upto)

    def test_reopen_tracks_existing_size(self):
        with al.LogWriter(self.path) as w:
            w.append_batch([b"x" * 10])
            w.sync()
        size = os.path.getsize(self.path)
        with al.LogWriter(self.path) as w:
            self.assertEqual(w.buffered_upto, size)
            self.assertEqual(w.durable_upto, size)

    def test_batch_roundtrip(self):
        payloads = [os.urandom(n) for n in (0, 1, 7, 100, 4096)]
        with al.LogWriter(self.path) as w:
            w.append_batch(payloads)
            w.sync()
        got = [r.payload for r in al.LogReader(self.path).iter_records()]
        self.assertEqual(got, payloads)

    def test_record_offsets(self):
        payloads = [b"aa", b"bbbb", b"c"]
        with al.LogWriter(self.path) as w:
            w.append_batch(payloads)
            w.sync()
        offsets = [r.offset for r in al.LogReader(self.path).iter_records()]
        expected = [0]
        for p in payloads[:-1]:
            expected.append(expected[-1] + al.HEADER_SIZE + len(p))
        self.assertEqual(offsets, expected)


class TestCleanFiles(TempLogTest):
    def test_empty_file(self):
        self.write_bytes(b"")
        result = self.scan_skip()
        self.assertTrue(result.ok)
        self.assertEqual(result.records, [])
        self.assertEqual(list(al.LogReader(self.path).iter_records()), [])

    def test_single_record(self):
        self.write_bytes(rec(b"hello"))
        result = self.scan_skip()
        self.assertTrue(result.ok)
        self.assertEqual([r.payload for r in result.records], [b"hello"])


class TestTruncation(TempLogTest):
    def test_only_half_record_header(self):
        # Torn write: only part of a header made it to disk.
        self.write_bytes(rec(b"complete") + rec(b"partial")[:5])
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"complete"])
        self.assertEqual(len(result.corruptions), 1)
        c = result.corruptions[0]
        self.assertEqual(c.kind, al.TRUNCATED)
        self.assertEqual(c.offset, al.HEADER_SIZE + 8)
        self.assertEqual(c.end, os.path.getsize(self.path))
        self.assertIn("torn write", c.reason)

    def test_only_half_record_payload(self):
        # Header complete, payload cut short.
        full = rec(b"x" * 100)
        self.write_bytes(full[: al.HEADER_SIZE + 40])
        result = self.scan_skip()
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.corruptions), 1)
        self.assertEqual(result.corruptions[0].kind, al.TRUNCATED)
        self.assertEqual(result.corruptions[0].offset, 0)

    def test_file_with_only_half_record(self):
        self.write_bytes(rec(b"data")[:7])
        result = self.scan_skip()
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.corruptions), 1)
        self.assertEqual(result.corruptions[0].kind, al.TRUNCATED)


class TestChecksum(TempLogTest):
    def test_flipped_payload_byte_detected(self):
        payload = b"important-payload" * 4
        blob = rec(b"before") + flip_payload_byte(payload) + rec(b"after")
        self.write_bytes(blob)
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"before", b"after"])
        self.assertEqual(len(result.corruptions), 1)
        c = result.corruptions[0]
        self.assertEqual(c.kind, al.CHECKSUM_MISMATCH)
        self.assertEqual(c.offset, al.HEADER_SIZE + 6)
        self.assertEqual(c.size, al.HEADER_SIZE + len(payload))

    def test_crc_field_all_zeros_detected(self):
        payload = b"payload-with-nonzero-crc"
        assert zlib.crc32(payload) != 0
        self.write_bytes(rec(b"ok1") + corrupt_crc_zero(payload) + rec(b"ok2"))
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"ok1", b"ok2"])
        self.assertEqual(len(result.corruptions), 1)
        c = result.corruptions[0]
        self.assertEqual(c.kind, al.CHECKSUM_MISMATCH)
        self.assertIn("0x00000000", c.reason)

    def test_length_smaller_than_actual_detected(self):
        # Length field claims fewer bytes than were really written: the CRC
        # computed over the truncated slice cannot match.
        payload = b"real-payload-here"
        blob = tampered_length(payload, 4) + rec(b"next")
        self.write_bytes(blob)
        result = self.scan_skip()
        kinds = [c.kind for c in result.corruptions]
        self.assertIn(al.CHECKSUM_MISMATCH, kinds)
        # The misaligned leftover bytes are reported too (bad magic region),
        # and the trailing good record is still recovered.
        self.assertIn(al.BAD_MAGIC, kinds)
        self.assertEqual(result.records[-1].payload, b"next")


class TestInvalidLength(TempLogTest):
    def test_huge_length_field(self):
        blob = rec(b"good") + tampered_length(b"payload", al.MAX_RECORD_SIZE + 1)
        blob += rec(b"recovered")
        self.write_bytes(blob)
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"good", b"recovered"])
        self.assertEqual(len(result.corruptions), 1)
        c = result.corruptions[0]
        self.assertEqual(c.kind, al.INVALID_LENGTH)
        self.assertEqual(c.offset, al.HEADER_SIZE + 4)
        # Skipped region ends exactly where the next record begins.
        self.assertEqual(c.end, os.path.getsize(self.path) - (al.HEADER_SIZE + 9))

    def test_garbage_bytes_between_records(self):
        blob = rec(b"one") + b"\xde\xad\xbe\xef garbage \x00" + rec(b"two")
        self.write_bytes(blob)
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"one", b"two"])
        self.assertEqual(len(result.corruptions), 1)
        c = result.corruptions[0]
        self.assertEqual(c.kind, al.BAD_MAGIC)
        self.assertEqual(c.offset, al.HEADER_SIZE + 3)
        self.assertEqual(c.size, len(b"\xde\xad\xbe\xef garbage \x00"))


class TestMultipleCorruptions(TempLogTest):
    def test_consecutive_damaged_regions(self):
        payload = b"victim-payload"
        blob = b"".join(
            [
                rec(b"r1"),
                flip_payload_byte(payload),                    # checksum
                b"\x01\x02\x03 garbage",                        # bad magic
                tampered_length(b"x", al.MAX_RECORD_SIZE + 5),  # invalid length
                rec(b"r2"),
                rec(b"r3"),
                rec(b"torn")[:6],                               # truncated tail
            ]
        )
        self.write_bytes(blob)
        result = self.scan_skip()
        self.assertEqual([r.payload for r in result.records], [b"r1", b"r2", b"r3"])
        kinds = [c.kind for c in result.corruptions]
        self.assertEqual(
            kinds,
            [al.CHECKSUM_MISMATCH, al.BAD_MAGIC, al.INVALID_LENGTH, al.TRUNCATED],
        )
        # Offsets are monotonically increasing and inside the file.
        size = os.path.getsize(self.path)
        for prev, cur in zip(result.corruptions, result.corruptions[1:]):
            self.assertLessEqual(prev.offset, cur.offset)
        for c in result.corruptions:
            self.assertGreaterEqual(c.offset, 0)
            self.assertLessEqual(c.end, size)
            self.assertGreater(c.end, c.offset)


class TestStrictMode(TempLogTest):
    def test_strict_raises_with_location(self):
        blob = rec(b"good") + flip_payload_byte(b"bad-payload") + rec(b"unreached")
        self.write_bytes(blob)
        reader = al.LogReader(self.path, mode=al.STRICT)
        with self.assertRaises(al.CorruptionError) as ctx:
            list(reader.iter_records())
        c = ctx.exception.corruption
        self.assertEqual(c.kind, al.CHECKSUM_MISMATCH)
        self.assertEqual(c.offset, al.HEADER_SIZE + 4)
        self.assertIn(str(c.offset), str(ctx.exception))

    def test_strict_stops_at_first_corruption(self):
        blob = (
            flip_payload_byte(b"first-bad")
            + tampered_length(b"y", al.MAX_RECORD_SIZE + 1)
            + rec(b"tail")[:3]
        )
        self.write_bytes(blob)
        with self.assertRaises(al.CorruptionError) as ctx:
            al.LogReader(self.path, mode=al.STRICT).scan()
        self.assertEqual(ctx.exception.corruption.kind, al.CHECKSUM_MISMATCH)

    def test_strict_clean_file_no_error(self):
        self.write_bytes(rec(b"a") + rec(b"b"))
        result = al.LogReader(self.path, mode=al.STRICT).scan()
        self.assertTrue(result.ok)
        self.assertEqual(len(result.records), 2)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            al.LogReader(self.path, mode="yolo")


class TestNoFalsePositives(TempLogTest):
    """Corruption must never be silently accepted as a valid record."""

    def _assert_not_returned(self, blob: bytes, poison: bytes):
        self.write_bytes(blob)
        result = self.scan_skip()
        self.assertFalse(result.ok, "corruption was not detected at all")
        for r in result.records:
            self.assertNotEqual(r.payload, poison)

    def test_tampered_length_never_valid(self):
        poison = b"tampered"
        self._assert_not_returned(
            tampered_length(poison, al.MAX_RECORD_SIZE + 1), poison
        )

    def test_flipped_content_never_valid(self):
        poison = b"flipped-content"
        self._assert_not_returned(flip_payload_byte(poison), poison)

    def test_zero_crc_never_valid(self):
        poison = b"zero-crc-payload"
        self._assert_not_returned(corrupt_crc_zero(poison), poison)

    def test_short_length_never_valid(self):
        poison = b"length-mismatch"
        self._assert_not_returned(tampered_length(poison, 2), poison)


if __name__ == "__main__":
    unittest.main(verbosity=2)
