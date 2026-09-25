"""会话令牌签发与校验库（仅依赖 Python 标准库）。

令牌格式（三段，点号分隔）::

    ST1.<payload_b64url>.<signature_b64url>

- 固定前缀 ``ST1`` 标识版本与算法套件。
- 载荷为规范 JSON（键排序、无空白、ASCII 转义），经无填充 base64url 编码。
- 签名为 ``HMAC-SHA256(key, "ST1." + payload_b64url)``，即签名同时覆盖
  固定前缀与载荷编码，前缀不可被剥离或替换。

确定性：同一载荷、同一时钟值、同一随机源（nonce）签发的令牌完全一致。
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

PREFIX = "ST1"
SIGNATURE_BYTES = hashlib.sha256().digest_size
DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_SKEW_SECONDS = 60
DEFAULT_MAX_PAYLOAD_BYTES = 4096


class Failure(enum.Enum):
    """校验失败原因，可区分。"""

    MALFORMED = "malformed"                      # 结构非法
    SIGNATURE_MISMATCH = "signature_mismatch"    # 签名不匹配
    EXPIRED = "expired"                          # 已过期
    NOT_YET_VALID = "not_yet_valid"              # 尚未生效
    CLOCK_SKEW_TOO_LARGE = "clock_skew_too_large"  # 签发方时钟偏移过大


class PayloadTooLargeError(ValueError):
    """签发时载荷序列化后超出大小上限。"""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: Optional[Failure] = None
    claims: Optional[dict] = None


def _canonical_json(obj: Any) -> bytes:
    """确定性序列化：键排序、最紧凑分隔符、非 ASCII 转义。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """严格解码无填充 base64url；非法字符或长度一律抛 ValueError。"""
    if not text or len(text) % 4 == 1:
        raise ValueError("invalid base64url length")
    if not all(c.isalnum() or c in "-_" for c in text):
        raise ValueError("invalid base64url alphabet")
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


def _fail(reason: Failure) -> VerifyResult:
    return VerifyResult(ok=False, reason=reason)


def issue(
    payload: Mapping[str, Any],
    key: bytes,
    *,
    now: Optional[int] = None,
    ttl: int = DEFAULT_TTL_SECONDS,
    not_before: Optional[int] = None,
    rand: Optional[Callable[[int], bytes]] = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> str:
    """签发令牌。

    - ``now``：签发时刻（Unix 秒），默认取系统时钟；注入固定值可复现。
    - ``rand``：随机源，签名为 ``rand(nbytes) -> bytes``，默认
      ``secrets.token_bytes``；注入确定性随机源可复现。
    - ``not_before``：生效时刻，默认等于 ``now``。
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("key must be at least 16 bytes")
    issued_at = int(time.time()) if now is None else int(now)
    nbf = issued_at if not_before is None else int(not_before)
    nonce = (rand if rand is not None else secrets.token_bytes)(16)
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) == 0:
        raise ValueError("rand must return non-empty bytes")

    claims = {
        "iat": issued_at,
        "nbf": nbf,
        "exp": issued_at + int(ttl),
        "nonce": _b64encode(bytes(nonce)),
        "data": payload,
    }
    body = _canonical_json(claims)
    if len(body) > max_payload_bytes:
        raise PayloadTooLargeError(
            f"payload {len(body)} bytes exceeds limit {max_payload_bytes}"
        )
    payload_b64 = _b64encode(body)
    sig = hmac.new(
        bytes(key), f"{PREFIX}.{payload_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{PREFIX}.{payload_b64}.{_b64encode(sig)}"


def verify(
    token: str,
    key: bytes,
    *,
    now: Optional[int] = None,
    max_skew: int = DEFAULT_MAX_SKEW_SECONDS,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> VerifyResult:
    """校验令牌，依次检查：结构 -> 签名 -> 有效期。

    有效期规则（``max_skew`` 为可配置的时钟偏移容忍窗口）：

    - ``iat > now + max_skew``：签发方时钟偏移过大；
    - ``now < nbf - max_skew``：尚未生效；
    - ``now > exp + max_skew``：已过期；
    - 边界取包含：``now == exp + max_skew`` 仍视为有效。
    """
    now_ts = int(time.time()) if now is None else int(now)

    # ---- 1. 结构检查 ----
    if not isinstance(token, str):
        return _fail(Failure.MALFORMED)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        return _fail(Failure.MALFORMED)
    try:
        body = _b64decode(parts[1])
        sig = _b64decode(parts[2])
    except (ValueError, binascii.Error):
        return _fail(Failure.MALFORMED)
    if len(sig) != SIGNATURE_BYTES:  # 含签名被截断
        return _fail(Failure.MALFORMED)
    if len(body) > max_payload_bytes:  # 超长载荷
        return _fail(Failure.MALFORMED)
    try:
        claims = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail(Failure.MALFORMED)
    if not isinstance(claims, dict):
        return _fail(Failure.MALFORMED)
    for field in ("iat", "nbf", "exp"):
        if field not in claims or not isinstance(claims[field], int):
            return _fail(Failure.MALFORMED)
    if "nonce" not in claims or "data" not in claims:
        return _fail(Failure.MALFORMED)

    # ---- 2. 签名检查（恒定时间比较）----
    expected = hmac.new(
        bytes(key), f"{PREFIX}.{parts[1]}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, sig):
        return _fail(Failure.SIGNATURE_MISMATCH)

    # ---- 3. 有效期检查 ----
    if claims["iat"] > now_ts + max_skew:
        return _fail(Failure.CLOCK_SKEW_TOO_LARGE)
    if now_ts < claims["nbf"] - max_skew:
        return _fail(Failure.NOT_YET_VALID)
    if now_ts > claims["exp"] + max_skew:
        return _fail(Failure.EXPIRED)
    return VerifyResult(ok=True, claims=claims)
