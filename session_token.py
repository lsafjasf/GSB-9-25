"""Stateless session token issuing and verification (stdlib only).

Token format:  v1.<payload_b64url>.<sig_b64url>

- The payload is a JSON object serialized deterministically
  (sorted keys, compact separators, UTF-8), then base64url-encoded
  without padding.
- The signature is HMAC-SHA256 over the ASCII bytes of
  "v1." + payload_b64url, i.e. the fixed prefix AND the payload,
  so neither can be swapped or truncated without detection.
- Signature comparison uses hmac.compare_digest (constant time).

Payload envelope (all integer seconds since the Unix epoch):
    {
        "v": 1,                 # envelope version
        "iat": <issued at>,
        "nbf": <not before>,
        "exp": <expires at>,
        "jti": <random nonce, hex>,   # from the injected random source
        "data": <application payload>
    }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

PREFIX = b"v1"
ENVELOPE_VERSION = 1
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 60
DEFAULT_MAX_PAYLOAD_BYTES = 4096
_NONCE_BYTES = 16


class Reason(Enum):
    """Machine-distinguishable verification outcomes."""

    OK = "ok"
    MALFORMED = "malformed"                # structure/shape is invalid
    BAD_SIGNATURE = "bad_signature"        # HMAC does not match
    EXPIRED = "expired"                    # now > exp + skew
    NOT_YET_VALID = "not_yet_valid"        # now < nbf - skew
    CLOCK_SKEW_TOO_LARGE = "clock_skew_too_large"  # iat too far in the future


@dataclass(frozen=True)
class Result:
    ok: bool
    reason: Reason
    payload: Optional[Dict[str, Any]] = None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    alphabet = "-_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    if not text or any(c not in alphabet for c in text):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _canonical_json(obj: Any) -> bytes:
    """Deterministic serialization: sorted keys, compact separators."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sign(key: bytes, payload_b64: str) -> str:
    mac = hmac.new(key, PREFIX + b"." + payload_b64.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


def _constant_time_equals(a: str, b: str) -> bool:
    # hmac.compare_digest never short-circuits on a matching prefix;
    # both inputs are ASCII base64url strings of the HMAC.
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


def issue(
    data: Dict[str, Any],
    key: bytes,
    *,
    now: Optional[int] = None,
    ttl: int = DEFAULT_TTL_SECONDS,
    not_before: Optional[int] = None,
    rng: Optional[Callable[[int], bytes]] = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> str:
    """Issue a token for ``data``.

    ``rng`` is the random source for the nonce (default: secrets.token_bytes).
    Given the same ``now`` and ``rng``, issuing the same payload twice
    produces byte-identical tokens (reproducible).
    """
    if not isinstance(data, dict):
        raise TypeError("payload data must be a dict")
    if not key:
        raise ValueError("key must be non-empty")
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    if now is None:
        import time

        now = int(time.time())
    nbf = now if not_before is None else not_before
    nonce = (rng or secrets.token_bytes)(_NONCE_BYTES)
    envelope = {
        "v": ENVELOPE_VERSION,
        "iat": now,
        "nbf": nbf,
        "exp": nbf + ttl,
        "jti": nonce.hex(),
        "data": data,
    }
    raw = _canonical_json(envelope)
    if len(raw) > max_payload_bytes:
        raise ValueError(
            f"payload too large: {len(raw)} bytes > {max_payload_bytes} limit"
        )
    payload_b64 = _b64url_encode(raw)
    return f"{PREFIX.decode()}.{payload_b64}.{_sign(key, payload_b64)}"


def verify(
    token: str,
    key: bytes,
    *,
    now: Optional[int] = None,
    max_clock_skew: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> Result:
    """Verify ``token``. Checks, in order: structure, signature, validity window.

    ``max_clock_skew`` is the tolerance window applied symmetrically to
    ``nbf``/``exp`` and used to detect an implausibly large clock offset
    against ``iat``.
    """
    if now is None:
        import time

        now = int(time.time())

    # ---- 1. structural checks (format only; payload not yet parsed) -----
    if not isinstance(token, str) or not key:
        return Result(False, Reason.MALFORMED)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX.decode():
        return Result(False, Reason.MALFORMED)
    payload_b64, sig_b64 = parts[1], parts[2]
    try:
        raw = _b64url_decode(payload_b64)
        sig_bytes = _b64url_decode(sig_b64)
    except ValueError:
        return Result(False, Reason.MALFORMED)
    if len(raw) == 0 or len(raw) > max_payload_bytes:
        return Result(False, Reason.MALFORMED)
    if len(sig_bytes) != hashlib.sha256().digest_size:
        return Result(False, Reason.MALFORMED)
    # ---- 2. signature check (constant time) ------------------------------
    # Authenticate BEFORE parsing the payload: never deserialize
    # unattested input.
    expected_sig = _sign(key, payload_b64)
    if not _constant_time_equals(expected_sig, sig_b64):
        return Result(False, Reason.BAD_SIGNATURE)

    # ---- 3. structural checks (payload content) --------------------------
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Result(False, Reason.MALFORMED)
    if not isinstance(envelope, dict):
        return Result(False, Reason.MALFORMED)
    required = ("v", "iat", "nbf", "exp", "jti", "data")
    if any(field not in envelope for field in required):
        return Result(False, Reason.MALFORMED)
    if envelope["v"] != ENVELOPE_VERSION:
        return Result(False, Reason.MALFORMED)
    if not all(
        isinstance(envelope[f], int) and not isinstance(envelope[f], bool)
        for f in ("iat", "nbf", "exp")
    ):
        return Result(False, Reason.MALFORMED)

    # ---- 4. validity window ----------------------------------------------
    iat, nbf, exp = envelope["iat"], envelope["nbf"], envelope["exp"]
    if iat - now > max_clock_skew:
        # Token claims to be issued implausibly far in the future:
        # either a forgery attempt or the verifier's clock is badly off.
        return Result(False, Reason.CLOCK_SKEW_TOO_LARGE)
    if now > exp + max_clock_skew:
        return Result(False, Reason.EXPIRED)
    if now < nbf - max_clock_skew:
        return Result(False, Reason.NOT_YET_VALID)

    return Result(True, Reason.OK, envelope["data"])
