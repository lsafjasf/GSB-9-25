"""自测 + 与逐元素参考实现对拍。

运行：python3 test_codec.py [-v]
"""
import random
import sys
import time
import unittest

import codec
from codec import Mode


# ------------------------------------------------------- 逐元素参考实现（刻意朴素）
def ref_delta_encode(seq):
    if not seq:
        return []
    out = [seq[0]]
    for i in range(1, len(seq)):
        out.append(seq[i] - seq[i - 1])
    return out


def ref_delta_decode(diff):
    if not diff:
        return []
    out = [diff[0]]
    for i in range(1, len(diff)):
        out.append(out[-1] + diff[i])
    return out


def ref_rle_encode(seq):
    runs = []
    for v in seq:
        if runs and runs[-1][0] == v:
            runs[-1][1] += 1
        else:
            runs.append([v, 1])
    return [tuple(r) for r in runs]


def ref_rle_decode(runs):
    out = []
    for v, c in runs:
        for _ in range(c):
            out.append(v)
    return out


# ------------------------------------------------------- 样本生成
EXTREMES = [0, 1, -1, 2**63 - 1, -(2**63), 2**127, -(2**127), 10**40, -(10**40),
            sys.maxsize, -sys.maxsize - 1]


def make_samples(seed=42):
    rng = random.Random(seed)
    samples = {
        "empty": [],
        "single": [rng.randint(-100, 100)],
        "all_same": [7] * 5000,
        "extremes": EXTREMES[:],
        "alternating": [(-1) ** i * (2**40) for i in range(2000)],
        "all_distinct": list(range(-3000, 3000)),
        "long_runs": [v for v in (rng.randint(-50, 50) for _ in range(200))
                      for _ in range(rng.randint(1, 300))],
        "monotone_ts": _monotone(rng, 5000),
        "random_small": [rng.randint(-5, 5) for _ in range(5000)],
        "random_big": [rng.randint(-(2**70), 2**70) for _ in range(2000)],
        "mixed_extremes": [rng.choice(EXTREMES) for _ in range(1000)],
    }
    return samples


def _monotone(rng, n):
    t, out = 1_700_000_000_000, []
    for _ in range(n):
        t += rng.randint(1, 9)
        out.append(t)
    return out


class TestZigzag(unittest.TestCase):
    def test_roundtrip_including_big(self):
        values = EXTREMES + [2**200, -(2**200), 3, -4]
        for v in values:
            self.assertEqual(codec.zigzag_decode(codec.zigzag_encode(v)), v)

    def test_no_sign_error(self):
        # 经典坑：固定位宽 zigzag 在边界值上出错；任意精度实现必须全对
        for bits in (1, 7, 8, 31, 32, 63, 64, 128):
            for v in (2**bits - 1, -(2**bits), 2**bits, -(2**bits) - 1):
                self.assertEqual(codec.zigzag_decode(codec.zigzag_encode(v)), v)


class TestRoundtripAllModes(unittest.TestCase):
    def test_every_sample_every_mode(self):
        for name, seq in make_samples().items():
            for mode in Mode:
                with self.subTest(sample=name, mode=mode.name):
                    data = codec.encode(seq, mode)
                    self.assertEqual(codec.decode(data), seq)

    def test_auto_roundtrip(self):
        for name, seq in make_samples().items():
            with self.subTest(sample=name):
                self.assertEqual(codec.decode(codec.encode(seq)), seq)


