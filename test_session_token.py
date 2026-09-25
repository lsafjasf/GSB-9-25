"""session_token 的自测套件：篡改用例集、时钟边界、恒定时间验证。"""

import base64
import hashlib
import hmac
import inspect
import json
import time
import unittest

import session_token as st

KEY = b"test-key-0123456789abcdef"
OTHER_KEY = b"other-key-0123456789abcde"
NOW = 1_700_000_000
PAYLOAD = {"uid": 42, "role": "admin", "tags": ["a", "b"]}


def fixed_rand(n):
    return bytes(range(n))


def issue_default(**kw):
    args = dict(now=NOW, ttl=3600, rand=fixed_rand)
    args.update(kw)
    return st.issue(PAYLOAD, KEY, **args)


def resign(claims: dict, key=KEY) -> str:
    """用给定 claims 手工构造一个签名正确的令牌（用于结构类用例）。"""
    body = st._canonical_json(claims)
    payload_b64 = st._b64encode(body)
    sig = hmac.new(key, f"{st.PREFIX}.{payload_b64}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{st.PREFIX}.{payload_b64}.{st._b64encode(sig)}"


def tamper_signature(token: str, byte_index: int) -> str:
    """把签名的第 byte_index 个字节翻转一个比特，保持其余部分不变。"""
    prefix, payload_b64, sig_b64 = token.split(".")
    sig = bytearray(st._b64decode(sig_b64))
    sig[byte_index] ^= 0x01
    return f"{prefix}.{payload_b64}.{st._b64encode(bytes(sig))}"


class RoundTripTests(unittest.TestCase):
    def test_issue_and_verify_ok(self):
        token = issue_default()
        result = st.verify(token, KEY, now=NOW + 10)
        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)
        self.assertEqual(result.claims["data"], PAYLOAD)
        self.assertEqual(result.claims["iat"], NOW)
        self.assertEqual(result.claims["exp"], NOW + 3600)

    def test_deterministic_serialization_key_order_irrelevant(self):
        a = st.issue({"b": 1, "a": 2}, KEY, now=NOW, rand=fixed_rand)
        b = st.issue({"a": 2, "b": 1}, KEY, now=NOW, rand=fixed_rand)
        self.assertEqual(a, b)

    def test_repeated_issuance_same_payload_reproducible(self):
        t1 = issue_default()
        t2 = issue_default()
        self.assertEqual(t1, t2)  # 同一载荷 + 同一时钟 + 同一随机源 => 同一令牌

    def test_repeated_issuance_real_randomness_differs_but_both_valid(self):
        t1 = st.issue(PAYLOAD, KEY, now=NOW)
        t2 = st.issue(PAYLOAD, KEY, now=NOW)
        self.assertNotEqual(t1, t2)  # nonce 不同
        self.assertTrue(st.verify(t1, KEY, now=NOW).ok)
        self.assertTrue(st.verify(t2, KEY, now=NOW).ok)

    def test_signature_covers_prefix(self):
        token = issue_default()
        self.assertEqual(
            st.verify("XX1" + token[len(st.PREFIX):], KEY, now=NOW).reason,
            st.Failure.MALFORMED,
        )


