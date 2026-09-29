"""scheduler 库自测：python3 -m unittest test_scheduler -v"""
import unittest
from datetime import date

from scheduler import (
    Calendar, ConflictError, Event, MovedTo, RecurrenceRule, TimeWindow,
    MONTH_END_CLAMP, MONTH_END_SKIP,
    expand_event, find_conflicts, iter_rule_dates, to_abs_minutes,
)

D = date
W = TimeWindow  # 简写


def single(eid, d, s, e):
    return Event(eid, single_date=d, windows=(W(s, e),))


class TestExpansion(unittest.TestCase):
    def test_daily_interval(self):
        rule = RecurrenceRule("daily", D(2026, 1, 1), interval=3)
        days = list(iter_rule_dates(rule, D(2026, 1, 1), D(2026, 1, 10)))
        self.assertEqual(days, [D(2026, 1, 1), D(2026, 1, 4), D(2026, 1, 7), D(2026, 1, 10)])

    def test_weekly_weekdays(self):
        rule = RecurrenceRule("weekly", D(2026, 9, 7), weekdays=(0, 2))  # 周一、周三
        days = list(iter_rule_dates(rule, D(2026, 9, 1), D(2026, 9, 20)))
        self.assertEqual(days, [D(2026, 9, 7), D(2026, 9, 9), D(2026, 9, 14), D(2026, 9, 16)])

    def test_effective_range(self):
        rule = RecurrenceRule("daily", D(2026, 3, 5), end_date=D(2026, 3, 8))
        days = list(iter_rule_dates(rule, D(2026, 1, 1), D(2026, 12, 31)))
        self.assertEqual(days, [D(2026, 3, 5), D(2026, 3, 6), D(2026, 3, 7), D(2026, 3, 8)])

    def test_multiple_windows_same_day(self):
        ev = Event("e", rule=RecurrenceRule("daily", D(2026, 1, 1)),
                   windows=(W(9 * 60, 10 * 60), W(14 * 60, 15 * 60)))
        insts = expand_event(ev, D(2026, 1, 1), D(2026, 1, 1))
        self.assertEqual(len(insts), 2)
        self.assertEqual((insts[0].start, insts[1].start),
                         (to_abs_minutes(D(2026, 1, 1), 540), to_abs_minutes(D(2026, 1, 1), 840)))


class TestMonthEnd(unittest.TestCase):
    def test_skip_missing_day(self):
        rule = RecurrenceRule("monthly", D(2026, 1, 31), monthdays=(31,),
                              month_end_policy=MONTH_END_SKIP)
        days = list(iter_rule_dates(rule, D(2026, 1, 1), D(2026, 5, 1)))
        # 2 月、4 月没有 31 日 -> 跳过
        self.assertEqual(days, [D(2026, 1, 31), D(2026, 3, 31)])

    def test_clamp_to_month_end(self):
        rule = RecurrenceRule("monthly", D(2026, 1, 31), monthdays=(31,),
                              month_end_policy=MONTH_END_CLAMP)
        days = list(iter_rule_dates(rule, D(2026, 1, 1), D(2026, 5, 1)))
        self.assertEqual(days, [D(2026, 1, 31), D(2026, 2, 28), D(2026, 3, 31), D(2026, 4, 30)])

    def test_leap_year_feb29(self):
        rule = RecurrenceRule("monthly", D(2023, 10, 30), monthdays=(30,),
                              month_end_policy=MONTH_END_CLAMP)
        days = list(iter_rule_dates(rule, D(2024, 2, 1), D(2024, 2, 29)))
        self.assertEqual(days, [D(2024, 2, 29)])  # 闰年 2 月顺延到 29 日

    def test_non_leap_year_feb28(self):
        rule = RecurrenceRule("monthly", D(2023, 1, 30), monthdays=(30,),
                              month_end_policy=MONTH_END_CLAMP)
        days = list(iter_rule_dates(rule, D(2023, 2, 1), D(2023, 2, 28)))
        self.assertEqual(days, [D(2023, 2, 28)])  # 平年 2 月顺延到 28 日

    def test_clamp_dedup(self):
        # 30 和 31 都顺延到 2 月 28 日，只应产生一个实例
        rule = RecurrenceRule("monthly", D(2026, 1, 30), monthdays=(30, 31),
                              month_end_policy=MONTH_END_CLAMP)
        days = list(iter_rule_dates(rule, D(2026, 2, 1), D(2026, 2, 28)))
        self.assertEqual(days, [D(2026, 2, 28)])


