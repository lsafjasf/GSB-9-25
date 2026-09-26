"""Self-tests for filededup. Run: python3 -m unittest discover -s tests -v"""

import hashlib
import io
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from filededup import Chunker, DedupIndex, scan_file
from filededup.cdc import READ_SIZE

# small chunk geometry so tests stay fast
MIN_S, AVG_S, MAX_S = 256, 1024, 4096


def make_chunker():
    return Chunker(MIN_S, AVG_S, MAX_S)


def chunk_bytes(data, chunker=None):
    c = chunker or make_chunker()
    return [ch for _, ch in c.iter_chunks(io.BytesIO(data))]


from filededup.scanner import chunk_id


def chunk_ids(data, chunker=None):
    return [chunk_id(ch) for ch in chunk_bytes(data, chunker)]


class RawStream(io.RawIOBase):
    """File-like object exposing only read(n); proves streaming works."""

    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.read_sizes = []

    def readable(self):
        return True

    def read(self, n=-1):
        self.read_sizes.append(n)
        return self._buf.read(n)


class TestChunking(unittest.TestCase):
    def test_empty_file_yields_no_chunks(self):
        self.assertEqual(chunk_bytes(b""), [])

    def test_tiny_file_is_single_chunk(self):
        self.assertEqual(chunk_bytes(b"hello world"), [b"hello world"])
        data = os.urandom(MIN_S - 1)
        self.assertEqual(chunk_bytes(data), [data])

    def test_chunks_reassemble_exactly(self):
        rng = random.Random(1)
        for size in (0, 1, MIN_S, AVG_S, MAX_S, MAX_S + 1, 300_000):
            data = rng.randbytes(size)
            chunks = chunk_bytes(data)
            self.assertEqual(b"".join(chunks), data, f"size={size}")

    def test_chunk_size_bounds(self):
        data = random.Random(2).randbytes(2_000_000)
        chunks = chunk_bytes(data)
        for ch in chunks[:-1]:  # last chunk may be a short remainder
            self.assertGreaterEqual(len(ch), MIN_S)
            self.assertLessEqual(len(ch), MAX_S)
        avg = sum(map(len, chunks)) / len(chunks)
        self.assertTrue(AVG_S / 2 < avg < AVG_S * 2, f"avg={avg:.0f}")

    def test_deterministic_boundaries(self):
        data = random.Random(3).randbytes(500_000)
        self.assertEqual(chunk_ids(data), chunk_ids(data))

    def test_streaming_across_read_boundaries(self):
        # chunking must not depend on how reads are sliced
        data = random.Random(4).randbytes(3 * READ_SIZE + 12345)
        whole = chunk_ids(data)
        for read_size in (1, 7, 4096, READ_SIZE):
            c = make_chunker()
            got = chunk_ids(data, c) if read_size == READ_SIZE else [
                chunk_id(ch)
                for _, ch in c.iter_chunks(io.BytesIO(data), read_size=read_size)
            ]
            self.assertEqual(got, whole, f"read_size={read_size}")

    def test_readonly_stream_interface(self):
        data = random.Random(5).randbytes(100_000)
        c = make_chunker()
        stream = RawStream(data)
        chunks = [ch for _, ch in c.iter_chunks(stream, read_size=8192)]
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(n == 8192 for n in stream.read_sizes))

    def test_insertion_preserves_most_chunks(self):
        rng = random.Random(6)
        base = rng.randbytes(1_000_000)
        for where, pos in (("head", 0), ("middle", len(base) // 2), ("tail", len(base))):
            edited = base[:pos] + rng.randbytes(5_000) + base[pos:]
            a, b = set(chunk_ids(base)), set(chunk_ids(edited))
            shared = len(a & b) / len(a)
            self.assertGreater(shared, 0.9, f"insert at {where}: shared={shared:.2%}")

    def test_deletion_preserves_most_chunks(self):
        rng = random.Random(7)
        base = rng.randbytes(1_000_000)
        pos = len(base) // 3
        edited = base[:pos] + base[pos + 8_000:]
        a, b = set(chunk_ids(base)), set(chunk_ids(edited))
        self.assertGreater(len(a & b) / len(a), 0.9)

    def test_unrelated_content_shares_nothing(self):
        rng = random.Random(8)
        a, b = rng.randbytes(500_000), rng.randbytes(500_000)
        self.assertEqual(set(chunk_ids(a)) & set(chunk_ids(b)), set())


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.rng = random.Random(42)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def scan(self, paths=None):
        index = DedupIndex(make_chunker())
        index.add_paths(paths or [self.dir])
        return index

    def test_empty_files_grouped_as_exact_duplicates(self):
        self.write("e1.bin", b"")
        self.write("e2.bin", b"")
        self.write("e3.bin", b"x")
        index = self.scan()
        groups = index.exact_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        empty = index.files[groups[0][0]]
        self.assertEqual(empty.n_chunks, 0)
        self.assertEqual(empty.digest, hashlib.sha256(b"").hexdigest())

    def test_identical_files_are_exact_group(self):
        data = self.rng.randbytes(200_000)
        self.write("a.bin", data)
        self.write("b.bin", data)
        self.write("c.bin", self.rng.randbytes(200_000))
        index = self.scan()
        groups = index.exact_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        self.assertEqual(index.similar_groups(), [])  # exact dupes collapse

    def test_near_duplicate_detected_with_similarity(self):
        base = self.rng.randbytes(800_000)
        edited = base[:400_000] + self.rng.randbytes(3_000) + base[400_000:]
        self.write("orig.bin", base)
        self.write("edited.bin", edited)
        index = self.scan()
        groups = index.similar_groups(threshold=0.5)
        self.assertEqual(len(groups), 1)
        edge = groups[0].edges[0]
        self.assertGreater(edge.containment, 0.9)
        self.assertGreater(edge.shared_chunks, 0)

    def test_unrelated_files_not_grouped(self):
        self.write("u1.bin", self.rng.randbytes(300_000))
        self.write("u2.bin", self.rng.randbytes(300_000))
        index = self.scan()
        self.assertEqual(index.exact_groups(), [])
        self.assertEqual(index.similar_groups(), [])

    def test_internal_repeated_chunks_reported(self):
        block = self.rng.randbytes(50_000)
        data = block + self.rng.randbytes(10_000) + block + block
        self.write("rep.bin", data)
        self.write("clean.bin", self.rng.randbytes(120_000))
        index = self.scan()
        repeats = index.internal_repeats()
        self.assertEqual(len(repeats), 1)
        file_idx, kinds, saved = repeats[0]
        self.assertEqual(index.files[file_idx].path, os.path.join(self.dir, "rep.bin"))
        self.assertGreater(kinds, 0)
        self.assertGreater(saved, 50_000)  # at least the repeated block

    def test_scan_file_single_pass_hashes(self):
        data = self.rng.randbytes(150_000)
        path = self.write("h.bin", data)
        info = scan_file(path, make_chunker())
        self.assertEqual(info.digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(info.size, len(data))
        self.assertEqual(sum(c for c, _ in info.chunks.values()), info.n_chunks)

    def test_mixed_directory_end_to_end(self):
        base = self.rng.randbytes(400_000)
        self.write("base.bin", base)
        self.write("base_copy.bin", base)                       # exact dup
        self.write("base_edit.bin", self.rng.randbytes(2_000) + base)  # near dup
        self.write("random.bin", self.rng.randbytes(400_000))   # unrelated
        self.write("empty.bin", b"")
        index = self.scan()
        self.assertEqual(len(index.exact_groups()), 1)
        sim = index.similar_groups()
        self.assertEqual(len(sim), 1)
        self.assertEqual(len(sim[0].members), 3)  # base, copy, edit


if __name__ == "__main__":
    unittest.main()
