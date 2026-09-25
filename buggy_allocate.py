"""现网有缺陷的分摊实现（仅用于复现问题，请勿在生产使用）。

已知五类缺陷：
1. int() 截断 + float 精度误差导致各份之和与总额不符
   （修补循环只在差额小于份数时生效，大差额被静默丢弃）；
2. 权重全为零时 ZeroDivisionError；
3. 负数总额时误差分配方向出错（range(负数) 为空，差额被静默丢弃）；
4. 余数总是分给排在前面的份，结果随输入顺序变化；
5. 返回裸 int 列表，无法追溯余数落在了哪一份上。
"""
from typing import List, Sequence


def buggy_allocate(total: int, weights: Sequence[int]) -> List[int]:
    total_weight = sum(weights)
    # 缺陷 1/2：float 除法 + int() 向零截断；total_weight 为 0 时除零
    shares = [int(total * w / total_weight) for w in weights]
    diff = total - sum(shares)
    # 缺陷 1/3/4：只修补 0 < diff < 份数 的情形，且永远补给最前面的份；
    # diff 为负（负数总额）或 diff 过大（float 精度误差累积）时静默丢弃
    if 0 < diff < len(shares):
        for i in range(diff):
            shares[i] += 1
    # 缺陷 5：只返回金额，余数去向不可追溯
    return shares