class TestConflicts(unittest.TestCase):
    def test_overlap_interval_reported(self):
        a = single("a", D(2026, 5, 1), 9 * 60, 11 * 60)
        b = single("b", D(2026, 5, 1), 10 * 60, 12 * 60)
        cs = find_conflicts(a, b, D(2026, 5, 1), D(2026, 5, 1))
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.overlap_start, to_abs_minutes(D(2026, 5, 1), 600))
        self.assertEqual(c.overlap_end, to_abs_minutes(D(2026, 5, 1), 660))
        self.assertEqual({c.a.event_id, c.b.event_id}, {"a", "b"})

    def test_adjacent_no_conflict(self):
        a = single("a", D(2026, 5, 1), 9 * 60, 10 * 60)
        b = single("b", D(2026, 5, 1), 10 * 60, 11 * 60)  # 首尾相接不算重叠
        self.assertEqual(find_conflicts(a, b, D(2026, 5, 1), D(2026, 5, 1)), [])

    def test_cross_midnight(self):
        a = single("a", D(2026, 5, 1), 22 * 60, 26 * 60)  # 22:00 ~ 次日 02:00
        b = single("b", D(2026, 5, 2), 1 * 60, 3 * 60)
        cs = find_conflicts(a, b, D(2026, 5, 1), D(2026, 5, 2))
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].overlap_start, to_abs_minutes(D(2026, 5, 2), 60))
        self.assertEqual(cs[0].overlap_end, to_abs_minutes(D(2026, 5, 2), 120))

    def test_cross_midnight_starting_day_before_range(self):
        # 每日 22:00~26:00（跨午夜）。查询区间从 5/2 开始，但 5/1 晚开始的
        # 实例延续到 5/2 凌晨 02:00，撞上 5/2 00:00~01:30 的单次事件。
        # 展开候选必须覆盖区间前一天，否则跨午夜实例漏报。
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 5, 1)),
                  windows=(W(22 * 60, 26 * 60),))
        b = single("b", D(2026, 5, 2), 0, 90)
        cs = find_conflicts(a, b, D(2026, 5, 2), D(2026, 5, 2))
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].a.date, D(2026, 5, 1))  # 实例锚点在前一天
        self.assertEqual(cs[0].overlap_start, to_abs_minutes(D(2026, 5, 2), 0))
        self.assertEqual(cs[0].overlap_end, to_abs_minutes(D(2026, 5, 2), 90))

    def test_recurring_vs_single(self):
        a = Event("standup", rule=RecurrenceRule("daily", D(2026, 1, 1)),
                  windows=(W(9 * 60, 9 * 60 + 30),))
        b = single("doctor", D(2026, 6, 10), 9 * 60 + 15, 10 * 60)
        cs = find_conflicts(a, b, D(2026, 1, 1), D(2026, 12, 31))
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].a.date, D(2026, 6, 10))

    def test_no_conflict_when_outside_effective_range(self):
        a = Event("x", rule=RecurrenceRule("daily", D(2026, 1, 1), end_date=D(2026, 3, 1)),
                  windows=(W(9 * 60, 10 * 60),))
        b = single("y", D(2026, 5, 1), 9 * 60, 10 * 60)
        self.assertEqual(find_conflicts(a, b, D(2026, 1, 1), D(2026, 12, 31)), [])


