"""金额分摊（整数最小单位）—— 修复版。

分配规则：带符号处理的最大余数法（largest remainder method）。

1. 校验：权重必须为非负整数，否则抛出 ValueError 并指明下标；
   权重和为零且总额非零时抛出 ValueError（无比例依据，拒绝分摊）。
2. 符号剥离：先对 abs(total) 分摊，最后统一乘回符号，
   保证 allocate(-t, w) == [-s for s in allocate(t, w)]，
   且每份符号与总额一致（总额为零时全部分得零）。
3. 取整：每份基准值 base_i = (|total| * w_i) // weight_sum（向下取整），
   余数 r_i = (|total| * w_i) mod weight_sum。
4. 余数分配：剩余 leftover = |total| - sum(base_i) 个最小单位，
   按 (r_i 降序, w_i 降序, 下标升序) 排序，前 leftover 份各 +1。
   决胜键只依赖该份自身的 (余数, 权重)，与输入顺序无关；
   下标仅用于在完全并列时给出确定、可复现的输出。

不变量：
- 各份之和严格等于总额；
- 权重为零者分得零，且永不承担余数；
- 结果与输入顺序无关（(weight, amount) 对的多重集合不变）；
- 每份符号与总额一致。
"""
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class Share:
    """单份分摊结果（可审计）。"""
    index: int            # 在输入 weights 中的原始下标
    weight: int           # 该份权重
    amount: int           # 分摊金额（最小单位，符号与总额一致）
    takes_residual: bool  # 是否承担了 1 个最小单位的余数


def allocate(total: int, weights: Sequence[int]) -> List[Share]:
    if not isinstance(total, int) or isinstance(total, bool):
        raise TypeError(f"total 必须为整数最小单位，得到 {type(total).__name__}")

    n = len(weights)
    negative = [i for i, w in enumerate(weights) if w < 0]
    if negative:
        raise ValueError(
            f"权重不允许为负，下标 {negative} 的权重为负："
            f"{[(i, weights[i]) for i in negative]}"
        )

    weight_sum = sum(weights)
    if weight_sum == 0:
        if total == 0:
            return [Share(index=i, weight=w, amount=0, takes_residual=False)
                    for i, w in enumerate(weights)]
        raise ValueError(
            f"权重和为零，无法按比例分摊非零总额 {total}；"
            f"请提供至少一个正权重，或将总额置零"
        )

    sign = 1 if total >= 0 else -1
    abs_total = abs(total)

    bases = [0] * n
    remainders = [0] * n
    allocated = 0
    for i, w in enumerate(weights):
        if w == 0:
            continue
        numerator = abs_total * w
        base = numerator // weight_sum
        bases[i] = base
        remainders[i] = numerator - base * weight_sum
        allocated += base

    leftover = abs_total - allocated  # 0 <= leftover <= 正权重份数
    # 决胜键 (余数降序, 权重降序, 下标升序)：只依赖自身属性，与输入顺序无关
    order = sorted(range(n), key=lambda i: (-remainders[i], -weights[i], i))
    takes_residual = [False] * n
    for i in order[:leftover]:
        bases[i] += 1
        takes_residual[i] = True

    return [
        Share(index=i, weight=weights[i], amount=sign * bases[i],
              takes_residual=takes_residual[i])
        for i in range(n)
    ]


def allocate_amounts(total: int, weights: Sequence[int]) -> List[int]:
    """便捷接口：只返回每份金额列表。"""
    return [s.amount for s in allocate(total, weights)]
