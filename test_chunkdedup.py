#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chunkdedup 自测: python3 test_chunkdedup.py [-v]"""

import hashlib
import os
import random
import shutil
import tempfile
import unittest

from chunkdedup import (CDCChunker, DedupIndex, FileRecord, scan_file,
                        scan_paths)

RNG = random.Random(1234)


def randbytes(n):
    return RNG.randbytes(n)


def chunk_all(data, **kw):
    chunker = CDCChunker(**kw)
    chunks = chunker.update(data)
    tail = chunker.finish()
    if tail is not None:
        chunks.append(tail)
    return chunks


class ChunkerTest(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(chunk_all(b""), [])

    def test_single_chunk_file(self):
        data = randbytes(100)  # 小于 min_size
        chunks = chunk_all(data)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].offset, 0)
        self.assertEqual(chunks[0].length, 100)

    def test_max_size_forced_cut(self):
        data = randbytes(300 * 1024)
        chunks = chunk_all(data)
        self.assertTrue(all(c.length <= 64 * 1024 for c in chunks))
        self.assertTrue(all(c.length >= 2 * 1024 for c in chunks[:-1]))

    def test_streaming_equals_one_shot(self):
        """任意分块喂入与一次性喂入产出完全相同的块序列。"""
        data = randbytes(500 * 1024)
        expected = [(c.offset, c.length, c.digest) for c in chunk_all(data)]
        for step in (1, 7, 4096, 65536, 1 << 20):
            chunker = CDCChunker()
            got = []
            for i in range(0, len(data), step):
                got.extend(chunker.update(data[i:i + step]))
            tail = chunker.finish()
            if tail is not None:
                got.append(tail)
            got = [(c.offset, c.length, c.digest) for c in got]
            self.assertEqual(expected, got, f"step={step}")

    def test_offsets_and_lengths_cover_file(self):
        data = randbytes(1 << 20)
        chunks = chunk_all(data)
        pos = 0
        for c in chunks:
            self.assertEqual(c.offset, pos)
            pos += c.length
        self.assertEqual(pos, len(data))

    def test_deterministic_boundaries(self):
        data = randbytes(200 * 1024)
        a = [c.digest for c in chunk_all(data)]
        b = [c.digest for c in chunk_all(data)]
        self.assertEqual(a, b)

    def test_insert_at_head_resyncs(self):
        """头部插入 5KB 后, 原文件绝大部分块应保持不变。"""
        base = randbytes(600 * 1024)
        modified = randbytes(5 * 1024) + base
        base_digests = {c.digest for c in chunk_all(base)}
        mod_chunks = chunk_all(modified)
        kept = sum(1 for c in mod_chunks if c.digest in base_digests)
        self.assertGreater(kept / len(mod_chunks), 0.9)

    def test_insert_in_middle_resyncs(self):
        base = randbytes(600 * 1024)
        cut = 300 * 1024
        modified = base[:cut] + randbytes(7 * 1024) + base[cut:]
        base_digests = {c.digest for c in chunk_all(base)}
        mod_chunks = chunk_all(modified)
        kept = sum(1 for c in mod_chunks if c.digest in base_digests)
        self.assertGreater(kept / len(mod_chunks), 0.9)

    def test_delete_in_middle_resyncs(self):
        base = randbytes(600 * 1024)
        modified = base[:100 * 1024] + base[100 * 1024 + 9000:]
        base_digests = {c.digest for c in chunk_all(base)}
        mod_chunks = chunk_all(modified)
        kept = sum(1 for c in mod_chunks if c.digest in base_digests)
        self.assertGreater(kept / len(mod_chunks), 0.9)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chunkdedup_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_empty_file(self):
        path = self.write("empty.bin", b"")
        rec = scan_file(path)
        self.assertEqual(rec.size, 0)
        self.assertEqual(rec.chunks, [])
        self.assertEqual(rec.sha256, hashlib.sha256(b"").hexdigest())

    def test_file_hash_matches(self):
        data = randbytes(123456)
        rec = scan_file(self.write("a.bin", data))
        self.assertEqual(rec.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(rec.size, len(data))

    def test_chunk_sink_streaming(self):
        data = randbytes(3 << 20)
        path = self.write("big.bin", data)
        via_sink = []
        rec = scan_file(path, chunk_sink=via_sink.append)
        self.assertEqual(rec.chunks, [])  # sink 模式不保留块
        rec2 = scan_file(path)
        self.assertEqual([c.digest for c in via_sink],
                         [c.digest for c in rec2.chunks])


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chunkdedup_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def build_index(self, files):
        index = DedupIndex()
        for name, data in files.items():
            index.add_path(self.write(name, data))
        return index

    def test_exact_duplicates(self):
        payload = randbytes(100 * 1024)
        index = self.build_index({
            "a.bin": payload, "b.bin": payload, "c.bin": randbytes(100 * 1024),
        })
        groups = index.exact_groups()
        self.assertEqual(len(groups), 1)
        paths = sorted(index.files[i].path for i in groups[0])
        self.assertTrue(paths[0].endswith("a.bin"))
        self.assertTrue(paths[1].endswith("b.bin"))

    def test_empty_files_are_exact_duplicates(self):
        index = self.build_index({"e1.bin": b"", "e2.bin": b"",
                                  "ne.bin": b"x"})
        groups = index.exact_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_similar_after_head_insert(self):
        base = randbytes(500 * 1024)
        index = self.build_index({
            "orig.bin": base,
            "head.bin": randbytes(8 * 1024) + base,
        })
        pairs = index.similar_pairs(threshold=0.5)
        self.assertEqual(len(pairs), 1)
        self.assertGreater(pairs[0].dice, 0.9)
        self.assertGreater(pairs[0].shared_ratio_small, 0.9)

    def test_similar_after_middle_insert(self):
        base = randbytes(500 * 1024)
        cut = 250 * 1024
        index = self.build_index({
            "orig.bin": base,
            "mid.bin": base[:cut] + randbytes(9 * 1024) + base[cut:],
        })
        pairs = index.similar_pairs(threshold=0.5)
        self.assertEqual(len(pairs), 1)
        self.assertGreater(pairs[0].dice, 0.9)

    def test_unrelated_files_not_similar(self):
        index = self.build_index({
            "r1.bin": randbytes(300 * 1024),
            "r2.bin": randbytes(300 * 1024),
        })
        self.assertEqual(index.similar_pairs(threshold=0.05), [])
        self.assertEqual(index.similar_groups(threshold=0.05), [])

    def test_similar_groups_clustering(self):
        base = randbytes(400 * 1024)
        index = self.build_index({
            "v1.bin": base,
            "v2.bin": randbytes(4 * 1024) + base,
            "v3.bin": base[:200 * 1024] + randbytes(4 * 1024)
                      + base[200 * 1024:],
            "other.bin": randbytes(400 * 1024),
        })
        groups = index.similar_groups(threshold=0.5)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].members), 3)

    def test_intra_file_repeated_chunks(self):
        block = randbytes(64 * 1024)
        index = self.build_index({"rep.bin": block * 6})
        reps = index.repeated_chunks(0)
        self.assertTrue(reps)
        total_repeat = sum(c for _, c, _ in reps)
        self.assertGreaterEqual(total_repeat, 4)

    def test_scan_paths_directory(self):
        self.write("x.bin", randbytes(50 * 1024))
        sub = os.path.join(self.dir, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "y.bin"), "wb") as f:
            f.write(randbytes(50 * 1024))
        index = scan_paths([self.dir])
        self.assertEqual(len(index.files), 2)


if __name__ == "__main__":
    unittest.main()
