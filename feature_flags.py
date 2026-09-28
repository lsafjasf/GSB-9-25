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

规则集搬运与时间阶梯灰度（本次迭代新增）：
  - export_bundle / import_bundle：规则集序列化为带元信息的 JSON 包，
    可在环境间搬运；import 后整体编译校验，非法配置直接拒收。
  - diff_configs：对比两个版本的配置，逐条列出 flag/规则的新增、删除
    与参数变化（default、env、窗口、比例/阶梯、规则顺序），
    结果完全确定（排序输出），便于断言。
  - 规则可携带 rollout（时间阶梯）：给出 (time, percentage) 档序列，
    随时间自动抬高灰度比例；阶梯时间必须严格递增、比例必须在 [0,100]
    且单调不减（可 plateau，不可回退），越界配置编译期即被拒绝。
    因为分桶阈值是“抬高”的（bucket < threshold），上一档已放量的用户
    在下一档必然仍放量，分桶本身 (salt,flag,user) 跨时间/跨版本保持稳定。
"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BUCKET_COUNT = 10_000  # 万分桶，灰度精度 0.01%
_CACHE_MAX = 200_000   # 分桶缓存上限，超出后整体清空重建（简单有界 LRU 替代）
RULESET_FORMAT = "feature-flags/ruleset/v1"


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
class CompiledStage:
    """时间阶梯的一档：at（epoch 秒）起，灰度比例为 percentage。"""
    at: float
    percentage: float          # 0..100
    threshold: int             # round(percentage * 100)，0..10000


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    value: Any
    threshold: int                 # 静态百分比时 0..10000；rollout 规则时为 0
    window_start: Optional[float]  # epoch 秒，含端点
    window_end: Optional[float]    # epoch 秒，含端点
    window_valid: bool             # start < end 才有效；非法窗口永不命中
    order: int
    stages: Tuple[CompiledStage, ...] = ()  # 非空表示时间阶梯灰度

    @property
    def is_rollout(self) -> bool:
        return bool(self.stages)


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


def _pct_number(flag_key: str, rule_id: str, pct: Any, where: str) -> float:
    """校验单个比例：必须是数值（拒绝 bool）且在 [0,100]。"""
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        raise ValueError(
            f"flag {flag_key!r} 规则 {rule_id!r} {where}的比例必须是数字，"
            f"实际为 {pct!r}"
        )
    pct = float(pct)
    if not (0 <= pct <= 100):
        raise ValueError(
            f"flag {flag_key!r} 规则 {rule_id!r} {where}的比例 {pct} 越界，"
            f"必须在 [0, 100] 内"
        )
    return pct


def _compile_rollout(flag_key: str, rule_id: str,
                     raw: Dict[str, Any]) -> Tuple[CompiledStage, ...]:
    """编译规则的 rollout 时间阶梯；非法阶梯直接抛 ValueError。

    规则：
      - rollout 必须是非空列表，元素含 time 与 percentage；
      - rollout 与静态 percentage 互斥（同一规则不得同时声明）；
      - 每档比例在 [0,100]，时间必须严格递增，比例必须单调不减
        （允许持平形成 plateau，但禁止回退，保证已放量用户不被收回）；
      - 每档阈值 round(percentage*100) 也必须单调不减
        （拒绝在 0.01% 精度内造成实际回退的配置，如 0.001% 后 0.0009%）。
    """
    stages_raw = raw.get("rollout")
    if stages_raw is None:
        return ()
    if "percentage" in raw:
        raise ValueError(
            f"flag {flag_key!r} 规则 {rule_id!r} 不能同时声明 percentage 与 rollout，"
            f"二选一"
        )
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValueError(
            f"flag {flag_key!r} 规则 {rule_id!r} 的 rollout 必须是非空列表，"
            f"实际为 {stages_raw!r}"
        )
    stages: List[CompiledStage] = []
    prev_at: Optional[float] = None
    prev_pct = -1.0
    prev_threshold = -1
    for idx, st in enumerate(stages_raw):
        if not isinstance(st, dict) or "time" not in st or "percentage" not in st:
            raise ValueError(
                f"flag {flag_key!r} 规则 {rule_id!r} rollout 第 {idx} 档"
                f"必须是含 time 与 percentage 的对象，实际为 {st!r}"
            )
        at = _parse_time(st["time"])
        pct = _pct_number(flag_key, rule_id, st["percentage"],
                          where=f"rollout 第 {idx} 档")
        threshold = round(pct * 100)
        if prev_at is not None and at <= prev_at:
            raise ValueError(
                f"flag {flag_key!r} 规则 {rule_id!r} rollout 时间必须严格递增："
                f"第 {idx} 档时间 {_format_epoch(at)} 不晚于上一档 "
                f"{_format_epoch(prev_at)}"
            )
        if pct < prev_pct or threshold < prev_threshold:
            raise ValueError(
                f"flag {flag_key!r} 规则 {rule_id!r} rollout 比例必须单调不减，"
                f"不允许回退：第 {idx} 档 {pct}% < 上一档 {prev_pct}%"
            )
        stages.append(CompiledStage(at=at, percentage=pct, threshold=threshold))
        prev_at, prev_pct, prev_threshold = at, pct, threshold
    return tuple(stages)


