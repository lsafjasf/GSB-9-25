"""演示：冲突实例输出样例。运行: python3 demo.py"""
from datetime import date

from scheduler import (
    Calendar, ConflictError, Event, MovedTo, RecurrenceRule, TimeWindow,
    MONTH_END_CLAMP, find_conflicts,
)

D = date
W = TimeWindow

# 每周一/三 09:00-10:00 的站会
standup = Event("standup", title="站会",
                rule=RecurrenceRule("weekly", D(2026, 9, 7), weekdays=(0, 2)),
                windows=(W(9 * 60, 10 * 60),))

# 9/16 的站会改期到 9/17 09:30-10:30（例外优先于规则）
standup.exceptions[D(2026, 9, 16)] = MovedTo(D(2026, 9, 17), (W(9 * 60 + 30, 10 * 60 + 30),))

# 每月 31 号 14:00 的复盘（小月顺延到月末）
review = Event("review", title="月度复盘",
               rule=RecurrenceRule("monthly", D(2026, 1, 31), monthdays=(31,),
                                   month_end_policy=MONTH_END_CLAMP),
               windows=(W(14 * 60, 15 * 60),))

# 单次：9/17 09:45-11:00 面试
interview = Event("interview", title="面试", single_date=D(2026, 9, 17),
                  windows=(W(9 * 60 + 45, 11 * 60),))

print("== 站会 vs 面试（改期后的实例撞上面试）==")
for c in find_conflicts(standup, interview, D(2026, 9, 1), D(2026, 9, 30)):
    print(c.fmt())

print("\n== 插入日历（强制冲突检测）==")
cal = Calendar()
cal.insert(standup, D(2026, 9, 1), D(2026, 12, 31))
cal.insert(review, D(2026, 1, 1), D(2026, 12, 31))
try:
    cal.insert(interview, D(2026, 9, 1), D(2026, 9, 30))
except ConflictError as e:
    print(e)
