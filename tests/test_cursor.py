"""Cursor codec, tamper-resistance and payload-privacy tests."""

import base64
import json
import unittest

from cursor_pagination import (
    BACKWARD,
    FORWARD,
    Cursor,
    CursorDecodeError,
    CursorSpecMismatchError,
    CursorTamperedError,
    SortSpec,
    decode_cursor,
    encode_cursor,
)

SECRET = "test-secret-key"
SPEC = SortSpec([("created_at", "desc"), ("price", "asc")])


def make_token(keys=(100, 9.5, 42), direction=FORWARD, spec=SPEC, secret=SECRET):
    return encode_cursor(Cursor(keys, direction), spec, secret)


def read_payload(token):
    payload_b64 = token.split(".")[0]
    pad = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + pad))


def forge_token(payload_dict):
    """Re-encode a modified payload WITHOUT re-signing (attacker has no secret)."""
    raw = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode()
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return forged + "." + make_token().split(".")[1]


class RoundTripTest(unittest.TestCase):
    def test_round_trip_forward_and_backward(self):
        for direction in (FORWARD, BACKWARD):
            cur = Cursor((1700000000, 19.99, "row-7"), direction)
            token = encode_cursor(cur, SPEC, SECRET)
            back = decode_cursor(token, SPEC, SECRET)
            self.assertEqual(back, cur)

    def test_round_trip_with_nulls(self):
        cur = Cursor((None, None, 5), FORWARD)
        token = encode_cursor(cur, SPEC, SECRET)
        self.assertEqual(decode_cursor(token, SPEC, SECRET), cur)

    def test_non_serializable_key_rejected(self):
        cur = Cursor((b"\x00raw-bytes", 1, 2), FORWARD)
        with self.assertRaises(ValueError):
            encode_cursor(cur, SPEC, SECRET)

    def test_nan_key_rejected(self):
        cur = Cursor((float("nan"), 1, 2), FORWARD)
        with self.assertRaises(ValueError):
            encode_cursor(cur, SPEC, SECRET)


class TamperTest(unittest.TestCase):
    def test_modified_sort_key_rejected(self):
        payload = read_payload(make_token())
        payload["k"][0] = 999999  # attacker moves the position
        with self.assertRaises(CursorTamperedError):
            decode_cursor(forge_token(payload), SPEC, SECRET)

    def test_modified_direction_rejected(self):
        payload = read_payload(make_token(direction=FORWARD))
        payload["dir"] = "b"  # attacker flips scan direction
        with self.assertRaises(CursorTamperedError):
            decode_cursor(forge_token(payload), SPEC, SECRET)

    def test_modified_spec_fingerprint_rejected(self):
        payload = read_payload(make_token())
        payload["fp"] = "0" * 16
        with self.assertRaises(CursorTamperedError):
            decode_cursor(forge_token(payload), SPEC, SECRET)

    def test_truncated_signature_rejected(self):
        token = make_token()
        body, sig = token.split(".")
        with self.assertRaises(CursorTamperedError):
            decode_cursor(body + "." + sig[:-4] + "AAAA", SPEC, SECRET)

    def test_wrong_secret_rejected(self):
        token = make_token()
        with self.assertRaises(CursorTamperedError):
            decode_cursor(token, SPEC, "another-secret")

    def test_malformed_tokens_rejected(self):
        for bad in ("", "no-dot-here", "a.b.c", ".sig", "body.", "!!!.!!!"):
            with self.assertRaises((CursorDecodeError, CursorTamperedError), msg=bad):
                decode_cursor(bad, SPEC, SECRET)

    def test_valid_cursor_for_other_sort_spec_rejected(self):
        other = SortSpec([("created_at", "asc"), ("price", "asc")])  # direction differs
        token = encode_cursor(Cursor((1, 2, 3), FORWARD), other, SECRET)
        with self.assertRaises(CursorSpecMismatchError):
            decode_cursor(token, SPEC, SECRET)

    def test_cursor_for_extra_column_spec_rejected(self):
        other = SortSpec([("created_at", "desc"), ("price", "asc"), ("name", "asc")])
        token = encode_cursor(Cursor((1, 2, 3, 4), FORWARD), other, SECRET)
        with self.assertRaises(CursorSpecMismatchError):
            decode_cursor(token, SPEC, SECRET)


class PayloadPrivacyTest(unittest.TestCase):
    def test_payload_carries_only_position_data(self):
        token = make_token(keys=(1700000000, 19.99, "row-7"))
        payload = read_payload(token)
        self.assertEqual(set(payload), {"v", "fp", "dir", "k"})
        self.assertEqual(payload["k"], [1700000000, 19.99, "row-7"])
        # no other record fields (name, email, ...) are embedded
        self.assertNotIn("name", json.dumps(payload))
        self.assertNotIn("email", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
