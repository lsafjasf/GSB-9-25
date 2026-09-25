#!/usr/bin/env python3
"""Reproduce the four production defect classes, old vs fixed implementation.

Usage:  python3 reproduce.py

Exit code is 0 (the script is a demonstration); the unittest suites in
tests/ enforce pass/fail.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

import conformance as c  # noqa: E402


SCENARIO_LABELS = [
    ("delete_current", "遍历中删除当前记录后漏掉下一条"),
    ("delete_unvisited", "遍历中删除尚未访问的记录"),
    ("delete_then_reinsert_same_key", "删除后再插入同一键导致重复访问"),
    ("compact_during_iteration", "清理后旧迭代器读到已释放数据"),
    ("concurrent_interleaved_insert_delete", "遍历期间插入/删除交错(并发语义)"),
]


def render(violations):
    if not violations:
        return "符合快照语义(每条活跃记录恰好访问一次)"
    return "; ".join(violations)


def main():
    print("选定的迭代可见性语义: 快照式 (snapshot at iterator creation)\n")
    for impl_name, factory in (
        ("旧实现 BuggyMemoryIndex (缺陷基线)", c.FACTORIES["buggy"]),
        ("修复实现 MemoryIndex (快照语义)", c.FACTORIES["fixed"]),
    ):
        print("=" * 78)
        print(impl_name)
        print("=" * 78)
        for key, label in SCENARIO_LABELS:
            violations = c.SCENARIOS[key](factory)
            verdict = "复现缺陷" if violations else "通过"
            print("[%s] %s" % (verdict, label))
            print("        %s" % render(violations))
        stats_violations = c.scenario_stats_consistency(factory)
        verdict = "复现缺陷" if stats_violations else "通过"
        print("[%s] 统计接口(条目数/已删除数/容量)与实际一致" % verdict)
        print("        %s" % render(stats_violations))
        print()


if __name__ == "__main__":
    main()
