"""原子性、损坏用例、跨版本升级与并发自测。

运行：python3 -m unittest -v tests.test_snapshot
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snapshot_lib import (  # noqa: E402
    CorruptedSnapshotError,
    CURRENT_VERSION,
    UnknownVersionError,
    cleanup_stale_tempfiles,
    commit_staged,
    encode_snapshot,
    load_raw,
    load_state,
    save_state,
    stage_state,
    upgrade_state,
)
from snapshot_lib.snapshot import _MIGRATIONS  # noqa: E402


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def v1_state():
    return {
        "user_id": "u-1",
        "messages": [{"role": "user", "content": "hi"}],
    }


class TempDirMixin:
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.path = os.path.join(self.dir, "session.snap")

    def tearDown(self):
        self._td.cleanup()


class RoundtripTests(TempDirMixin, unittest.TestCase):
    def test_empty_state(self):
        save_state(self.path, {})
        snap = load_state(self.path)
        self.assertEqual(snap.state, {})
        self.assertEqual(snap.version, CURRENT_VERSION)
        self.assertIsNone(snap.upgraded_from)
        self.assertTrue(snap.created)

    def test_roundtrip_preserves_content(self):
        state = {"session": {"messages": [1, 2, 3], "user_id": "x"}, "notes": []}
        save_state(self.path, state)
        snap = load_state(self.path)
        self.assertEqual(snap.state, state)

    def test_large_state_roundtrip(self):
        # 5 MiB 级别，保证测试套件本身跑得快；数十 MiB 见 benchmark.py
        blob = "x" * (5 * 1024 * 1024)
        state = {"blob": blob, "n": 42}
        save_state(self.path, state)
        snap = load_state(self.path)
        self.assertEqual(snap.state["blob"], blob)
        self.assertEqual(snap.state["n"], 42)
        self.assertGreater(os.path.getsize(self.path), 5 * 1024 * 1024)


class IntegrityTests(TempDirMixin, unittest.TestCase):
    def _write_raw(self, raw: bytes):
        with open(self.path, "wb") as fh:
            fh.write(raw)

    def test_payload_corruption_detected(self):
        raw = bytearray(encode_snapshot({"a": 1}))
        raw[-1] ^= 0xFF
        self._write_raw(bytes(raw))
        with self.assertRaises(CorruptedSnapshotError) as cm:
            load_state(self.path)
        self.assertIn("sha256 mismatch", str(cm.exception))

    def test_truncated_header_detected(self):
        raw = encode_snapshot({"a": 1})
        # 只保留前三个头字段，丢掉 terminator/body —— 快照头被截断。
        cut = raw.split(b"\n", 3)
        self._write_raw(b"\n".join(cut[:3]))
        with self.assertRaises(CorruptedSnapshotError) as cm:
            load_state(self.path)
        self.assertIn("header truncated", str(cm.exception))

    def test_truncated_body_detected(self):
        raw = encode_snapshot({"a": 1})
        sep = raw.index(b"\n\n") + 2
        # 头完整，只截掉 body 的后半段（写入在 body 中途中断）。
        self._write_raw(raw[: sep + (len(raw) - sep) // 2])
        with self.assertRaises(CorruptedSnapshotError) as cm:
            load_state(self.path)
        self.assertIn("length mismatch", str(cm.exception))

    def test_tampered_length_detected(self):
        raw = encode_snapshot({"a": 1}).replace(
            b"length: ", b"length: 999999\nx: ", 1
        )
        self._write_raw(raw)
        with self.assertRaises(CorruptedSnapshotError):
            load_state(self.path)

    def test_garbage_file_detected(self):
        self._write_raw(b"this is not a snapshot at all\n")
        with self.assertRaises(CorruptedSnapshotError) as cm:
            load_state(self.path)
        self.assertIn("bad magic", str(cm.exception))

    def test_version_payload_mismatch_detected(self):
        raw = encode_snapshot(v1_state(), version=1)
        head, body = raw.split(b"\n\n", 1)
        tampered_body = body.replace(b'"version":1', b'"version":2', 1)
        new_head = head
        for line in head.split(b"\n"):
            if line.startswith(b"sha256: "):
                new_head = new_head.replace(
                    line,
                    b"sha256: " + hashlib.sha256(tampered_body).hexdigest().encode(),
                )
        self._write_raw(new_head + b"\n\n" + tampered_body)
        with self.assertRaises(CorruptedSnapshotError) as cm:
            load_state(self.path)
        self.assertIn("version mismatch", str(cm.exception))

    def test_unknown_version_distinct_from_corruption(self):
        # 内容完全合法（自洽的 sha256/length），只是版本号不认识。
        raw = encode_snapshot({"future": True}, version=99)
        self._write_raw(raw)
        with self.assertRaises(UnknownVersionError) as cm:
            load_state(self.path)
        self.assertEqual(cm.exception.version, 99)
        # load_raw（不升级）不报未知版本——完整性是好的。
        state, version, _ = load_raw(self.path)
        self.assertEqual((state, version), ({"future": True}, 99))

    def test_missing_file_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            load_state(self.path)


class FailurePreservesFileTests(TempDirMixin, unittest.TestCase):
    def test_failed_recovery_never_touches_original(self):
        save_state(self.path, {"good": True})
        before = _read_bytes(self.path)
        st = os.stat(self.path)
        listing_before = set(os.listdir(self.dir))

        tampered = bytearray(before)
        tampered[20] ^= 0x01
        with open(self.path, "wb") as fh:
            fh.write(bytes(tampered))

        with self.assertRaises(CorruptedSnapshotError):
            load_state(self.path)
        # 原文件字节、目录内容均未被恢复流程改动。
        self.assertEqual(_read_bytes(self.path), bytes(tampered))
        self.assertEqual(os.stat(self.path).st_size, st.st_size)
        self.assertEqual(set(os.listdir(self.dir)), listing_before)

        # 恢复失败后修好文件，仍能正常恢复。
        with open(self.path, "wb") as fh:
            fh.write(before)
        self.assertEqual(load_state(self.path).state, {"good": True})

    def test_unknown_version_also_preserves_file(self):
        raw = encode_snapshot({"x": 1}, version=99)
        with open(self.path, "wb") as fh:
            fh.write(raw)
        with self.assertRaises(UnknownVersionError):
            load_state(self.path)
        self.assertEqual(_read_bytes(self.path), raw)


class AtomicWriteTests(TempDirMixin, unittest.TestCase):
    def test_write_failure_cleans_own_tempfile(self):
        tmp = stage_state(self.path, encode_snapshot({"ok": 1}))
        self.assertTrue(os.path.exists(tmp))
        os.chmod(self.dir, 0o500)  # 目录不可写 -> replace 失败
        try:
            with self.assertRaises(OSError):
                commit_staged(self.path, tmp)
        finally:
            os.chmod(self.dir, 0o700)
        # 正式快照从未出现；残留临时文件可被显式清扫。
        self.assertFalse(os.path.exists(self.path))
        removed = cleanup_stale_tempfiles(self.path)
        self.assertEqual(len(removed), 1)
        self.assertEqual(os.listdir(self.dir), [])

    def test_interrupted_write_leaves_tempfile_which_next_save_reaps(self):
        # 模拟进程在 stage 之后、commit 之前崩溃：留下临时文件残留。
        orphan = stage_state(self.path, encode_snapshot({"half": "written"}))
        self.assertTrue(os.path.basename(orphan).startswith(".sessnap-"))
        self.assertFalse(os.path.exists(self.path))

        # 下一次正常保存：新快照原子落地，残留被持锁清扫。
        save_state(self.path, {"recovered": True})
        self.assertEqual(load_state(self.path).state, {"recovered": True})
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp.sessnap")]
        self.assertEqual(leftovers, [])

    def test_partial_tempfile_bytes_never_loaded_as_snapshot(self):
        # 临时文件只写了一半（被截断），正式快照仍是旧的完整数据。
        save_state(self.path, {"old": "good"})
        orphan = stage_state(self.path, encode_snapshot({"new": 1})[:100])
        # load 只读正式路径，绝不会把半截临时文件当成快照。
        self.assertEqual(load_state(self.path).state, {"old": "good"})
        save_state(self.path, {"new": 2})
        self.assertEqual(load_state(self.path).state, {"new": 2})
        self.assertFalse(os.path.exists(orphan))


class MigrationTests(TempDirMixin, unittest.TestCase):
    def test_migrations_are_idempotent_stepwise(self):
        s = v1_state()
        s2 = _MIGRATIONS[1](s)
        self.assertEqual(_MIGRATIONS[1](s2), s2)
        s3 = _MIGRATIONS[2](s2)
        self.assertEqual(_MIGRATIONS[2](s3), s3)

    def test_full_upgrade_idempotent_and_deterministic(self):
        s = v1_state()
        once, v = upgrade_state(json.loads(json.dumps(s)), 1)
        self.assertEqual(v, CURRENT_VERSION)
        # 从已升级结果再升级，结果一致；同一输入跑两次也一致。
        again, v2 = upgrade_state(json.loads(json.dumps(once)), CURRENT_VERSION)
        self.assertEqual((again, v2), (once, CURRENT_VERSION))
        once_b, _ = upgrade_state(json.loads(json.dumps(s)), 1)
        self.assertEqual(once_b, once)

    def test_cross_two_versions_from_disk(self):
        """跨两个版本（v1 -> v2 -> v3）的端到端升级测试。"""
        raw = encode_snapshot(v1_state(), version=1)
        with open(self.path, "wb") as fh:
            fh.write(raw)
        snap = load_state(self.path)
        self.assertEqual(snap.version, CURRENT_VERSION)
        self.assertEqual(snap.upgraded_from, 1)
        self.assertEqual(
            snap.state,
            {
                "session": {
                    "user_id": "u-1",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                "notes": [],
            },
        )
        # 升级后以当前版本重存，再次读取不再发生升级。
        save_state(self.path, snap.state, version=snap.version)
        snap2 = load_state(self.path)
        self.assertIsNone(snap2.upgraded_from)
        self.assertEqual(snap2.state, snap.state)

    def test_cross_two_versions_with_string_notes(self):
        # v2 时代写下的字符串 notes，升级到 v3 变为对象列表；重复升级稳定。
        v2 = {"session": {"user_id": "u", "messages": []}, "notes": ["a", "b"]}
        raw = encode_snapshot(v2, version=2)
        with open(self.path, "wb") as fh:
            fh.write(raw)
        snap = load_state(self.path)
        self.assertEqual(snap.upgraded_from, 2)
        self.assertEqual(
            snap.state["notes"], [{"id": 0, "text": "a"}, {"id": 1, "text": "b"}]
        )
        again, ver = upgrade_state(
            json.loads(json.dumps(snap.state)), CURRENT_VERSION
        )
        self.assertEqual((again, ver), (snap.state, CURRENT_VERSION))

    def test_unknown_jump_version_rejected(self):
        with self.assertRaises(UnknownVersionError):
            upgrade_state(v1_state(), 1, target_version=5)


class ConcurrencyTests(TempDirMixin, unittest.TestCase):
    @staticmethod
    def _writer(path, worker_id, iterations):
        # 各进程写不同内容；最终文件必须是某次完整写入（可正常恢复）。
        for i in range(iterations):
            save_state(path, {"worker": worker_id, "i": i, "pad": "p" * 1024})
            time.sleep(0.001)

    def test_concurrent_writers_always_leave_valid_snapshot(self):
        ctx = mp.get_context("fork")
        procs = [
            ctx.Process(target=self._writer, args=(self.path, wid, 20))
            for wid in range(4)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
        for p in procs:
            self.assertEqual(p.exitcode, 0)

        snap = load_state(self.path)
        self.assertIn(snap.state["worker"], range(4))
        self.assertIn(snap.state["i"], range(20))
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp.sessnap")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
