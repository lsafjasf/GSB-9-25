"""规模基准：百万次展开耗时。运行: python3 benchmark.py"""
import time
from datetime import date, timedelta

from scheduler import (
    Event, RecurrenceRule, TimeWindow, expand_event, find_conflicts,
)

D = date


def bench(name, fn, repeat=3):
    best = min(min((fn() for _ in range(repeat)), key=lambda t: t) for _ in [0])
    print(f"{name:<48} {best:8.3f} s")
    return best


def main():
    results = {}

    # 1) 每日规则展开 1,000,000 个实例（约 2738 年）
    ev_daily = Event("daily", rule=RecurrenceRule("daily", D(1, 1, 1)),
                     windows=(TimeWindow(9 * 60, 10 * 60),))
    end = D(1, 1, 1) + timedelta(days=999_999)

    def run_daily():
        t0 = time.perf_counter()
        insts = expand_event(ev_daily, D(1, 1, 1), end)
        assert len(insts) == 1_000_000, len(insts)
        return time.perf_counter() - t0

    results["daily_1m"] = bench("每日规则展开 1,000,000 实例", run_daily)

    # 2) 每日 2 时段（同一天多实例）展开 1,000,000 实例
    ev_two = Event("two", rule=RecurrenceRule("daily", D(1, 1, 1)),
                   windows=(TimeWindow(9 * 60, 10 * 60), TimeWindow(14 * 60, 15 * 60)))
    end2 = D(1, 1, 1) + timedelta(days=499_999)

    def run_two():
        t0 = time.perf_counter()
        insts = expand_event(ev_two, D(1, 1, 1), end2)
        assert len(insts) == 1_000_000, len(insts)
        return time.perf_counter() - t0

    results["two_windows_1m"] = bench("每日2时段展开 1,000,000 实例", run_two)

    # 3) 每月规则（clamp）：date 上限 9999 年，测 8000 年全量展开
    rule_m = RecurrenceRule("monthly", D(2000, 1, 31), monthdays=(31,),
                            month_end_policy="clamp")

    def run_monthly_full():
        t0 = time.perf_counter()
        insts = expand_event(Event("m", rule=rule_m, windows=(TimeWindow(60, 120),)),
                             D(2000, 1, 1), D(9999, 12, 31))
        assert len(insts) == 96000, len(insts)
        return time.perf_counter() - t0

    results["monthly_8k_years"] = bench("每月(clamp)规则展开 8000 年 = 96,000 实例", run_monthly_full)

    # 4) 大规模冲突检测：两个每日事件各 100,000 实例做扫描
    big_a = Event("a", rule=RecurrenceRule("daily", D(2000, 1, 1)),
                  windows=(TimeWindow(9 * 60, 10 * 60),))
    big_b = Event("b", rule=RecurrenceRule("daily", D(2000, 1, 1)),
                  windows=(TimeWindow(9 * 60 + 30, 10 * 60 + 30),))
    end3 = D(2000, 1, 1) + timedelta(days=99_999)

    def run_conflict():
        t0 = time.perf_counter()
        cs = find_conflicts(big_a, big_b, D(2000, 1, 1), end3)
        assert len(cs) == 100_000, len(cs)
        return time.perf_counter() - t0

    results["conflict_100k"] = bench("冲突检测 100,000 x 100,000 实例", run_conflict)

    print(f"\n合计: {sum(results.values()):.3f} s  (Python {__import__('sys').version.split()[0]})")


if __name__ == "__main__":
    main()
