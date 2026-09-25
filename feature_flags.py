"""feature_flags — 分层特性开关求值库（仅 Python 3 标准库）。

分层与优先级（高 -> 低）：
  1. rules 规则列表：按定义顺序逐条匹配，先定义者优先；
     每条规则可携带 window（时间窗口）与 percentage（分桶灰度，0~100）。
  2. env 环境覆盖：按当前环境名取固定值。
  3. default 默认值。
  4. 开关缺失：返回调用方传入的 fallback，并在求值轨迹中留痕。

一致性快照：
  FlagStore.snapshot() 返回一个 Snapshot，它持有编译后不可变配置的引用，
  并把 now 固定在快照创建时刻。快照内多次求值看到同一份配置与同一时刻；
  FlagStore.update() 整体替换配置，旧快照不受影响；编译后的配置创建后
  不再被修改，因此并发读取快照无需加锁即线程安全。

冲突消解：
  同优先级（同在 rules 层）多条规则同时命中且结论相反时，
  “先定义者胜”（确定性，与求值顺序无关），被压制的规则记入轨迹 conflicts。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BUCKET_COUNT = 10_000  # 万分桶，灰度精度 0.01%
_CACHE_MAX = 200_000   # 分桶缓存上限，超出后整体清空重建（简单有界 LRU 替代）


# ---------------------------------------------------------------- 配置编译

def _parse_time(value: Any) -> float:
    """把 ISO 8601 字符串或 epoch 秒数解析为 epoch 秒（UTC）。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # 朴素时间按 UTC 处理
        return dt.timestamp()
    raise ValueError(f"无法解析的时间值: {value!r}")


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    value: Any
    threshold: int                 # 0..10000，bucket < threshold 即命中
    window_start: Optional[float]  # epoch 秒，含端点
    window_end: Optional[float]    # epoch 秒，含端点
    window_valid: bool             # start < end 才有效；非法窗口永不命中
    order: int


@dataclass(frozen=True)
class CompiledFlag:
    key: str
    default: Any
    env: Tuple[Tuple[str, Any], ...]   # 冻结为 tuple，保证不可变
    rules: Tuple[CompiledRule, ...]

    def env_value(self, env: str) -> Tuple[bool, Any]:
        for name, value in self.env:
            if name == env:
                return True, value
        return False, None


class CompiledConfig:
    """编译后的不可变配置：创建后不再修改，可安全地被多线程/多快照共享。"""

    __slots__ = ("salt", "flags", "warnings", "_bucket_cache", "_cache_lock")

    def __init__(self, salt: str, flags: Dict[str, CompiledFlag],
                 warnings: List[str]):
        self.salt = salt
        self.flags = flags            # 仅读，不再增删改
        self.warnings = warnings      # 编译期校验告警（如非法时间窗口）
        self._bucket_cache: Dict[Tuple[str, str], int] = {}
        self._cache_lock = threading.Lock()

    def bucket_of(self, flag_key: str, user_id: str) -> int:
        """稳定分桶：sha1(salt:flag:user) -> [0, 10000)。带缓存。"""
        cache_key = (flag_key, user_id)
        with self._cache_lock:
            cached = self._bucket_cache.get(cache_key)
        if cached is not None:
            return cached
        digest = hashlib.sha1(
            f"{self.salt}:{flag_key}:{user_id}".encode("utf-8")
        ).digest()
        bucket = int.from_bytes(digest[:8], "big") % BUCKET_COUNT
        with self._cache_lock:
            if len(self._bucket_cache) >= _CACHE_MAX:
                self._bucket_cache.clear()
            self._bucket_cache[cache_key] = bucket
        return bucket


def compile_config(config: Dict[str, Any]) -> CompiledConfig:
    """把原始 dict 配置编译为不可变结构；非法配置在编译期暴露。

    - percentage 越界（不在 [0,100]）：直接抛 ValueError（明确失败）。
    - 时间窗口非法（start >= end）：规则保留但标记 window_valid=False，
      永不命中，并记入 CompiledConfig.warnings。
    """
    salt = str(config.get("salt", "feature-flags"))
    warnings: List[str] = []
    flags: Dict[str, CompiledFlag] = {}

    for key, spec in (config.get("flags") or {}).items():
        rules: List[CompiledRule] = []
        for order, raw in enumerate(spec.get("rules") or []):
            pct = raw.get("percentage", 100)
            if not (0 <= pct <= 100):
                raise ValueError(
                    f"flag {key!r} 规则 {raw.get('id')!r} 的 percentage={pct} 越界，"
                    f"必须在 [0, 100] 内"
                )
            window = raw.get("window") or {}
            ws = _parse_time(window["start"]) if "start" in window else None
            we = _parse_time(window["end"]) if "end" in window else None
            valid = True
            if ws is not None and we is not None and ws >= we:
                valid = False
                warnings.append(
                    f"flag {key!r} 规则 {raw.get('id')!r} 时间窗口非法 "
                    f"(start >= end)，该规则永不生效"
                )
            rules.append(CompiledRule(
                rule_id=str(raw.get("id", f"rule-{order}")),
                value=raw.get("value"),
                threshold=int(pct * 100),
                window_start=ws,
                window_end=we,
                window_valid=valid,
                order=order,
            ))
        flags[key] = CompiledFlag(
            key=key,
            default=spec.get("default"),
            env=tuple((str(k), v) for k, v in (spec.get("env") or {}).items()),
            rules=tuple(rules),
        )
    return CompiledConfig(salt=salt, flags=flags, warnings=warnings)


# ---------------------------------------------------------------- 求值结果

