"""日程安排与冲突检测库（仅标准库）。

时间模型：
- 日期用 datetime.date（仅日历，无时区）。
- 一天内的时间用整数“分钟”表示（0..1439）。
- 绝对时刻用整数“绝对分钟”表示 = date.toordinal() * 1440 + 分钟。
- 跨午夜时段：TimeWindow 的 end 允许 > 1440（如 22:00-26:00 表示 22:00 到次日 02:00）。

优先级规则（例外 > 重复规则）：
- exceptions[date] = None            -> 该日期的规则实例被跳过；
- exceptions[date] = MovedTo(...)    -> 该日期的规则实例被移除，改期实例取而代之；
- 改期后的实例与任何其他实例一样参与冲突检测，因此“改期撞上别的日程”会被检出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

MINUTES_PER_DAY = 1440

# 月末/闰年缺失日的显式处理策略
MONTH_END_SKIP = "skip"    # 该月没有此日 -> 跳过本次
MONTH_END_CLAMP = "clamp"  # 该月没有此日 -> 顺延（收拢）到当月最后一天

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"


def _days_in_month(year: int, month: int) -> int:
    if (year, month) == (9999, 12):
        return 31  # date 上限，无法取下月 1 号
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def to_abs_minutes(d: date, minute: int = 0) -> int:
    """日期 + 分钟内偏移 -> 绝对分钟整数。"""
    return d.toordinal() * MINUTES_PER_DAY + minute


def fmt_abs(abs_min: int) -> str:
    """绝对分钟 -> 'YYYY-MM-DD HH:MM' 便于阅读。"""
    day, minute = divmod(abs_min, MINUTES_PER_DAY)
    d = date.fromordinal(day)
    return f"{d.isoformat()} {minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class TimeWindow:
    """一天内的时段 [start, end)，单位分钟。end > 1440 表示跨到次日。"""
    start: int
    end: int

    def normalized(self) -> tuple[int, int]:
        if not 0 <= self.start < MINUTES_PER_DAY:
            raise ValueError(f"start 必须在 [0, 1440) 内: {self.start}")
        end = self.end
        if end <= self.start:
            end += MINUTES_PER_DAY  # end <= start 视为跨午夜
        if end > 2 * MINUTES_PER_DAY:
            raise ValueError(f"时段最长不超过 24 小时: {self}")
        return self.start, end


@dataclass(frozen=True)
class RecurrenceRule:
    """重复规则：负责产生“哪些日期”，时段由 Event.windows 决定。

    freq:
      daily   -> 每 interval 天
      weekly  -> 每 interval 周，在 weekdays 指定的星期（0=周一 .. 6=周日）
      monthly -> 每 interval 月，在 monthdays 指定的日（如 31）；
                 该月没有此日时按 month_end_policy 处理。
    生效区间: [start_date, end_date]，end_date 为 None 表示无限。
    """
    freq: str
    start_date: date
    end_date: date | None = None
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    monthdays: tuple[int, ...] = ()
    month_end_policy: str = MONTH_END_SKIP

    def __post_init__(self):
        if self.interval < 1:
            raise ValueError("interval 必须 >= 1")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        if self.freq == FREQ_WEEKLY:
            if not self.weekdays or any(not 0 <= w <= 6 for w in self.weekdays):
                raise ValueError("weekly 需要合法的 weekdays (0=周一..6=周日)")
        if self.freq == FREQ_MONTHLY:
            if not self.monthdays or any(not 1 <= m <= 31 for m in self.monthdays):
                raise ValueError("monthly 需要合法的 monthdays (1..31)")
            if self.month_end_policy not in (MONTH_END_SKIP, MONTH_END_CLAMP):
                raise ValueError("month_end_policy 必须是 'skip' 或 'clamp'")
        if self.freq not in (FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY):
            raise ValueError(f"未知 freq: {self.freq}")


@dataclass(frozen=True)
class MovedTo:
    """例外：把某天的实例改期。windows 为 None 时沿用事件原有时段。"""
    new_date: date
    windows: tuple[TimeWindow, ...] | None = None


@dataclass
class Event:
    """日程。rule 为 None 时是单次日程（用 single_date）。"""
    event_id: str
    title: str = ""
    windows: tuple[TimeWindow, ...] = (TimeWindow(9 * 60, 10 * 60),)
    rule: RecurrenceRule | None = None
    single_date: date | None = None
    exceptions: dict[date, None | MovedTo] = field(default_factory=dict)

    def __post_init__(self):
        if self.rule is None and self.single_date is None:
            raise ValueError("单次日程必须提供 single_date")
        if self.rule is not None and self.single_date is not None:
            raise ValueError("rule 与 single_date 只能二选一")
        if not self.windows:
            raise ValueError("至少需要一个时段")
        for w in self.windows:
            w.normalized()  # 校验


@dataclass(frozen=True)
class Instance:
    """展开后的具体实例。start/end 为绝对分钟。moved_from 非空表示它是改期实例。"""
    event_id: str
    date: date
    start: int
    end: int
    moved_from: date | None = None

    def fmt(self) -> str:
        tag = f" (改期自 {self.moved_from.isoformat()})" if self.moved_from else ""
        return f"[{self.event_id}] {fmt_abs(self.start)} ~ {fmt_abs(self.end)}{tag}"


@dataclass(frozen=True)
class Conflict:
    """两日程的一次冲突：具体重叠区间 + 双方展开实例。"""
    a: Instance
    b: Instance
    overlap_start: int
    overlap_end: int

    def fmt(self) -> str:
        return (
            f"冲突 {fmt_abs(self.overlap_start)} ~ {fmt_abs(self.overlap_end)}\n"
            f"  A: {self.a.fmt()}\n"
            f"  B: {self.b.fmt()}"
        )


def iter_rule_dates(rule: RecurrenceRule, range_start: date, range_end: date):
    """在 [range_start, range_end] 内展开规则命中的日期（升序、去重）。"""
    lo = max(rule.start_date, range_start)
    hi = min(rule.end_date, range_end) if rule.end_date else range_end
    if lo > hi:
        return

    if rule.freq == FREQ_DAILY:
        delta = (lo - rule.start_date).days
        d = lo + timedelta(days=(-delta) % rule.interval)
        while d <= hi:
            yield d
            d += timedelta(days=rule.interval)

    elif rule.freq == FREQ_WEEKLY:
        anchor = rule.start_date - timedelta(days=rule.start_date.weekday())
        step = 7 * rule.interval
        k = max(0, (lo - anchor).days // step)
        week_start = anchor + timedelta(days=k * step)
        while week_start <= hi:
            for wd in sorted(rule.weekdays):
                d = week_start + timedelta(days=wd)
                if rule.start_date <= d and lo <= d <= hi:
                    yield d
            week_start += timedelta(days=step)

    else:  # monthly
        base = rule.start_date.year * 12 + (rule.start_date.month - 1)
        diff = (lo.year * 12 + lo.month - 1) - base
        k = max(0, -(-diff // rule.interval))  # ceil(diff / interval)
        while True:
            total = base + k * rule.interval
            if total > 9999 * 12 + 11:
                return  # 超出 date 可表示范围
            year, month0 = divmod(total, 12)
            month = month0 + 1
            first = date(year, month, 1)
            if first > hi:
                return
            dim = _days_in_month(year, month)
            emitted: set[date] = set()
            for md in sorted(rule.monthdays):
                if md <= dim:
                    d = date(year, month, md)
                elif rule.month_end_policy == MONTH_END_CLAMP:
                    d = date(year, month, dim)  # 顺延到当月最后一天
                else:
                    continue  # skip：该月没有此日，跳过
                if d in emitted:
                    continue  # clamp 可能让多个 monthday 落到同一天
                emitted.add(d)
                if rule.start_date <= d and lo <= d <= hi:
                    yield d
            k += 1


def expand_event(ev: Event, range_start: date, range_end: date) -> list[Instance]:
    """把事件在 [range_start, range_end] 内展开为实例列表（按 start 升序）。

    例外优先级高于重复规则：先由规则/单次日期产生候选日期，
    再逐日应用 exceptions（跳过或改期），最后按时间范围过滤。
    """
    range_lo = to_abs_minutes(range_start)
    range_hi = to_abs_minutes(range_end) + MINUTES_PER_DAY

    def emit(d: date, windows, moved_from) -> list[Instance]:
        out = []
        for w in windows:
            s, e = w.normalized()
            inst = Instance(ev.event_id, d, to_abs_minutes(d, s), to_abs_minutes(d, e), moved_from)
            if inst.start < range_hi and inst.end > range_lo:
                out.append(inst)
        return out

    instances: list[Instance] = []
    if ev.rule is None:
        candidates = [ev.single_date]
    else:
        # 改期可能把区间外一天的实例移进区间；跨午夜时段也可能从区间前一天
        # 开始、凌晨才进入区间。向前多展开一天，最终仍由上面的绝对时间
        # 范围过滤（inst.start < range_hi and inst.end > range_lo）兜底。
        pre = range_start - timedelta(days=1)
        candidates = iter_rule_dates(ev.rule, pre, range_end)

    for d in candidates:
        if d in ev.exceptions:
            exc = ev.exceptions[d]
            if exc is None:
                continue  # 跳过
            windows = exc.windows if exc.windows is not None else ev.windows
            instances.extend(emit(exc.new_date, windows, moved_from=d))
        else:
            instances.extend(emit(d, ev.windows, moved_from=None))

    instances.sort(key=lambda i: (i.start, i.end))
    return instances


def find_conflicts(ev_a: Event, ev_b: Event,
                   range_start: date, range_end: date) -> list[Conflict]:
    """返回两事件在范围内的所有冲突（含具体重叠区间与双方实例）。"""
    inst_a = expand_event(ev_a, range_start, range_end)
    inst_b = expand_event(ev_b, range_start, range_end)
    conflicts: list[Conflict] = []
    j = 0
    for a in inst_a:
        while j < len(inst_b) and inst_b[j].end <= a.start:
            j += 1
        k = j
        while k < len(inst_b) and inst_b[k].start < a.end:
            b = inst_b[k]
            conflicts.append(Conflict(a, b, max(a.start, b.start), min(a.end, b.end)))
            k += 1
    return conflicts


class ConflictError(Exception):
    def __init__(self, conflicts: list[Conflict]):
        self.conflicts = conflicts
        msg = "插入失败，检测到冲突:\n" + "\n".join(c.fmt() for c in conflicts)
        super().__init__(msg)


class Calendar:
    """日程集合。插入前强制冲突检测。"""

    def __init__(self):
        self.events: dict[str, Event] = {}

    def check_insert(self, ev: Event, range_start: date, range_end: date) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for other in self.events.values():
            if other.event_id == ev.event_id:
                continue
            conflicts.extend(find_conflicts(other, ev, range_start, range_end))
        conflicts.sort(key=lambda c: (c.overlap_start, c.overlap_end))
        return conflicts

    def insert(self, ev: Event, range_start: date, range_end: date) -> Event:
        conflicts = self.check_insert(ev, range_start, range_end)
        if conflicts:
            raise ConflictError(conflicts)
        self.events[ev.event_id] = ev
        return ev