def _format_epoch(ts: float) -> str:
    """epoch 秒 -> 规范化 UTC ISO 字符串（差异对比/报错统一口径）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _rollout_threshold(stages: Tuple[CompiledStage, ...],
                       now: float) -> Tuple[int, int]:
    """求 now 时刻阶梯所处档位：返回 (生效阈值, 档位下标，-1 表示尚未开始)。"""
    active_idx = -1
    for idx, st in enumerate(stages):
        if now >= st.at:
            active_idx = idx
        else:
            break
    if active_idx < 0:
        return 0, -1
    return stages[active_idx].threshold, active_idx


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
            rule_id = str(raw.get("id", f"rule-{order}"))
            stages = _compile_rollout(key, rule_id, raw)
            if stages:
                threshold = 0  # 阈值由求值时刻按阶梯解析
            else:
                pct = raw.get("percentage", 100)
                if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                    raise ValueError(
                        f"flag {key!r} 规则 {rule_id!r} 的 percentage 必须是数字，"
                        f"实际为 {pct!r}"
                    )
                if not (0 <= pct <= 100):
                    raise ValueError(
                        f"flag {key!r} 规则 {rule_id!r} 的 percentage={pct} 越界，"
                        f"必须在 [0, 100] 内"
                    )
                threshold = int(pct * 100)
            rules.append(CompiledRule(
                rule_id=rule_id,
                value=raw.get("value"),
                threshold=threshold,
                window_start=ws,
                window_end=we,
                window_valid=valid,
                order=order,
                stages=stages,
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
        need_bucket = False
        for r in flag.rules:
            if r.is_rollout:
                if any(0 < st.threshold < BUCKET_COUNT for st in r.stages):
                    need_bucket = True
            elif 0 < r.threshold < BUCKET_COUNT:
                need_bucket = True
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
            if rule.is_rollout:
                active_threshold, active_idx = _rollout_threshold(
                    rule.stages, now)
                if active_idx < 0:
                    first = rule.stages[0]
                    trace.append(
                        f"rule {rule.rule_id!r}: 阶梯灰度未开始"
                        f"（首档 {_format_epoch(first.at)} "
                        f"{first.percentage}%），跳过"
                    )
                    continue
                st = rule.stages[active_idx]
                trace.append(
                    f"rule {rule.rule_id!r}: 阶梯灰度处于第 {active_idx} 档"
                    f"（{_format_epoch(st.at)} 起 {st.percentage}%，"
                    f"threshold={st.threshold}）"
                )
            else:
                active_threshold = rule.threshold
            if active_threshold == 0:
                trace.append(f"rule {rule.rule_id!r}: percentage=0，永不命中")
                continue
            if active_threshold < BUCKET_COUNT:
                if bucket is None or bucket >= active_threshold:
                    trace.append(
                        f"rule {rule.rule_id!r}: 分桶未命中"
                        f"（threshold={active_threshold}）"
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

    def bucket(self, flag_key: str, user_id: str) -> int:
        """对外暴露的稳定分桶 [0,10000)：同一 (salt,flag,user) 永不漂移。"""
        return self._compiled.bucket_of(flag_key, str(user_id))

    def rollout_plan(self, flag_key: str,
                     rule_id: Optional[str] = None) -> Optional["RolloutPlanView"]:
        """返回某条（阶梯）规则在本快照时刻的档位推进情况；非阶梯/不存在返回 None。"""
        flag = self._compiled.flags.get(flag_key)
        if flag is None:
            return None
        rule = None
        for cand in flag.rules:
            if cand.is_rollout and (rule_id is None or cand.rule_id == rule_id):
                rule = cand
                break
        if rule is None:
            return None
        threshold, idx = _rollout_threshold(rule.stages, self._now)
        return RolloutPlanView(
            rule_id=rule.rule_id,
            stages=tuple(StageView(at=st.at, at_iso=_format_epoch(st.at),
                                   percentage=st.percentage,
                                   threshold=st.threshold)
                         for st in rule.stages),
            active_index=idx,
            active_threshold=threshold,
            now=self._now,
        )


@dataclass(frozen=True)
class StageView:
    at: float
    at_iso: str
    percentage: float
    threshold: int


@dataclass(frozen=True)
class RolloutPlanView:
    rule_id: str
    stages: Tuple[StageView, ...]
    active_index: int          # -1 表示尚未到首档
    active_threshold: int
    now: float

    @property
    def started(self) -> bool:
        return self.active_index >= 0

    @property
    def active_percentage(self) -> Optional[float]:
        if self.active_index < 0:
            return None
        return self.stages[self.active_index].percentage


# ---------------------------------------------------------------- 规则集导入导出

def export_bundle(config: Dict[str, Any], *, version: Optional[str] = None,
                  source_env: Optional[str] = None,
                  exported_at: Optional[float] = None) -> Dict[str, Any]:
    """把规则集配置打包为可跨环境搬运的 JSON 包。

    导出前先编译校验：越界比例、非法阶梯等问题在导出侧即失败，
    避免把坏配置搬运到下游环境。返回的是深拷贝，调用方可自由修改。
    """
    compile_config(config)  # 校验，非法配置直接抛 ValueError
    if exported_at is None:
        exported_at = time.time()
    bundle: Dict[str, Any] = {
        "format": RULESET_FORMAT,
        "config": copy.deepcopy(config),
        "meta": {
            "exported_at": _format_epoch(float(exported_at)),
            "flag_count": len((config.get("flags") or {})),
        },
    }
    if version is not None:
        bundle["meta"]["version"] = str(version)
    if source_env is not None:
        bundle["meta"]["source_env"] = str(source_env)
    return bundle


def export_json(config: Dict[str, Any], **kwargs: Any) -> str:
    """export_bundle 的 JSON 字符串形式（缩进 2，UTF-8，可直接落盘/过网）。"""
    return json.dumps(export_bundle(config, **kwargs),
                      ensure_ascii=False, indent=2, sort_keys=False)


def import_bundle(data: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """导入规则集包；做结构与语义双重校验，返回 (config, meta)。

    接受两种输入：
      - 已解析的 dict（裸 config 或带 format 的 bundle）；
      - JSON 字符串 / bytes。
    校验失败（格式不符、内容不是 dict、编译不过）一律抛 ValueError，
    即“越界配置被拒绝”。返回的 config 可直接交给 FlagStore.update。
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"规则集包不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"规则集包必须是 JSON 对象，实际为 {type(data).__name__}")

    if "format" in data or "config" in data:
        if data.get("format") != RULESET_FORMAT:
            raise ValueError(
                f"规则集包 format 不被支持: {data.get('format')!r}，"
                f"期望 {RULESET_FORMAT!r}")
        config = data.get("config")
        if not isinstance(config, dict):
            raise ValueError("规则集包的 config 字段必须是对象")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    else:
        config, meta = data, {}

    compile_config(config)  # 语义校验：越界比例/非法阶梯在此被拒绝
    return copy.deepcopy(config), dict(meta)


