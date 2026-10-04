"""refresh_tokens.py

刷新令牌轮换（Refresh Token Rotation）库，纯 Python 3 标准库实现。

安全性质：
1. 每次刷新签发一对新的访问/刷新令牌，旧刷新令牌立即标记为 used。
2. 已作废的刷新令牌再次出现即判定为重放：整个令牌家族（family）立即失效，
   族内所有访问令牌与刷新令牌（含攻击者刚轮换出来的新令牌）全部被拒。
3. 刷新存在绝对上限：最大轮换次数 max_refreshes 与家族绝对寿命
   max_family_age，任一触发即要求重新认证，家族同样整体作废。
4. 时间通过 time_func 注入（默认 time.time），便于确定性测试。
5. 所有状态变更在同一把锁内完成，并发使用同一刷新令牌时恰有一次成功，
   其余请求触发重放处理。

令牌本身是不透明随机串（secrets.token_urlsafe），状态只存服务端。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "TokenPair",
    "RefreshTokenStore",
    "TokenError",
    "UnknownTokenError",
    "FamilyRevokedError",
    "ReplayDetectedError",
    "RefreshTokenExpiredError",
    "AccessTokenExpiredError",
    "AbsoluteLimitError",
]


class TokenError(Exception):
    """所有令牌相关错误的基类。"""


class UnknownTokenError(TokenError):
    """令牌不存在（从未签发或已不属于本存储）。"""


class FamilyRevokedError(TokenError):
    """令牌家族已被整体作废，任何族内令牌都不得再使用。"""

    def __init__(self, family_id: str, reason: str):
        super().__init__(f"family {family_id} revoked: {reason}")
        self.family_id = family_id
        self.reason = reason


class ReplayDetectedError(FamilyRevokedError):
    """检测到已作废刷新令牌被再次使用（重放），家族随即失效。"""

    def __init__(self, family_id: str):
        TokenError.__init__(self, f"replay detected in family {family_id}")
        self.family_id = family_id
        self.reason = "replay"


class AbsoluteLimitError(FamilyRevokedError):
    """达到刷新绝对上限（轮换次数或家族寿命），必须重新认证。"""

    def __init__(self, family_id: str, limit: str):
        TokenError.__init__(
            self, f"absolute refresh limit ({limit}) reached in family {family_id}"
        )
        self.family_id = family_id
        self.reason = f"absolute_limit:{limit}"
        self.limit = limit


class RefreshTokenExpiredError(TokenError):
    """刷新令牌自身 TTL 已过（此情形不连坐家族，但该令牌不可再用）。"""


class AccessTokenExpiredError(TokenError):
    """访问令牌已过期。"""


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    family_id: str
    subject: str
    refresh_seq: int
    access_expires_at: float
    refresh_expires_at: float


@dataclass
class _RefreshRecord:
    token: str
    family_id: str
    subject: str
    seq: int
    issued_at: float
    expires_at: float
    status: str = "active"  # active | used


@dataclass
class _AccessRecord:
    token: str
    family_id: str
    subject: str
    expires_at: float


@dataclass
class _Family:
    family_id: str
    subject: str
    created_at: float
    refresh_count: int = 0
    revoked: bool = False
    revoke_reason: Optional[str] = None
    refresh_tokens: Dict[str, _RefreshRecord] = field(default_factory=dict)
    access_tokens: Dict[str, _AccessRecord] = field(default_factory=dict)


class RefreshTokenStore:
    """服务端令牌存储：签发、轮换、重放检测、族失效、绝对上限。

    参数均以秒为单位；time_func 可注入虚拟时钟（接收零个参数、返回当前时间）。
    max_refreshes / max_family_age 设为 None 表示对应维度不限，但生产环境
    至少应配置其一，否则令牌家族可无限续期。
    """

    def __init__(
        self,
        access_ttl: float = 60.0,
        refresh_ttl: float = 900.0,
        max_refreshes: Optional[int] = 50,
        max_family_age: Optional[float] = 86400.0,
        time_func: Callable[[], float] = time.time,
    ):
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._max_refreshes = max_refreshes
        self._max_family_age = max_family_age
        self._now = time_func
        self._lock = threading.RLock()
        self._families: Dict[str, _Family] = {}
        self._refresh_index: Dict[str, str] = {}
        self._access_index: Dict[str, str] = {}

    # ---- 公开 API -------------------------------------------------------

    def authenticate(self, subject: str) -> TokenPair:
        """重新认证（登录）后调用：建立全新令牌家族并签发首对令牌。"""
        now = self._now()
        family = _Family(
            family_id="fam_" + secrets.token_urlsafe(12),
            subject=subject,
            created_at=now,
        )
        with self._lock:
            self._families[family.family_id] = family
            return self._issue_pair(family, seq=0, now=now)

    def refresh(self, refresh_token: str) -> TokenPair:
        """用刷新令牌换新令牌对；失败时抛出本模块定义的异常。"""
        now = self._now()
        with self._lock:
            record = self._lookup_refresh(refresh_token)
            family = self._families[record.family_id]

            # 家族已作废：族内任何令牌一律拒绝；若作废原因正是重放且本令牌
            # 已被使用过，按重放上报（并发场景下后到的请求也判定为重放）。
            if family.revoked:
                if family.revoke_reason == "replay" and record.status == "used":
                    raise ReplayDetectedError(family.family_id)
                raise FamilyRevokedError(family.family_id, family.revoke_reason or "?")

            # 重放优先判定：旧令牌再次出现是安全事件，立即族诛。
            if record.status == "used":
                self._revoke_family(family, "replay")
                raise ReplayDetectedError(family.family_id)

            # 刷新令牌自身过期：拒绝本次请求（不连坐家族，合法过期非攻击）。
            if now >= record.expires_at:
                raise RefreshTokenExpiredError(
                    f"refresh token expired at {record.expires_at}, now {now}"
                )

            # 绝对上限：次数或家族寿命触顶即整体作废，强制重新认证。
            hit_limit = self._absolute_limit(family, now)
            if hit_limit is not None:
                self._revoke_family(family, f"absolute_limit:{hit_limit}")
                raise AbsoluteLimitError(family.family_id, hit_limit)

            # 正常轮换：旧令牌作废，签发新令牌对。
            record.status = "used"
            family.refresh_count += 1
            return self._issue_pair(family, seq=record.seq + 1, now=now)

    def validate_access(self, access_token: str) -> str:
        """校验访问令牌，返回 subject；家族被作废时访问令牌同样被拒。"""
        with self._lock:
            family_id = self._access_index.get(access_token)
            if family_id is None:
                raise UnknownTokenError("unknown access token")
            family = self._families[family_id]
            record = family.access_tokens[access_token]
            if family.revoked:
                raise FamilyRevokedError(family.family_id, family.revoke_reason or "?")
            if self._now() >= record.expires_at:
                raise AccessTokenExpiredError("access token expired")
            return record.subject

    def revoke_family(self, family_id: str) -> None:
        """主动登出：作废整个家族。"""
        with self._lock:
            family = self._families.get(family_id)
            if family is None:
                raise UnknownTokenError(f"unknown family {family_id}")
            self._revoke_family(family, "logout")

    # ---- 状态查询（供测试/运维断言）------------------------------------

    def family_status(self, family_id: str) -> Tuple[bool, int, Optional[str]]:
        """返回 (是否已作废, 已成功刷新次数, 作废原因)。"""
        with self._lock:
            family = self._families.get(family_id)
            if family is None:
                raise UnknownTokenError(f"unknown family {family_id}")
            return family.revoked, family.refresh_count, family.revoke_reason

    def family_tokens(self, family_id: str) -> Tuple[List[str], List[str]]:
        """返回族内历史上签发过的全部 (刷新令牌, 访问令牌)，用于族失效断言。"""
        with self._lock:
            family = self._families[family_id]
            return (
                [r.token for r in family.refresh_tokens.values()],
                [r.token for r in family.access_tokens.values()],
            )

    # ---- 内部实现 -------------------------------------------------------

    def _lookup_refresh(self, token: str) -> _RefreshRecord:
        family_id = self._refresh_index.get(token)
        if family_id is None:
            raise UnknownTokenError("unknown refresh token")
        return self._families[family_id].refresh_tokens[token]

    def _absolute_limit(self, family: _Family, now: float) -> Optional[str]:
        if (
            self._max_refreshes is not None
            and family.refresh_count >= self._max_refreshes
        ):
            return "max_refreshes"
        if (
            self._max_family_age is not None
            and now >= family.created_at + self._max_family_age
        ):
            return "max_family_age"
        return None

    def _revoke_family(self, family: _Family, reason: str) -> None:
        family.revoked = True
        family.revoke_reason = reason

    def _issue_pair(self, family: _Family, seq: int, now: float) -> TokenPair:
        refresh_token = "rt_" + secrets.token_urlsafe(24)
        access_token = "at_" + secrets.token_urlsafe(24)

        refresh_record = _RefreshRecord(
            token=refresh_token,
            family_id=family.family_id,
            subject=family.subject,
            seq=seq,
            issued_at=now,
            expires_at=now + self._refresh_ttl,
        )
        access_record = _AccessRecord(
            token=access_token,
            family_id=family.family_id,
            subject=family.subject,
            expires_at=now + self._access_ttl,
        )
        family.refresh_tokens[refresh_token] = refresh_record
        family.access_tokens[access_token] = access_record
        self._refresh_index[refresh_token] = family.family_id
        self._access_index[access_token] = family.family_id

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            family_id=family.family_id,
            subject=family.subject,
            refresh_seq=seq,
            access_expires_at=access_record.expires_at,
            refresh_expires_at=refresh_record.expires_at,
        )