@dataclass(frozen=True)
class Evaluation:
    flag_key: str
    value: Any
    layer: str                 # rule / env / default / fallback
    trace: Tuple[str, ...]     # 命中的规则链（可解释性）
    conflicts: Tuple[str, ...] # 同优先级冲突留痕

    def explain(self) -> str:
        lines = [f"flag={self.flag_key} value={self.value!r} layer={self.layer}"]
        lines += [f"  - {step}" for step in self.trace]
        lines += [f"  ! {c}" for c in self.conflicts]
        return "\n".join(lines)


# ---------------------------------------------------------------- 求值核心

def _evaluate(compiled: CompiledConfig, env: str, now: float,
              flag_key: str, user_id: Optional[str], fallback: Any) -> Evaluation:
    trace: List[str] = []
    conflicts: List[str] = []

    flag = compiled.flags.get(flag_key)
    if flag is None:
        trace.append(f"flag {flag_key!r} 未配置 -> 返回调用方 fallback={fallback!r}")
        return Evaluation(flag_key, fallback, "fallback", tuple(trace), ())

    # 第 1 层：rules（含时间窗口 + 分桶灰度）
    if flag.rules:
        need_bucket = any(0 < r.threshold < BUCKET_COUNT for r in flag.rules)
        bucket: Optional[int] = None
        if need_bucket:
            if user_id is None:
                trace.append("存在灰度规则但未提供 user_id，所有灰度规则视为未命中")
            else:
                bucket = compiled.bucket_of(flag_key, str(user_id))
                trace.append(f"user={user_id!r} 分桶 bucket={bucket}/10000")

        winner: Optional[CompiledRule] = None
        for rule in flag.rules:
            if not rule.window_valid:
                trace.append(f"rule {rule.rule_id!r}: 时间窗口非法，跳过")
                continue
            if rule.window_start is not None and now < rule.window_start:
                trace.append(f"rule {rule.rule_id!r}: 未到窗口起点，跳过")
                continue
            if rule.window_end is not None and now > rule.window_end:
                trace.append(f"rule {rule.rule_id!r}: 已过窗口终点，跳过")
                continue
            if rule.threshold == 0:
                trace.append(f"rule {rule.rule_id!r}: percentage=0，永不命中")
                continue
            if rule.threshold < BUCKET_COUNT:
                if bucket is None or bucket >= rule.threshold:
                    trace.append(
                        f"rule {rule.rule_id!r}: 分桶未命中"
                        f"（threshold={rule.threshold}）"
                    )
                    continue
            # 命中
            if winner is None:
                winner = rule
                trace.append(
                    f"rule {rule.rule_id!r}: 命中 -> value={rule.value!r}"
                )
            elif rule.value != winner.value:
                conflicts.append(
                    f"冲突: rule {rule.rule_id!r} 同样命中且结论相反"
                    f"（{rule.value!r} vs {winner.value!r}），"
                    f"按定义顺序由 {winner.rule_id!r} 胜出"
                )
            else:
                trace.append(f"rule {rule.rule_id!r}: 命中但结论一致，忽略")

        if winner is not None:
            return Evaluation(flag_key, winner.value, "rule",
                              tuple(trace), tuple(conflicts))

    # 第 2 层：环境覆盖
    hit, value = flag.env_value(env)
    if hit:
        trace.append(f"env={env!r} 命中环境覆盖 -> value={value!r}")
        return Evaluation(flag_key, value, "env", tuple(trace), tuple(conflicts))
    trace.append(f"env={env!r} 无环境覆盖")

    # 第 3 层：默认值
    trace.append(f"回落默认值 -> value={flag.default!r}")
    return Evaluation(flag_key, flag.default, "default",
                      tuple(trace), tuple(conflicts))


# ---------------------------------------------------------------- 存储与快照

class FlagStore:
    """持有当前配置；update 整体替换，snapshot 取一致性快照。"""

    def __init__(self, config: Dict[str, Any], env: Optional[str] = None):
        self._lock = threading.Lock()
        self._compiled = compile_config(config)
        self._env = env if env is not None else os.environ.get("APP_ENV", "dev")

    def update(self, config: Dict[str, Any]) -> None:
        """原子替换配置；已创建的快照持有旧编译结果，不受影响。"""
        new_compiled = compile_config(config)
        with self._lock:
            self._compiled = new_compiled

    def snapshot(self, now: Optional[float] = None) -> "Snapshot":
        # 读引用是原子操作（GIL）；CompiledConfig 创建后不可变，无需加锁拷贝
        compiled = self._compiled
        return Snapshot(compiled, self._env,
                        time.time() if now is None else float(now))

    def evaluate(self, flag_key: str, user_id: Optional[str] = None,
                 fallback: Any = None) -> Evaluation:
        return self.snapshot().evaluate(flag_key, user_id, fallback)


class Snapshot:
    """一致性快照：配置与 now 在创建时固定，多次求值结果一致。"""

    __slots__ = ("_compiled", "_env", "_now")

    def __init__(self, compiled: CompiledConfig, env: str, now: float):
        self._compiled = compiled
        self._env = env
        self._now = now

    @property
    def now(self) -> float:
        return self._now

    @property
    def compile_warnings(self) -> List[str]:
        return list(self._compiled.warnings)

    def evaluate(self, flag_key: str, user_id: Optional[str] = None,
                 fallback: Any = None) -> Evaluation:
        return _evaluate(self._compiled, self._env, self._now,
                         flag_key, user_id, fallback)

    def is_enabled(self, flag_key: str, user_id: Optional[str] = None) -> bool:
        return bool(self.evaluate(flag_key, user_id, fallback=False).value)
