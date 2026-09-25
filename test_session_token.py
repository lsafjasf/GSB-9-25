"""Self-tests for session_token (stdlib unittest only).

Run:  python3 -m unittest -v test_session_token
"""

import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest import mock

import session_token as st

KEY = b"test-key-0123456789abcdef"
NOW = 1_700_000_000
TTL = 3600
SKEW = 60


def fixed_rng(n):
    """Deterministic random source for reproducible issuance."""
    return bytes(range(n))


def issue(data=None, **kw):
    args = dict(now=NOW, ttl=TTL, rng=fixed_rng)
    args.update(kw)
    return st.issue(data if data is not None else {"uid": 42, "role": "admin"},
                    KEY, **args)


def split(token):
    return token.split(".")


def craft_token(envelope, key=KEY):
    """Build a correctly-signed token from a raw envelope dict."""
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(key, b"v1." + payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"v1.{payload_b64}.{sig_b64}"


class RoundTripTests(unittest.TestCase):
    def test_ok_roundtrip(self):
        token = issue()
        res = st.verify(token, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, st.Reason.OK)
        self.assertEqual(res.payload, {"uid": 42, "role": "admin"})

    def test_deterministic_serialization_reproducible(self):
        # Same input + same random source -> byte-identical tokens.
        t1 = issue()
        t2 = issue()
        self.assertEqual(t1, t2)
        # Key order in the input dict must not matter.
        t3 = st.issue({"role": "admin", "uid": 42}, KEY,
                      now=NOW, ttl=TTL, rng=fixed_rng)
        self.assertEqual(t1, t3)

    def test_duplicate_issuance_distinct_nonces_with_real_rng(self):
        # Default rng (secrets) makes each token unique even for same payload.
        t1 = st.issue({"a": 1}, KEY, now=NOW, ttl=TTL)
        t2 = st.issue({"a": 1}, KEY, now=NOW, ttl=TTL)
        self.assertNotEqual(t1, t2)
        self.assertTrue(st.verify(t1, KEY, now=NOW).ok)
        self.assertTrue(st.verify(t2, KEY, now=NOW).ok)

    def test_wrong_key_rejected(self):
        token = issue()
        res = st.verify(token, b"other-key-0123456789abcdef",
                        now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.BAD_SIGNATURE)


class TamperTests(unittest.TestCase):
    def test_payload_modified(self):
        prefix, payload_b64, sig = split(issue())
        # Flip one payload character to a different valid base64url char.
        i = len(payload_b64) // 2
        orig = payload_b64[i]
        repl = "A" if orig != "A" else "B"
        tampered = f"{prefix}.{payload_b64[:i]}{repl}{payload_b64[i+1:]}.{sig}"
        res = st.verify(tampered, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.BAD_SIGNATURE)

    def test_payload_swapped_with_other_valid_payload(self):
        # Attacker replaces the payload with a different, well-formed one
        # but keeps the original signature.
        prefix, _, sig = split(issue())
        other = split(issue(data={"uid": 1, "role": "user"}))[1]
        res = st.verify(f"{prefix}.{other}.{sig}", KEY, now=NOW,
                        max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.BAD_SIGNATURE)

    def test_signature_truncated(self):
        token = issue()
        truncated = token[:-1]
        res = st.verify(truncated, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.MALFORMED)

    def test_signature_one_bit_flipped(self):
        prefix, payload_b64, sig = split(issue())
        i = len(sig) // 2
        repl = "A" if sig[i] != "A" else "B"
        tampered = f"{prefix}.{payload_b64}.{sig[:i]}{repl}{sig[i+1:]}"
        res = st.verify(tampered, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.BAD_SIGNATURE)

    def test_missing_field(self):
        env = {"v": 1, "iat": NOW, "nbf": NOW, "jti": "00", "data": {}}
        token = craft_token(env)  # correctly signed, but "exp" missing
        res = st.verify(token, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.MALFORMED)

    def test_missing_segments_and_bad_prefix(self):
        _, payload_b64, sig = split(issue())
        for bad in ("", "v1", f"v1.{payload_b64}", f"v2.{payload_b64}.{sig}",
                    f"v1.{payload_b64}.{sig}.extra", "a.b.c.d"):
            res = st.verify(bad, KEY, now=NOW, max_clock_skew=SKEW)
            self.assertEqual(res.reason, st.Reason.MALFORMED, msg=bad)

    def test_invalid_base64_and_json(self):
        res = st.verify("v1.!!!.AAAA", KEY, now=NOW)
        self.assertEqual(res.reason, st.Reason.MALFORMED)
        not_json = base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode()
        sig = hmac.new(KEY, b"v1." + not_json.encode(),
                       hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        res = st.verify(f"v1.{not_json}.{sig_b64}", KEY, now=NOW)
        self.assertEqual(res.reason, st.Reason.MALFORMED)

    def test_oversized_payload(self):
        big = {"blob": "x" * (st.DEFAULT_MAX_PAYLOAD_BYTES + 1)}
        with self.assertRaises(ValueError):
            st.issue(big, KEY, now=NOW, ttl=TTL, rng=fixed_rng)
        # A correctly-signed token whose payload exceeds the limit is
        # rejected at the structural stage.
        env = {"v": 1, "iat": NOW, "nbf": NOW, "exp": NOW + TTL,
               "jti": "00", "data": big}
        token = craft_token(env)
        res = st.verify(token, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertEqual(res.reason, st.Reason.MALFORMED)

    def test_wrong_version_and_non_int_times(self):
        base = {"v": 1, "iat": NOW, "nbf": NOW, "exp": NOW + TTL,
                "jti": "00", "data": {}}
        for mutate in ({"v": 2}, {"exp": "soon"}, {"iat": True}):
            env = {**base, **mutate}
            res = st.verify(craft_token(env), KEY, now=NOW,
                            max_clock_skew=SKEW)
            self.assertEqual(res.reason, st.Reason.MALFORMED, msg=str(mutate))


class ClockBoundaryTests(unittest.TestCase):
    """exp = NOW + TTL, nbf = NOW, skew = SKEW."""

    def setUp(self):
        self.token = issue()
        self.exp = NOW + TTL

    def check(self, now):
        return st.verify(self.token, KEY, now=now,
                         max_clock_skew=SKEW).reason

    def test_exactly_at_expiry_still_valid(self):
        self.assertEqual(self.check(self.exp), st.Reason.OK)

    def test_exactly_at_expiry_plus_skew_still_valid(self):
        self.assertEqual(self.check(self.exp + SKEW), st.Reason.OK)

    def test_one_second_beyond_tolerance_expired(self):
        self.assertEqual(self.check(self.exp + SKEW + 1), st.Reason.EXPIRED)

    def test_far_future_expired(self):
        self.assertEqual(self.check(self.exp + 10 * SKEW), st.Reason.EXPIRED)

    def test_not_yet_valid_boundary(self):
        future_nbf = NOW + 1000
        token = issue(now=NOW, not_before=future_nbf)
        # exactly nbf - skew -> valid
        self.assertEqual(
            st.verify(token, KEY, now=future_nbf - SKEW,
                      max_clock_skew=SKEW).reason, st.Reason.OK)
        # one second earlier -> not yet valid
        self.assertEqual(
            st.verify(token, KEY, now=future_nbf - SKEW - 1,
                      max_clock_skew=SKEW).reason, st.Reason.NOT_YET_VALID)

    def test_clock_skew_too_large(self):
        # Token issued 10 minutes in the "future" relative to verifier.
        token = issue(now=NOW + 600)
        # iat - now == 600 > SKEW -> clock skew too large
        self.assertEqual(
            st.verify(token, KEY, now=NOW, max_clock_skew=SKEW).reason,
            st.Reason.CLOCK_SKEW_TOO_LARGE)
        # exactly at the tolerance boundary -> accepted
        token_edge = issue(now=NOW + SKEW)
        self.assertEqual(
            st.verify(token_edge, KEY, now=NOW, max_clock_skew=SKEW).reason,
            st.Reason.OK)

    def test_zero_skew_configuration(self):
        res = st.verify(self.token, KEY, now=self.exp + 1, max_clock_skew=0)
        self.assertEqual(res.reason, st.Reason.EXPIRED)
        res = st.verify(self.token, KEY, now=self.exp, max_clock_skew=0)
        self.assertEqual(res.reason, st.Reason.OK)


class ConstantTimeTests(unittest.TestCase):
    def test_compare_digest_is_used(self):
        token = issue()
        with mock.patch.object(st.hmac, "compare_digest",
                               wraps=st.hmac.compare_digest) as spy:
            st.verify(token, KEY, now=NOW, max_clock_skew=SKEW)
        self.assertTrue(spy.called)

    def test_compare_time_independent_of_match_prefix(self):
        """Time to compare must not grow with the matching-prefix length.

        A short-circuiting compare returns earlier when the mismatch is at
        the start; a constant-time compare scans the whole input either way.
        We time mismatch-at-first-char vs mismatch-at-last-char and require
        the medians to be within a tight relative band.
        """
        _, _, sig = split(issue())
        n = len(sig)
        flip_first = ("A" if sig[0] != "A" else "B") + sig[1:]
        flip_last = sig[:-1] + ("A" if sig[-1] != "A" else "B")

        # Interleave the two cases and keep the min per-call time: the min
        # is the standard robust estimator for CPU-bound micro-benchmarks
        # (noise only ever adds time), and interleaving cancels drift.
        loops, repeats = 20000, 15
        best = {"first": float("inf"), "last": float("inf")}
        for _ in range(repeats):
            for name, candidate in (("first", flip_first),
                                    ("last", flip_last)):
                t0 = time.perf_counter()
                for _ in range(loops):
                    st._constant_time_equals(sig, candidate)
                dt = (time.perf_counter() - t0) / loops
                best[name] = min(best[name], dt)
        t_first, t_last = best["first"], best["last"]
        # Constant-time: ratio must be ~1. A prefix-short-circuit compare
        # would show t_last >> t_first. Allow generous jitter margin.
        self.assertLess(t_last / t_first, 1.5,
                        msg=f"first={t_first:.3e}s last={t_last:.3e}s")
        self.assertLess(t_first / t_last, 1.5)


if __name__ == "__main__":
    unittest.main()
