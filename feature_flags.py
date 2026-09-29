"""feature_flags — 特性开关求值库（仅依赖 Python 标准库）。

分层配置模型（优先级从高到低）：
    1. 时间窗口（gate）：窗口外 / 窗口非法 → 直接返回默认值
    2. 环境覆盖（env override）：当前环境命中 → 返回覆盖值
    3. 用户分桶灰度（rollout）：按稳定哈希分桶，命中比例 → 返回灰度值
    4. 默认值（default）

一致性快照：FlagStore 采用 copy-on-write，snapshot() 抓取某一版本的
不可变配置引用；同一 Snapshot 内多次求值结果一致，配置更新不影响旧快照，
并发读取无需加锁（读路径无锁，写路径持锁替换引用）。

缓存 / 预编译策略：
    - 配置加载时一次性编译（时间窗口 ISO 字符串 → epoch 时间戳、
      灰度规则排序、比例校验），求值路径零解析。
    - 分桶哈希（SHA-256）通过 functools.lru_cache 记忆化，
      同一 (flag_key, user_id, salt) 只计算一次；lru_cache 线程安全。
"""

from __future__ import annotations

import functools
import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger("feature_flags")

# 万分桶：支持两位小数的灰度比例，且取模基数为 100 的整数倍，分布均匀。
BUCKET_COUNT = 10_000


class ConfigError(ValueError):
    """配置非法（加载期即拒绝，行为明确）。"""


# ---------------------------------------------------------------------------
# 分桶
# ---------------------------------------------------------------------------

def stable_bucket(flag_key: str, user_id: str, salt: str = "") -> int:
    """稳定分桶：同一 (flag_key, user_id, salt) 永远得到 [0, BUCKET_COUNT) 中同一桶。"""
    return _cached_bucket(flag_key, user_id, salt)