# ---------------------------------------------------------------- 版本差异对比

@dataclass(frozen=True)
class AddedEntry:
    kind: str          # flag / rule
    flag: str
    rule: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class RemovedEntry:
    kind: str          # flag / rule
    flag: str
    rule: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class ParamChange:
    flag: str
    scope: str         # flag / rule:<id> / env:<name>
    param: str
    old: Any
    new: Any


@dataclass(frozen=True)
class ConfigDiff:
    added: Tuple[AddedEntry, ...]
    removed: Tuple[RemovedEntry, ...]
    changed: Tuple[ParamChange, ...]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def report(self) -> str:
        """人类可读报告；输出顺序完全确定，可直接快照断言。"""
        lines: List[str] = []
        if self.empty:
            return "两个版本无差异"
        if self.added:
            lines.append(f"新增（{len(self.added)}）：")
            for e in self.added:
                what = f"flag {e.flag!r}" if e.kind == "flag" \
                    else f"flag {e.flag!r} 规则 {e.rule!r}"
                lines.append(f"  + {what}{('：' + e.detail) if e.detail else ''}")
        if self.removed:
            lines.append(f"删除（{len(self.removed)}）：")
            for e in self.removed:
                what = f"flag {e.flag!r}" if e.kind == "flag" \
                    else f"flag {e.flag!r} 规则 {e.rule!r}"
                lines.append(f"  - {what}{('：' + e.detail) if e.detail else ''}")
        if self.changed:
            lines.append(f"参数变化（{len(self.changed)}）：")
            for c in self.changed:
                lines.append(
                    f"  ~ flag {c.flag!r} [{c.scope}] {c.param}: "
                    f"{_diff_show(c.param, c.old)} -> {_diff_show(c.param, c.new)}")
        return "\n".join(lines)


