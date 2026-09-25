"""复现旧版分摊函数的五类现网问题。直接运行：python3 reproduce_issues.py"""

import random
import sys

sys.path.insert(0, "src")
from allocation_buggy import allocate


def issue_1_sum_mismatch():
    # 浮点 round 过度进位，diff < 0 被丢弃，总和偏离总额
    total, weights = 100, [3, 3, 3]  # round(33.33)=33 x3 -> 99, diff=1 补到第 0 份 -> 100? 换个更典型的
    cases = [(100, [3, 3, 3]), (5, [1, 1, 1]), (101, [2, 2, 2, 2, 2])]
    bad = []
    for total, weights in cases:
        got = allocate(total, weights)
        if sum(got) != total:
            bad.append((total, weights, got))
    return bad


def issue_2_zero_division():
    try:
        allocate(100, [0, 0, 0])
        return None
    except ZeroDivisionError as e:
        return e


def issue_3_negative_total_direction():
    # 负数总额：round 后 diff<0 被丢弃，误差未按负方向分配
    total, weights = -100, [1, 1, 1]
    got = allocate(total, weights)
    return (total, weights, got, sum(got))


def issue_4_order_dependent():
    weights = [1, 1, 1, 2]
    base = allocate(7, weights)
    rng = random.Random(42)
    variants = set()
    for _ in range(20):
        perm = list(range(len(weights)))
        rng.shuffle(perm)
        shuffled = [weights[i] for i in perm]
        got = allocate(7, shuffled)
        restored = tuple(sorted(zip(shuffled, got)))  # (权重, 金额) 多重集合
        variants.add(restored)
    return base, variants


def issue_5_not_auditable():
    got = allocate(10, [1, 1, 1])
    return got  # 裸整数列表，无法知道余额落在哪份


print("=== 问题 1：总和与总额差几分 ===")
for total, weights, got in issue_1_sum_mismatch():
    print(f"  total={total} weights={weights} -> {got}, sum={sum(got)} (差 {total - sum(got)})")

print("=== 问题 2：权重全为零时除零 ===")
print(f"  {type(issue_2_zero_division()).__name__}: {issue_2_zero_division()}")

print("=== 问题 3：负数总额误差方向出错 ===")
total, weights, got, s = issue_3_negative_total_direction()
print(f"  total={total} weights={weights} -> {got}, sum={s} (应为 {total}，误差 {total - s} 未按负方向分配)")

print("=== 问题 4：结果随输入顺序变化 ===")
base, variants = issue_4_order_dependent()
print(f"  原始结果 {base}；打乱顺序后 (权重,金额) 多重集合出现 {len(variants)} 种: {sorted(variants)}")

print("=== 问题 5：无法追溯余额落点 ===")
print(f"  返回值仅是 {issue_5_not_auditable()}，无权重与余数承担标记")
