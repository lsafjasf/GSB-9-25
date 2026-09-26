"""Self-tests for snapshot.py (stdlib unittest only).

Run:  python3 test_snapshot.py -v
"""

import os
import shutil
import tempfile
import threading
import unittest

import snapshot


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="snaptest-")
        self.path = os.path.join(self.dir, "session.snap")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def write_bytes(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    # ------------------------------------------------------------------
    # Basic round-trip
    # ------------------------------------------------------------------

    def test_roundtrip_current_version(self):
        state = {
            "history": [{"role": "user", "content": "hi"}],
            "meta": {"tags": ["a"], "schema": 3},
        }
        snapshot.save(self.path, state)
        self.assertEqual(snapshot.load(self.path), state)

    def test_empty_states(self):
        for state in ({}, [], None, "", 0):
            snapshot.save(self.path, state)
            version, raw = snapshot.load_raw(self.path)
            self.assertEqual(version, snapshot.CURRENT_VERSION)
            self.assertEqual(raw, state)

    def test_unicode_and_nested_state(self):
        state = {"history": [{"content": "你好，世界 🌍"}], "n": 42}
        snapshot.save(self.path, state)
        loaded = snapshot.load(self.path)
        self.assertEqual(loaded["history"], state["history"])
        self.assertEqual(loaded["n"], 42)

    # ------------------------------------------------------------------
    # Atomicity
    # ------------------------------------------------------------------

    def test_failed_save_keeps_previous_snapshot_and_cleans_temp(self):
        good = {"history": ["v1"], "meta": {"tags": [], "schema": 3}}
        snapshot.save(self.path, good)
        before = self.read_bytes()

        real_replace = os.replace

        def boom(*args, **kwargs):
            raise OSError("simulated crash during replace")

        snapshot.os.replace = boom
        try:
            with self.assertRaises(OSError):
                snapshot.save(self.path, {"history": ["v2"]})
        finally:
            snapshot.os.replace = real_replace

        self.assertEqual(self.read_bytes(), before)  # old snapshot intact
        self.assertEqual(snapshot.load(self.path), good)
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".snap-")]
        self.assertEqual(leftovers, [])  # temp file cleaned up

    def test_interrupted_save_leaves_harmless_temp_residue(self):
        good = {"history": ["ok"], "meta": {"tags": [], "schema": 3}}
        snapshot.save(self.path, good)
        # Simulate a crashed writer: orphan temp file next to the snapshot.
        residue = os.path.join(self.dir, ".snap-deadbeef.tmp")
        with open(residue, "wb") as f:
            f.write(b"partial garbage")
        self.assertEqual(snapshot.load(self.path), good)  # load unaffected
        self.assertEqual(snapshot.cleanup_stale_temps(self.path), 1)
        self.assertFalse(os.path.exists(residue))
        self.assertEqual(snapshot.cleanup_stale_temps(self.path), 0)

    def test_concurrent_writers_leave_one_valid_snapshot(self):
        states = [
            {"history": [f"writer-{i}"], "meta": {"tags": [], "schema": 3}}
            for i in range(16)
        ]
        errors = []

        def writer(state):
            try:
                for _ in range(5):
                    snapshot.save(self.path, state)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(s,)) for s in states]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        final = snapshot.load(self.path)  # must be valid, not torn
        self.assertIn(final, states)
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".snap-")]
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------------
    # Corruption: distinct from unknown-version, original preserved
    # ------------------------------------------------------------------

    def test_corrupted_payload_detected_and_file_preserved(self):
        snapshot.save(self.path, {"history": ["x"] * 100})
        data = bytearray(self.read_bytes())
        data[-1] ^= 0xFF  # flip last payload byte
        self.write_bytes(bytes(data))
        before = self.read_bytes()

        with self.assertRaises(snapshot.CorruptedSnapshotError):
            snapshot.load(self.path)
        self.assertEqual(self.read_bytes(), before)  # untouched after failure

        state, msg = snapshot.restore(self.path, default={"fresh": True})
        self.assertEqual(state, {"fresh": True})
        self.assertIn("corrupted", msg)
        self.assertEqual(self.read_bytes(), before)

    def test_truncated_header(self):
        snapshot.save(self.path, {"history": ["x"]})
        data = self.read_bytes()
        self.write_bytes(data[: len(snapshot.MAGIC) + 2])  # cut inside header
        with self.assertRaises(snapshot.CorruptedSnapshotError):
            snapshot.load(self.path)

    def test_truncated_payload(self):
        snapshot.save(self.path, {"history": ["x"] * 50})
        data = self.read_bytes()
        self.write_bytes(data[: len(data) - 10])
        with self.assertRaises(snapshot.CorruptedSnapshotError):
            snapshot.load(self.path)

    def test_bad_magic(self):
        self.write_bytes(b"NOTASNAP" + b"\x00" * 64)
        with self.assertRaises(snapshot.CorruptedSnapshotError):
            snapshot.load(self.path)

    def test_unknown_version_distinguished_from_corruption(self):
        # version 99 with a perfectly valid checksum
        snapshot.save(self.path, {"history": []}, version=99)
        before = self.read_bytes()
        with self.assertRaises(snapshot.UnknownVersionError):
            snapshot.load(self.path)
        self.assertEqual(self.read_bytes(), before)

        state, msg = snapshot.restore(self.path, default="fallback")
        self.assertEqual(state, "fallback")
        self.assertIn("unknown snapshot version", msg)

    def test_restore_missing_file(self):
        state, msg = snapshot.restore(self.path, default={})
        self.assertEqual(state, {})
        self.assertIn("no snapshot", msg)

    # ------------------------------------------------------------------
    # Version upgrades
    # ------------------------------------------------------------------

    def test_upgrade_v1_across_two_versions(self):
        v1_state = {"history": [{"role": "user", "content": "old"}]}
        snapshot.save(self.path, v1_state, version=1)
        version, raw = snapshot.load_raw(self.path)
        self.assertEqual(version, 1)
        self.assertEqual(raw, v1_state)  # file on disk stays v1

        loaded = snapshot.load(self.path)
        self.assertEqual(loaded["history"], v1_state["history"])
        self.assertEqual(loaded["meta"], {"tags": [], "schema": 3})

    def test_upgrade_v2_to_v3(self):
        v2_state = {"history": ["m1"], "meta": {"custom": 1}}
        snapshot.save(self.path, v2_state, version=2)
        loaded = snapshot.load(self.path)
        self.assertEqual(
            loaded,
            {"history": ["m1"], "meta": {"custom": 1, "tags": [], "schema": 3}},
        )

    def test_upgrade_chain_matches_step_by_step(self):
        v1_state = {"history": ["a", "b"]}
        direct = snapshot.upgrade(1, dict(v1_state))  # v1 -> v3 in one call
        stepwise = snapshot._upgrade_2_to_3(snapshot._upgrade_1_to_2(dict(v1_state)))
        self.assertEqual(direct, stepwise)

    def test_upgrade_is_idempotent(self):
        v1_state = {"history": ["a"], "meta": {"tags": ["keep"], "schema": 3}}
        once = snapshot.upgrade(1, dict(v1_state))
        twice = snapshot.upgrade(snapshot.CURRENT_VERSION, dict(once))
        thrice = snapshot.upgrade(snapshot.CURRENT_VERSION, dict(twice))
        self.assertEqual(once, twice)
        self.assertEqual(twice, thrice)
        # existing user data survives the idempotent re-run
        self.assertEqual(once["meta"]["tags"], ["keep"])

    def test_upgrade_rejects_out_of_range_version(self):
        with self.assertRaises(snapshot.UnknownVersionError):
            snapshot.upgrade(0, {})
        with self.assertRaises(snapshot.UnknownVersionError):
            snapshot.upgrade(99, {})

    # ------------------------------------------------------------------
    # Larger state round-trip (functional, not the benchmark)
    # ------------------------------------------------------------------

    def test_multi_megabyte_state_roundtrip(self):
        state = {
            "history": [
                {"role": "user", "content": "x" * 1000, "i": i}
                for i in range(3000)
            ]
        }
        snapshot.save(self.path, state)
        self.assertGreater(os.path.getsize(self.path), 3_000_000)
        self.assertEqual(snapshot.load(self.path)["history"], state["history"])


if __name__ == "__main__":
    unittest.main()