_MISSING = object()  # 差异对比中的“旧版本无此项”哨兵


def _canon_time(v: Any) -> Any:
    if v is None:
        return None
    try:
        return _format_epoch(_parse_time(v))
    except (ValueError, TypeError):
        return v


def _canon_rule(raw: Dict[str, Any], order: int) -> Dict[str, Any]:
    """规范化单条规则，便于两版本逐项对比（时间统一为 epoch UTC ISO）。"""
    window = raw.get("window") or {}
    rollout = raw.get("rollout")
    if rollout is not None:
        pct: Any = None  # rollout 规则不使用静态 percentage
        canon_rollout = tuple(
            (_canon_time(st.get("time")),
             float(st["percentage"]) if not isinstance(st["percentage"], bool)
             else st["percentage"])
            for st in rollout
        )
    else:
        pct = raw.get("percentage", 100)
        canon_rollout = None
    return {
        "id": str(raw.get("id", f"rule-{order}")),
        "value": raw.get("value"),
        "percentage": pct,
        "rollout": canon_rollout,
        "window_start": _canon_time(window["start"]) if "start" in window else None,
        "window_end": _canon_time(window["end"]) if "end" in window else None,
        "order": order,
    }


def _canon_flag(spec: Any) -> Dict[str, Any]:
    spec = spec or {}
    return {
        "default": spec.get("default"),
        "env": {str(k): v for k, v in (spec.get("env") or {}).items()},
        "rules": tuple(_canon_rule(r, i)
                       for i, r in enumerate(spec.get("rules") or [])),
    }


def _unwrap_config(data: Any) -> Dict[str, Any]:
    """diff 入口：允许直接传裸 config，也允许传 import/export 的 bundle。"""
    if isinstance(data, dict) and "config" in data and (
            "format" in data or "meta" in data):
        inner = data["config"]
        if not isinstance(inner, dict):
            raise ValueError("bundle 的 config 字段必须是对象")
        return inner
    if not isinstance(data, dict):
        raise ValueError("待对比的配置必须是 dict 或规则集 bundle")
    return data


def _diff_show(param: str, value: Any) -> str:
    if value is _MISSING:
        return "（无）"
    if param == "percentage":
        if value is None:
            return "（无，使用 rollout 阶梯）"
        return f"{value}%"
    if param == "rollout":
        if value is None:
            return "（无）"
        return "[" + ", ".join(f"{at}@{pct:g}%" for at, pct in value) + "]"
    return repr(value)


