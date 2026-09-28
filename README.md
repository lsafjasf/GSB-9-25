# 日程安排与冲突检测库（Python 3，仅标准库）

时间模型：日期用 `datetime.date`（纯日历、无时区），时刻用整数分钟表示
（`绝对分钟 = date.toordinal() * 1440 + 当日分钟`）。跨午夜时段用
`end > 1440` 表示（如 22:00–26:00 即 22:00 到次日 02:00）。

## 文件

- `scheduler.py` — 库源码
- `test_scheduler.py` — 22 个自测（unittest）
- `demo.py` — 冲突实例输出样例
- `benchmark.py` — 规模基准（百万次展开耗时）

## 重复规则

`RecurrenceRule(freq, start_date, end_date=None, interval=1, weekdays=(), monthdays=(), month_end_policy="skip")`

- `daily`：每 `interval` 天。
- `weekly`：每 `interval` 周，`weekdays` 指定星期（0=周一 … 6=周日），可多天。
- `monthly`：每 `interval` 月，`monthdays` 指定日（1..31），可多日。
- 生效区间 `[start_date, end_date]`，`end_date=None` 表示无限。
- 同一天多次实例：在 `Event.windows` 里放多个 `TimeWindow`。

### 月末与闰年（显式策略 `month_end_policy`）

- `"skip"`：该月没有此日（如 2 月 31 日）则跳过本次。
- `"clamp"`：顺延（收拢）到当月最后一天；平年 2 月 → 28 日，闰年 2 月 → 29 日；
  多个 monthday 收拢到同一天时自动去重。

对应测试：`TestMonthEnd`（skip/clamp/闰年 2·29/平年 2·28/收拢去重）与
`TestLongRange.test_long_range_monthly_leap_correctness`（2000 是闰年、2100 不是）。

## 例外日期与优先级

`Event.exceptions: dict[date, None | MovedTo]`，键是规则原本命中的日期：

- 值为 `None` → 该日实例被**跳过**；
- 值为 `MovedTo(new_date, windows=None)` → 该日实例被移除，**改期**到新日期
  （`windows` 缺省沿用原时段，也可指定新时段）。

**优先级：例外 > 重复规则。** 展开时先由规则产生候选日期，再逐日应用例外，
改期实例（`Instance.moved_from` 记录原日期）与其他实例一样参与冲突检测，
因此“改期后撞上另一日程”一定会被检出（见
`TestExceptions.test_moved_exception_creates_conflict`）。

## 冲突检测

`find_conflicts(ev_a, ev_b, range_start, range_end)` 返回 `Conflict` 列表，
每条包含具体重叠区间 `[overlap_start, overlap_end)` 与双方展开实例
（哪天、哪一时段、是否改期）。`Calendar.insert()` 插入前强制检测，
冲突时抛 `ConflictError` 且不入库。相邻不重叠（10:00 结束 vs 10:00 开始）不算冲突。

展开重复规则时，候选日期会比查询区间**向前多取一天**再由绝对时间区间过滤：
否则两类真实冲突会漏报——改期例外把实例搬到查询首日（原日期在区间外、候选取不到），
以及跨午夜实例从前一日开始、延续到查询区间内。对应测试见
`TestExceptions.test_moved_exception_query_starts_on_new_date` 与
`TestConflicts.test_cross_midnight_starting_before_first_query_day`。

### 输出样例（`python3 demo.py`）

```
== 站会 vs 面试（改期后的实例撞上面试）==
冲突 2026-09-17 09:45 ~ 2026-09-17 10:30
  A: [standup] 2026-09-17 09:30 ~ 2026-09-17 10:30 (改期自 2026-09-16)
  B: [interview] 2026-09-17 09:45 ~ 2026-09-17 11:00

== 插入日历（强制冲突检测）==
插入失败，检测到冲突:
冲突 2026-09-17 09:45 ~ 2026-09-17 10:30
  A: [standup] 2026-09-17 09:30 ~ 2026-09-17 10:30 (改期自 2026-09-16)
  B: [interview] 2026-09-17 09:45 ~ 2026-09-17 11:00
```

## 规模数据（`python3 benchmark.py`，Python 3.12.3，本机实测）

| 场景 | 规模 | 耗时 |
|---|---|---|
| 每日规则展开 | 1,000,000 实例（约 2738 年） | ~1.34 s |
| 每日 2 时段（同日多实例）展开 | 1,000,000 实例 | ~1.07 s |
| 每月规则（clamp）展开 | 96,000 实例（8000 年全量） | ~0.14 s |
| 冲突检测（双每日事件扫描） | 100,000 × 100,000 实例 | ~0.33 s |

即百万级实例展开约 1 秒量级；冲突检测为展开 + 双指针扫描，随实例数线性增长。

## 运行命令

```bash
python3 -m unittest test_scheduler -v   # 自测（24 个用例）
python3 demo.py                          # 冲突实例输出样例
python3 benchmark.py                     # 规模基准
```

覆盖的边界情形（均有对应测试）：相邻不重叠、跨午夜、同一天多实例、
跨午夜实例始于查询首日前一天、长时间范围展开（100 年每日 = 36525 天计数校验）、
月末 skip/clamp、闰年/平年 2 月、例外跳过、例外改期及其引发的新冲突
（含查询区间从改期后新日期开始的情形）。