class TestDifferentialVsReference(unittest.TestCase):
    """库的值域变换 vs 逐元素参考实现，逐元素对拍。"""

    def test_transforms_match_reference(self):
        rng = random.Random(7)
        cases = list(make_samples(seed=99).values())
        cases += [[rng.randint(-(2**64), 2**64) for _ in range(rng.randint(0, 500))]
                  for _ in range(50)]
        for i, seq in enumerate(cases):
            with self.subTest(case=i):
                # 差分
                self.assertEqual(codec.delta_encode_values(seq), ref_delta_encode(seq))
                self.assertEqual(codec.delta_decode_values(ref_delta_encode(seq)), seq)
                # 游程
                self.assertEqual(codec.rle_encode_values(seq), ref_rle_encode(seq))
                self.assertEqual(codec.rle_decode_values(ref_rle_encode(seq)), seq)
                # 组合
                first, runs = codec.delta_rle_encode_values(seq)
                ref_diff = ref_delta_encode(seq)
                if seq:
                    self.assertEqual(first, [ref_diff[0]])
                    self.assertEqual(runs, ref_rle_encode(ref_diff[1:]))
                    self.assertEqual(codec.delta_rle_decode_values(first, runs), seq)
                else:
                    self.assertEqual((first, runs), ([], []))

    def test_serialized_roundtrip_matches_reference(self):
        rng = random.Random(123)
        for trial in range(200):
            kind = trial % 4
            if kind == 0:
                seq = [rng.choice(EXTREMES) for _ in range(rng.randint(0, 300))]
            elif kind == 1:
                seq = [rng.randint(-3, 3) for _ in range(rng.randint(0, 2000))]
            elif kind == 2:
                seq = [rng.randint(0, 5) for _ in range(rng.randint(0, 1000))]
                seq = [v for v in seq for _ in range(rng.randint(1, 20))]
            else:
                seq = [rng.randint(-(2**100), 2**100) for _ in range(rng.randint(0, 100))]
            with self.subTest(trial=trial):
                for mode in Mode:
                    self.assertEqual(codec.decode(codec.encode(seq, mode)), seq)
                self.assertEqual(codec.decode(codec.encode(seq)), seq)


class TestModeSelection(unittest.TestCase):
    def test_constant_picks_rle_family(self):
        res = codec.encode_with_report([42] * 10000)
        self.assertIn(res.mode, (Mode.RLE, Mode.DELTA_RLE))
        self.assertLess(res.size, 100)

    def test_monotone_picks_delta_family(self):
        seq = _monotone(random.Random(1), 10000)
        res = codec.encode_with_report(seq)
        self.assertIn(res.mode, (Mode.DELTA, Mode.DELTA_RLE))
        self.assertLess(res.size, len(seq) * 8 // 4)

    def test_oscillating_falls_back_to_raw(self):
        # 剧烈震荡：差分绝对值巨大、无重复游程，任何压缩都劣化 -> 回退 RAW
        seq = [(-1) ** i * (2**60 + i) for i in range(5000)]
        res = codec.encode_with_report(seq)
        self.assertEqual(res.mode, Mode.RAW, res.reason)
        self.assertEqual(codec.decode(res.data), seq)  # 解压端自动识别回退

    def test_random_big_falls_back_to_raw(self):
        rng = random.Random(5)
        seq = [rng.randint(-(2**70), 2**70) for _ in range(3000)]
        res = codec.encode_with_report(seq)
        self.assertEqual(res.mode, Mode.RAW, res.reason)

    def test_reason_mentions_sizes(self):
        res = codec.encode_with_report([1, 2, 3, 4])
        self.assertIn("raw", res.reason)
        self.assertEqual(set(res.sizes), {"raw", "delta", "rle", "delta_rle"})


class TestRobustness(unittest.TestCase):
    def test_truncated_raises(self):
        data = codec.encode([1, 2, 3, 1000])
        for cut in range(len(data)):
            with self.assertRaises(ValueError):
                codec.decode(data[:cut])

    def test_trailing_garbage_raises(self):
        data = codec.encode([1, 2, 3])
        with self.assertRaises(ValueError):
            codec.decode(data + b"\x00")

    def test_bad_magic_and_mode(self):
        with self.assertRaises(ValueError):
            codec.decode(b"XXXX\x00\x00")
        with self.assertRaises(ValueError):
            codec.decode(codec.MAGIC + b"\xfe\x00")

    def test_type_check(self):
        with self.assertRaises(TypeError):
            codec.encode([1, 2.5, 3])
        with self.assertRaises(TypeError):
            codec.encode([True, False])


class TestScale(unittest.TestCase):
    def test_million_elements(self):
        n = 1_000_000
        seq = _monotone(random.Random(0), n)
        t0 = time.perf_counter()
        res = codec.encode_with_report(seq)
        t1 = time.perf_counter()
        out = codec.decode(res.data)
        t2 = time.perf_counter()
        self.assertEqual(out, seq)
        print(f"\n[perf] n={n} 递增时间戳: mode={res.mode.name} "
              f"{n * 8}B -> {res.size}B ({res.size / (n * 8):.2%}), "
              f"encode {t1 - t0:.2f}s, decode {t2 - t1:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