def diff_configs(old: Any, new: Any) -> ConfigDiff:
    """对比两个版本的规则集配置，逐条列出新增、删除与参数变化。

    - flag 以 key 为身份；规则以 (flag, rule id) 为身份；
    - 参数变化覆盖：salt、flag 的 default、env 覆盖值、规则的
      value/percentage/rollout 阶梯/时间窗口，以及规则定义顺序；
    - 输出按 (flag, scope, param) 排序，结论完全确定，可直接断言。
    """
    old_cfg = _unwrap_config(old)
    new_cfg = _unwrap_config(new)
    added: List[AddedEntry] = []
    removed: List[RemovedEntry] = []
    changed: List[ParamChange] = []

    old_salt = str(old_cfg.get("salt", "feature-flags"))
    new_salt = str(new_cfg.get("salt", "feature-flags"))
    if old_salt != new_salt:
        changed.append(ParamChange(
            flag="<salt>", scope="config", param="salt",
            old=old_salt, new=new_salt))

    old_flags = old_cfg.get("flags") or {}
    new_flags = new_cfg.get("flags") or {}
    old_keys, new_keys = set(old_flags), set(new_flags)

    for key in sorted(new_keys - old_keys):
        spec = _canon_flag(new_flags[key])
        added.append(AddedEntry(
            "flag", key,
            detail=f"{len(spec['rules'])} 条规则，default={spec['default']!r}"))
    for key in sorted(old_keys - new_keys):
        spec = _canon_flag(old_flags[key])
        removed.append(RemovedEntry(
            "flag", key,
            detail=f"{len(spec['rules'])} 条规则，default={spec['default']!r}"))

    params = ("default",)
    for key in sorted(old_keys & new_keys):
        of = _canon_flag(old_flags[key])
        nf = _canon_flag(new_flags[key])

        for p in params:
            if of[p] != nf[p]:
                changed.append(ParamChange(
                    key, "flag", p, of[p], nf[p]))

        old_env, new_env = of["env"], nf["env"]
        for env_name in sorted(set(old_env) | set(new_env)):
            if env_name not in old_env:
                changed.append(ParamChange(
                    key, f"env:{env_name}", "value",
                    _MISSING, new_env[env_name]))
            elif env_name not in new_env:
                changed.append(ParamChange(
                    key, f"env:{env_name}", "value",
                    old_env[env_name], _MISSING))
            elif old_env[env_name] != new_env[env_name]:
                changed.append(ParamChange(
                    key, f"env:{env_name}", "value",
                    old_env[env_name], new_env[env_name]))

        old_rules = {r["id"]: r for r in of["rules"]}
        new_rules = {r["id"]: r for r in nf["rules"]}
        old_order = [r["id"] for r in of["rules"]]
        new_order = [r["id"] for r in nf["rules"]]

        for rid in sorted(set(new_rules) - set(old_rules)):
            r = new_rules[rid]
            added.append(AddedEntry(
                "rule", key, rule=rid,
                detail=_diff_show("percentage", r["percentage"])
                if r["rollout"] is None
                else f"rollout {_diff_show('rollout', r['rollout'])}"))
        for rid in sorted(set(old_rules) - set(new_rules)):
            r = old_rules[rid]
            removed.append(RemovedEntry(
                "rule", key, rule=rid,
                detail=_diff_show("percentage", r["percentage"])
                if r["rollout"] is None
                else f"rollout {_diff_show('rollout', r['rollout'])}"))

        for rid in sorted(set(old_rules) & set(new_rules)):
            or_, nr = old_rules[rid], new_rules[rid]
            scope = f"rule:{rid}"
            for p in ("value", "percentage", "rollout",
                      "window_start", "window_end"):
                if or_[p] != nr[p]:
                    changed.append(ParamChange(
                        key, scope, p, or_[p], nr[p]))
            # 定义顺序变化（仅对两版本都存在的规则）
            if rid in old_order and rid in new_order:
                oi, ni = old_order.index(rid), new_order.index(rid)
                if oi != ni:
                    changed.append(ParamChange(
                        key, scope, "order", oi, ni))

    changed.sort(key=lambda c: (c.flag, c.scope, c.param))
    return ConfigDiff(tuple(added), tuple(removed), tuple(changed))