class TamperTests(unittest.TestCase):
    """篡改用例集。"""

    def test_tampered_payload(self):
        token = issue_default()
        prefix, payload_b64, sig_b64 = token.split(".")
        claims = json.loads(st._b64decode(payload_b64))
        claims["data"]["role"] = "root"  # 提权篡改
        forged = f"{prefix}.{st._b64encode(st._canonical_json(claims))}.{sig_b64}"
        result = st.verify(forged, KEY, now=NOW)
        self.assertEqual(result.reason, st.Failure.SIGNATURE_MISMATCH)

    def test_truncated_signature(self):
        token = issue_default()
        truncated = token[: token.rfind(".") + 1] + token.split(".")[2][:20]
        self.assertEqual(
            st.verify(truncated, KEY, now=NOW).reason, st.Failure.MALFORMED
        )

    def test_missing_field(self):
        claims = {"iat": NOW, "nbf": NOW, "nonce": "AA", "data": {}}  # 缺 exp
        token = resign(claims)
        self.assertEqual(
            st.verify(token, KEY, now=NOW).reason, st.Failure.MALFORMED
        )

    def test_oversized_payload_rejected_at_issue(self):
        big = {"blob": "x" * (st.DEFAULT_MAX_PAYLOAD_BYTES + 1)}
        with self.assertRaises(st.PayloadTooLargeError):
            st.issue(big, KEY, now=NOW, rand=fixed_rand)

    def test_oversized_payload_rejected_at_verify(self):
        claims = {"iat": NOW, "nbf": NOW, "exp": NOW + 3600,
                  "nonce": "AA", "data": {"blob": "x" * 5000}}
        token = resign(claims)
        self.assertEqual(
            st.verify(token, KEY, now=NOW).reason, st.Failure.MALFORMED
        )

    def test_wrong_key(self):
        token = issue_default()
        self.assertEqual(
            st.verify(token, OTHER_KEY, now=NOW).reason,
            st.Failure.SIGNATURE_MISMATCH,
        )

    def test_structure_variants(self):
        token = issue_default()
        cases = [
            "",                        # 空
            "abc",                     # 段数不足
            token + ".extra",          # 段数过多
            token.replace("ST1", "ST2", 1),  # 前缀错误
            "ST1..sig",                # 空载荷段
            "ST1.@@@.sig",             # 非法 base64
            "ST1." + token.split(".")[1] + ".!!!",  # 非法签名编码
            "ST1.bm90anNvbg.sig",      # 载荷非 JSON 对象
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertEqual(
                    st.verify(bad, KEY, now=NOW).reason, st.Failure.MALFORMED
                )


class ClockBoundaryTests(unittest.TestCase):
    """时钟边界：恰好到期、恰好超出容忍窗口。"""

    SKEW = 60

    def v(self, token, now):
        return st.verify(token, KEY, now=now, max_skew=self.SKEW)

    def test_exactly_at_expiry_still_valid(self):
        token = issue_default()
        self.assertTrue(self.v(token, NOW + 3600).ok)  # now == exp

    def test_exactly_at_expiry_plus_skew_still_valid(self):
        token = issue_default()
        self.assertTrue(self.v(token, NOW + 3600 + self.SKEW).ok)  # now == exp+skew

    def test_one_second_beyond_tolerance_expired(self):
        token = issue_default()
        result = self.v(token, NOW + 3600 + self.SKEW + 1)
        self.assertEqual(result.reason, st.Failure.EXPIRED)

    def test_exactly_at_nbf_minus_skew_valid(self):
        token = issue_default(not_before=NOW + 300)
        self.assertTrue(self.v(token, NOW + 300 - self.SKEW).ok)  # now == nbf-skew

    def test_one_second_before_nbf_tolerance_not_yet_valid(self):
        token = issue_default(not_before=NOW + 300)
        result = self.v(token, NOW + 300 - self.SKEW - 1)
        self.assertEqual(result.reason, st.Failure.NOT_YET_VALID)

    def test_iat_exactly_at_skew_limit_valid(self):
        token = issue_default()  # iat == NOW
        self.assertTrue(self.v(token, NOW - self.SKEW).ok)  # iat == now+skew

    def test_iat_beyond_skew_limit_rejected(self):
        token = issue_default()
        result = self.v(token, NOW - self.SKEW - 1)
        self.assertEqual(result.reason, st.Failure.CLOCK_SKEW_TOO_LARGE)

    def test_zero_skew_configuration(self):
        token = issue_default()
        self.assertTrue(st.verify(token, KEY, now=NOW + 3600, max_skew=0).ok)
        self.assertEqual(
            st.verify(token, KEY, now=NOW + 3601, max_skew=0).reason,
            st.Failure.EXPIRED,
        )


class ConstantTimeTests(unittest.TestCase):
    """恒定时间比较验证。

    1. 静态检查：实现必须使用 hmac.compare_digest（CPython 为纯 C 的
       恒定时间比较，不随前缀匹配长度提前退出）。
    2. 统计检查：对“首字节即不匹配”与“仅末字节不匹配”的签名分别计时，
       两者均值应无显著差异。计时测试本质上是统计性的，取多批次的
       最小均值以抑制调度噪声，并给出宽松阈值。
    """

    def test_implementation_uses_compare_digest(self):
        source = inspect.getsource(st.verify)
        self.assertIn("hmac.compare_digest", source)
        self.assertNotIn("==", source.split("compare_digest")[0].split("expected")[1])

    def test_compare_digest_time_independent_of_match_prefix(self):
        rounds = 7
        iterations = 4000
        sig_a = b"\x00" * 32
        sig_b_diff_first = b"\xff" + b"\x00" * 31   # 第 0 字节即不同
        sig_b_diff_last = b"\x00" * 31 + b"\xff"    # 仅最后字节不同

        def measure(candidate):
            best = float("inf")
            for _ in range(rounds):
                start = time.perf_counter()
                for _ in range(iterations):
                    hmac.compare_digest(sig_a, candidate)
                best = min(best, (time.perf_counter() - start) / iterations)
            return best

        t_first = measure(sig_b_diff_first)
        t_last = measure(sig_b_diff_last)
        self.assertLess(
            abs(t_first - t_last) / max(t_first, t_last), 0.25,
            f"compare_digest 耗时随匹配前缀变化: first={t_first:.3e}s last={t_last:.3e}s",
        )

    def test_verify_time_independent_of_signature_match_prefix(self):
        token = issue_default()
        bad_first = tamper_signature(token, 0)    # 签名首字节被改
        bad_last = tamper_signature(token, 31)    # 签名末字节被改
        self.assertEqual(st.verify(bad_first, KEY, now=NOW).reason,
                         st.Failure.SIGNATURE_MISMATCH)
        self.assertEqual(st.verify(bad_last, KEY, now=NOW).reason,
                         st.Failure.SIGNATURE_MISMATCH)

        rounds = 5
        iterations = 2000

        def measure(t):
            best = float("inf")
            for _ in range(rounds):
                start = time.perf_counter()
                for _ in range(iterations):
                    st.verify(t, KEY, now=NOW)
                best = min(best, (time.perf_counter() - start) / iterations)
            return best

        t_first = measure(bad_first)
        t_last = measure(bad_last)
        self.assertLess(
            abs(t_first - t_last) / max(t_first, t_last), 0.25,
            f"verify 耗时随签名匹配前缀变化: first={t_first:.3e}s last={t_last:.3e}s",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
