"""Signed, tamper-proof cursor encoding.

A cursor token is ``base64url(payload_json) + "." + base64url(hmac)``.
The payload contains ONLY:

  * ``v``   - cursor format version;
  * ``fp``  - fingerprint of the sort spec the cursor was issued for;
  * ``dir`` - scan direction ("f" forward / "b" backward);
  * ``k``   - the raw sort-key values (sort columns + record id).

It carries no other record fields, so no sensitive data beyond the sort
key values themselves is exposed. The HMAC-SHA256 signature covers the
whole payload: any modification of the sort keys, the direction or the
spec fingerprint is detected and rejected with :class:`CursorTamperedError`.
"""

import base64
import hashlib
import hmac
import json

from .errors import CursorDecodeError, CursorSpecMismatchError, CursorTamperedError

FORWARD = "forward"
BACKWARD = "backward"

_VERSION = 1
_DIR_TO_CODE = {FORWARD: "f", BACKWARD: "b"}
_CODE_TO_DIR = {"f": FORWARD, "b": BACKWARD}


class Cursor:
    """Decoded cursor: a position (sort-key values) plus a scan direction."""

    __slots__ = ("keys", "direction")

    def __init__(self, keys, direction):
        if direction not in (FORWARD, BACKWARD):
            raise ValueError("invalid direction %r" % (direction,))
        self.keys = tuple(keys)
        self.direction = direction

    def __repr__(self):
        return "Cursor(keys=%r, direction=%r)" % (self.keys, self.direction)

    def __eq__(self, other):
        return (
            isinstance(other, Cursor)
            and self.keys == other.keys
            and self.direction == other.direction
        )


def _normalize_secret(secret):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ValueError("secret must be a non-empty str or bytes")
    return bytes(secret)


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret, payload_b64):
    return hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()


def encode_cursor(cursor, spec, secret):
    """Encode and sign ``cursor`` for the given sort spec."""
    secret = _normalize_secret(secret)
    payload = {
        "v": _VERSION,
        "fp": spec.fingerprint(),
        "dir": _DIR_TO_CODE[cursor.direction],
        "k": list(cursor.keys),
    }
    try:
        raw = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "sort key values must be JSON-serializable (got %s)" % exc
        ) from exc
    payload_b64 = _b64encode(raw)
    sig_b64 = _b64encode(_sign(secret, payload_b64))
    return payload_b64 + "." + sig_b64


def decode_cursor(token, spec, secret):
    """Verify and decode a cursor token.

    Raises:
        CursorDecodeError: malformed token or payload.
        CursorTamperedError: signature verification failed (tampered token
            or wrong secret).
        CursorSpecMismatchError: valid cursor issued for another sort spec.
    """
    secret = _normalize_secret(secret)
    if not isinstance(token, str):
        raise CursorDecodeError("cursor token must be a string")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CursorDecodeError("malformed cursor token")
    payload_b64, sig_b64 = parts
    try:
        given_sig = _b64decode(sig_b64)
    except Exception:
        raise CursorTamperedError("cursor signature is not valid base64")
    expected_sig = _sign(secret, payload_b64)
    if not hmac.compare_digest(given_sig, expected_sig):
        raise CursorTamperedError(
            "cursor signature verification failed: "
            "the cursor was tampered with or signed with another secret"
        )
    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        raise CursorDecodeError("cursor payload is not valid JSON")
    if not isinstance(payload, dict):
        raise CursorDecodeError("cursor payload must be a JSON object")
    if payload.get("v") != _VERSION:
        raise CursorDecodeError("unsupported cursor version %r" % (payload.get("v"),))
    if payload.get("fp") != spec.fingerprint():
        raise CursorSpecMismatchError(
            "cursor was issued for a different sort spec "
            "(sort columns or directions do not match)"
        )
    direction = _CODE_TO_DIR.get(payload.get("dir"))
    if direction is None:
        raise CursorDecodeError("invalid cursor direction %r" % (payload.get("dir"),))
    keys = payload.get("k")
    if not isinstance(keys, list) or len(keys) != len(spec.columns) + 1:
        raise CursorDecodeError("cursor key tuple has the wrong shape")
    return Cursor(keys, direction)