@functools.lru_cache(maxsize=1_000_000)
def _cached_bucket(flag_key: str, user_id: str, salt: str) -> int:
    digest = hashlib.sha256(
        f"{salt}\x00{flag_key}\x00{user_id}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_COUNT


# ---------------------------------------------------------------------------
# 编译后的配置（预编译：求值路径不再做任何解析）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledWindow:
    start_ts: Optional[float]
    end_ts: Optional[float]
    valid: bool
    error: Optional[str]

    def contains(self, now_ts: float) -> bool:
        if self.start_ts is not None and now_ts < self.start_ts:
            return False
        if self.end_ts is not None and now_ts > self.end_ts:
            return False
        return True


@dataclass(frozen=True)
class CompiledRollout:
    rule_id: str
    value: Any
    percentage: float  # [0, 100]
    salt: str
    priority: int


@dataclass(frozen=True)
class CompiledFlag:
    key: str
    default: Any
    env_overrides: Mapping[str, Any]
    rollouts: tuple  # tuple[CompiledRollout, ...]，已按 (priority desc, rule_id asc) 排序
    window: Optional[CompiledWindow]


def _parse_iso(text: str, field_name: str) -> float:
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"time_window.{field_name} 不是合法 ISO-8601 时间: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # 裸时间一律按 UTC 解释（明确行为）
    return dt.timestamp()


def _compile_window(raw: Optional[Mapping[str, Any]]) -> Optional[CompiledWindow]:
    if raw is None:
        return None
    start_raw, end_raw = raw.get("start"), raw.get("end")
    try:
        start_ts = _parse_iso(start_raw, "start") if start_raw is not None else None
        end_ts = _parse_iso(end_raw, "end") if end_raw is not None else None
    except ConfigError as exc:
        # 非法时间窗口：不抛错中断加载，而是标记为 invalid，
        # 求值时视为窗口永不生效并留痕（明确行为，见 README）。
        logger.warning("非法时间窗口，按永不生效处理: %s", exc)
        return CompiledWindow(None, None, valid=False, error=str(exc))
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        msg = f"time_window.start({start_raw}) 晚于 end({end_raw})"
        logger.warning("非法时间窗口，按永不生效处理: %s", msg)
        return CompiledWindow(None, None, valid=False, error=msg)
    return CompiledWindow(start_ts, end_ts, valid=True, error=None)


def compile_flag(raw: Mapping[str, Any]) -> CompiledFlag:
    """把 JSON 风格的原始配置编译为求值用的不可变结构。非法配置抛 ConfigError。"""
    key = raw.get("key")
    if not key:
        raise ConfigError("flag 缺少 key")
    rollouts = []
    for index, r in enumerate(raw.get("rollouts") or ()):
        pct = r.get("percentage")
        if not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise ConfigError(f"flag {key!r} 的 rollout[{index}].percentage 必须在 [0,100]，得到 {pct!r}")
        rollouts.append(CompiledRollout(
            rule_id=str(r.get("rule_id", f"rollout[{index}]")),
            value=r.get("value", True),
            percentage=float(pct),
            salt=str(r.get("salt", "")),
            priority=int(r.get("priority", 0)),
        ))
    # 预排序：优先级降序、rule_id 升序 —— 冲突消解规则在编译期就固化。
    rollouts.sort(key=lambda r: (-r.priority, r.rule_id))
    return CompiledFlag(
        key=key,
        default=raw.get("default"),
        env_overrides=dict(raw.get("env_overrides") or {}),
        rollouts=tuple(rollouts),
        window=_compile_window(raw.get("time_window")),
    )


# ---------------------------------------------------------------------------
# 求值结果与规则链留痕
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TraceStep:
    rule: str      # 命中的层级，如 "time_window" / "env_override" / "rollout" / "default"
    outcome: str   # hit | miss | conflict | suppressed | error | skip
    detail: str


@dataclass(frozen=True)
class EvalResult:
    key: str
    value: Any
    reason: str               # 最终结论来源：flag_not_found / invalid_time_window /
                              # outside_time_window / env_override / rollout / default
    trace: tuple              # tuple[TraceStep, ...] 完整规则链，可解释
    version: int              # 求值所用的配置版本

    def explain(self) -> str:
        lines = [f"flag={self.key!r} value={self.value!r} reason={self.reason} (config v{self.version})"]
        lines += [f"  [{s.outcome:8s}] {s.rule}: {s.detail}" for s in self.trace]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 存储与快照
# ---------------------------------------------------------------------------

class FlagStore:
    """copy-on-write 配置存储：写时整体替换，读（快照）无锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flags: Mapping[str, CompiledFlag] = {}
        self._version = 0

    def load(self, configs: Iterable[Mapping[str, Any]]) -> int:
        """编译并原子替换全部配置，返回新版本号。非法配置抛 ConfigError 且不影响旧配置。"""
        compiled = {}
        for raw in configs:
            flag = compile_flag(raw)
            if flag.key in compiled:
                raise ConfigError(f"重复的 flag key: {flag.key!r}")
            compiled[flag.key] = flag
        with self._lock:
            self._flags = compiled
            self._version += 1
            return self._version

    @property
    def version(self) -> int:
        return self._version

    def snapshot(self) -> "Snapshot":
        """抓取当前版本的不可变快照；之后的 load 不影响已抓取的快照。"""
        with self._lock:
            return Snapshot(self._flags, self._version)


class Snapshot:
    """一致性快照：同一快照内多次求值看到同一份配置；并发读取安全（只读 + 无锁）。"""

    __slots__ = ("_flags", "_version")

    def __init__(self, flags: Mapping[str, CompiledFlag], version: int) -> None:
        self._flags = flags
        self._version = version

    @property
    def version(self) -> int:
        return self._version

    def evaluate(
        self,
        key: str,
        *,
        user_id: Optional[str] = None,
        env: Optional[str] = None,
        now: Optional[datetime] = None,
        fallback: Any = None,
    ) -> EvalResult:
        trace: list[TraceStep] = []

        flag = self._flags.get(key)
        if flag is None:
            # 配置缺失：返回调用方给的 fallback，留痕，不抛异常（明确行为）。
            trace.append(TraceStep("lookup", "miss", f"flag {key!r} 不存在，返回 fallback"))
            return EvalResult(key, fallback, "flag_not_found", tuple(trace), self._version)

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_ts = now_dt.timestamp()

        # 1) 时间窗口 gate
        if flag.window is not None:
            if not flag.window.valid:
                trace.append(TraceStep("time_window", "error",
                                       f"窗口非法({flag.window.error})，按不生效处理，返回默认值"))
                return EvalResult(key, flag.default, "invalid_time_window", tuple(trace), self._version)
            if not flag.window.contains(now_ts):
                trace.append(TraceStep("time_window", "miss", "当前时间不在窗口内，返回默认值"))
                return EvalResult(key, flag.default, "outside_time_window", tuple(trace), self._version)
            trace.append(TraceStep("time_window", "hit", "当前时间在窗口内，继续向下求值"))

        # 2) 环境覆盖
        if env is not None and env in flag.env_overrides:
            value = flag.env_overrides[env]
            trace.append(TraceStep("env_override", "hit", f"env={env!r} 命中覆盖值 {value!r}"))
            return EvalResult(key, value, "env_override", tuple(trace), self._version)
        trace.append(TraceStep("env_override", "miss", f"env={env!r} 无覆盖"))

        # 3) 用户分桶灰度（同优先级冲突：确定消解 + 留痕）
        if flag.rollouts:
            if user_id is None:
                trace.append(TraceStep("rollout", "skip", "未提供 user_id，跳过分桶，返回默认值"))
            else:
                hits = [
                    r for r in flag.rollouts
                    if stable_bucket(flag.key, user_id, r.salt) < r.percentage * (BUCKET_COUNT // 100)
                ]
                if hits:
                    top_priority = hits[0].priority
                    tier = [r for r in hits if r.priority == top_priority]
                    winner = tier[0]  # 已按 rule_id 升序排序 → 确定消解
                    conflict_losers: list[CompiledRollout] = []
                    if len(tier) > 1 and any(r.value != winner.value for r in tier[1:]):
                        conflict_losers = list(tier[1:])
                        losers = [r.rule_id for r in conflict_losers]
                        trace.append(TraceStep(
                            "rollout", "conflict",
                            f"优先级 {top_priority} 处 {len(tier)} 条规则结论冲突，"
                            f"按 rule_id 字典序确定消解：{winner.rule_id!r} 胜出，被压制: {losers}"))
                        logger.warning("flag %s 灰度规则冲突，%s 胜出，被压制: %s",
                                       key, winner.rule_id, losers)
                    # 所有命中但未胜出的规则都要在规则链里留下去向：
                    # 同优先级结论冲突者已记入 conflict；其余记 suppressed。
                    for r in hits:
                        if r is winner or r in conflict_losers:
                            continue
                        if r.priority == top_priority:
                            detail = (f"规则 {r.rule_id!r} 命中，与 {winner.rule_id!r} 同优先级同结论，"
                                      f"按 rule_id 字典序未胜出，被合并")
                        else:
                            detail = (f"规则 {r.rule_id!r} 命中（优先级 {r.priority}），"
                                      f"被更高优先级 {top_priority} 的规则 {winner.rule_id!r} 压制")
                        trace.append(TraceStep("rollout", "suppressed", detail))
                    bucket = stable_bucket(flag.key, user_id, winner.salt)
                    trace.append(TraceStep(
                        "rollout", "hit",
                        f"规则 {winner.rule_id!r} 命中：bucket={bucket} < "
                        f"{winner.percentage}% → {winner.value!r}"))
                    return EvalResult(key, winner.value, "rollout", tuple(trace), self._version)
                trace.append(TraceStep("rollout", "miss", "未落入任何灰度比例"))

        # 4) 默认值
        trace.append(TraceStep("default", "hit", f"返回默认值 {flag.default!r}"))
        return EvalResult(key, flag.default, "default", tuple(trace), self._version)