class TestExceptions(unittest.TestCase):
    def test_skip_exception_removes_conflict(self):
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 1, 1)),
                  windows=(W(9 * 60, 10 * 60),),
                  exceptions={D(2026, 5, 1): None})  # 5/1 跳过
        b = single("b", D(2026, 5, 1), 9 * 60, 10 * 60)
        self.assertEqual(find_conflicts(a, b, D(2026, 1, 1), D(2026, 12, 31)), [])

    def test_moved_exception_creates_conflict(self):
        # 规则实例原本 5/1 09:00，改期到 5/2 09:00，撞上 5/2 的另一日程
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 5, 1), end_date=D(2026, 5, 1)),
                  windows=(W(9 * 60, 10 * 60),),
                  exceptions={D(2026, 5, 1): MovedTo(D(2026, 5, 2))})
        b = single("b", D(2026, 5, 2), 9 * 60 + 30, 11 * 60)
        cs = find_conflicts(a, b, D(2026, 5, 1), D(2026, 5, 3))
        self.assertEqual(len(cs), 1)
        inst = cs[0].a if cs[0].a.event_id == "a" else cs[0].b
        self.assertEqual(inst.moved_from, D(2026, 5, 1))  # 例外优先于规则
        self.assertEqual(inst.date, D(2026, 5, 2))
        self.assertEqual(cs[0].overlap_start, to_abs_minutes(D(2026, 5, 2), 570))
        self.assertEqual(cs[0].overlap_end, to_abs_minutes(D(2026, 5, 2), 600))

    def test_moved_exception_with_new_window(self):
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 5, 1), end_date=D(2026, 5, 1)),
                  windows=(W(9 * 60, 10 * 60),),
                  exceptions={D(2026, 5, 1): MovedTo(D(2026, 5, 2), (W(20 * 60, 21 * 60),))})
        insts = expand_event(a, D(2026, 5, 1), D(2026, 5, 3))
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0].start, to_abs_minutes(D(2026, 5, 2), 20 * 60))

    def test_original_date_no_longer_conflicts_after_move(self):
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 5, 1), end_date=D(2026, 5, 1)),
                  windows=(W(9 * 60, 10 * 60),),
                  exceptions={D(2026, 5, 1): MovedTo(D(2026, 5, 2))})
        b = single("b", D(2026, 5, 1), 9 * 60, 10 * 60)  # 原日期已让位
        self.assertEqual(find_conflicts(a, b, D(2026, 5, 1), D(2026, 5, 3)), [])

    def test_moved_conflict_detected_when_range_starts_on_new_date(self):
        # 规则实例原本只在 5/1，被改期到 5/2；查询区间恰好从新日期 5/2 开始。
        # 原日期（5/1）落在查询区间外一天，候选展开必须包含它，否则改期
        # 实例不会生成，5/2 09:30 起的冲突会被漏报。
        a = Event("a", rule=RecurrenceRule("daily", D(2026, 5, 1), end_date=D(2026, 5, 1)),
                  windows=(W(9 * 60, 10 * 60),),
                  exceptions={D(2026, 5, 1): MovedTo(D(2026, 5, 2))})
        b = single("b", D(2026, 5, 2), 9 * 60 + 30, 11 * 60)
        cs = find_conflicts(a, b, D(2026, 5, 2), D(2026, 5, 2))
        self.assertEqual(len(cs), 1)
        inst = cs[0].a if cs[0].a.event_id == "a" else cs[0].b
        self.assertEqual(inst.date, D(2026, 5, 2))
        self.assertEqual(inst.moved_from, D(2026, 5, 1))
        self.assertEqual(cs[0].overlap_start, to_abs_minutes(D(2026, 5, 2), 570))
        self.assertEqual(cs[0].overlap_end, to_abs_minutes(D(2026, 5, 2), 600))


class TestCalendar(unittest.TestCase):
    def test_insert_conflict_raises(self):
        cal = Calendar()
        cal.insert(single("a", D(2026, 5, 1), 9 * 60, 10 * 60), D(2026, 5, 1), D(2026, 5, 1))
        with self.assertRaises(ConflictError) as ctx:
            cal.insert(single("b", D(2026, 5, 1), 9 * 60 + 30, 11 * 60),
                       D(2026, 5, 1), D(2026, 5, 1))
        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertNotIn("b", cal.events)

    def test_insert_ok_when_free(self):
        cal = Calendar()
        cal.insert(single("a", D(2026, 5, 1), 9 * 60, 10 * 60), D(2026, 5, 1), D(2026, 5, 1))
        cal.insert(single("b", D(2026, 5, 1), 10 * 60, 11 * 60), D(2026, 5, 1), D(2026, 5, 1))
        self.assertEqual(len(cal.events), 2)


class TestLongRange(unittest.TestCase):
    def test_long_range_expansion_count(self):
        # 100 年每日展开：273 年内的天数校验（含闰年）
        rule = RecurrenceRule("daily", D(2000, 1, 1))
        days = list(iter_rule_dates(rule, D(2000, 1, 1), D(2099, 12, 31)))
        self.assertEqual(len(days), (D(2099, 12, 31) - D(2000, 1, 1)).days + 1)  # 36525
        self.assertEqual(days[0], D(2000, 1, 1))
        self.assertEqual(days[-1], D(2099, 12, 31))

    def test_long_range_monthly_leap_correctness(self):
        rule = RecurrenceRule("monthly", D(2000, 1, 29), monthdays=(29,),
                              month_end_policy=MONTH_END_CLAMP)
        days = list(iter_rule_dates(rule, D(2000, 1, 1), D(2000, 12, 31)))
        self.assertEqual(days[1], D(2000, 2, 29))  # 2000 是闰年
        rule2 = RecurrenceRule("monthly", D(2100, 1, 29), monthdays=(29,),
                               month_end_policy=MONTH_END_CLAMP)
        days2 = list(iter_rule_dates(rule2, D(2100, 1, 1), D(2100, 12, 31)))
        self.assertEqual(days2[1], D(2100, 2, 28))  # 2100 不是闰年


if __name__ == "__main__":
    unittest.main()
