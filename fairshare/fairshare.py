"""加权公平分配库（仅标准库）。

模型：
    n 个租户共享容量 capacity。租户 i 有：
      - weight  w_i >= 0   权重（0 表示不参与分配）
      - demand  d_i >= 0   申请量（分配上限）
      - minimum m_i >= 0   最小保障（分配下限）

分配规则：
    1. 先给每个租户其最小保障 m_i。
    2. 剩余容量按权重做注水法（water-filling）分配，
       每个租户在 m_i 之上最多再分到 d_i - m_i。
    3. 零权重租户分配为零（因此要求其 m_i == 0，否则不可行）。

不变量（可行时严格成立）：
    - 守恒：sum(alloc) == min(capacity, 有权租户申请之和 + 零权重租户保障之和)
    - 上限：alloc_i <= d_i
    - 下限：alloc_i >= m_i
    - 零权重：w_i == 0  =>  alloc_i == 0
    - 确定性：同输入多次运行结果完全一致

复杂度：O(n log n)（排序主导），空间 O(n)。
"""

from fractions import Fraction


class InfeasibleError(Exception):
    """最小保障不可行时抛出，携带缺口与冲突方信息。"""

    def __init__(self, capacity, total_minimum, conflicts):
        self.capacity = capacity
        self.total_minimum = total_minimum
        self.deficit = total_minimum - capacity
        self.conflicts = conflicts  # [(index, minimum), ...] 保障>0 的租户
        names = ", ".join(f"#{i}(min={m})" for i, m in conflicts)
        super().__init__(
            f"infeasible: sum of minimums {total_minimum} exceeds capacity "
            f"{capacity} (deficit={self.deficit}); conflicting parties: {names}"
        )


def allocate(capacity, weights, demands, minimums=None):
    """加权公平分配，返回整数分配列表。

    参数均为非负整数序列，长度一致。容量与额度以同一单位计。
    最小保障总和超过容量时抛出 InfeasibleError，绝不静默违约。
    """
    n = len(weights)
    if not (len(demands) == n and (minimums is None or len(minimums) == n)):
        raise ValueError("weights/demands/minimums length mismatch")
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    minimums = list(minimums) if minimums is not None else [0] * n

    for i in range(n):
        if weights[i] < 0 or demands[i] < 0 or minimums[i] < 0:
            raise ValueError(f"tenant #{i}: negative weight/demand/minimum")
        if minimums[i] > demands[i]:
            raise InfeasibleError(
                demands[i], minimums[i], [(i, minimums[i])]
            )
        if weights[i] == 0 and minimums[i] > 0:
            raise InfeasibleError(
                capacity, minimums[i], [(i, minimums[i])]
            )

    total_min = sum(minimums)
    if total_min > capacity:
        conflicts = [(i, minimums[i]) for i in range(n) if minimums[i] > 0]
        raise InfeasibleError(capacity, total_min, conflicts)

    # 1) 先发放最小保障
    alloc = list(minimums)
    remaining = capacity - total_min

    # 2) 剩余容量注水分配：活跃租户 (索引, 权重, 剩余可分配上限)
    active = [
        (i, weights[i], demands[i] - minimums[i])
        for i in range(n)
        if weights[i] > 0 and demands[i] > minimums[i]
    ]
    if remaining > 0 and active:
        extra = _waterfill(remaining, active)
        for i, amount in extra:
            alloc[i] += amount
    return alloc


def _waterfill(remaining, active):
    """把 remaining 按权重注水分给 active，每项上限为剩余额度。

    用 Fraction 求精确水位线，再向下取整并按确定规则分发余数，
    保证 sum == remaining 且结果确定。返回 [(index, amount), ...]。
    """
    # 按 上限/权重 升序：比值小者先触顶
    ordered = sorted(active, key=lambda t: Fraction(t[2], t[1]))
    total_weight = sum(w for _, w, _ in active)
    pool = remaining
    level = None  # 最终水位（每单位权重分得的量），Fraction
    saturated = []  # (index, cap)
    unsaturated = []  # (index, weight)
    for i, w, cap in ordered:
        # 若当前水位下该租户应得 w * pool/total_weight >= cap，则其触顶
        if Fraction(w) * pool >= Fraction(cap) * total_weight:
            saturated.append((i, cap))
            pool -= cap
            total_weight -= w
        else:
            unsaturated.append((i, w))
    if total_weight > 0:
        level = Fraction(pool, total_weight)

    # 未触顶者按水位取整，余数按确定规则（小数部分降序、索引升序）补齐
    result = list(saturated)
    if level is not None:
        floors = []
        for i, w in unsaturated:
            exact = w * level
            floor = exact.numerator // exact.denominator
            floors.append((i, floor, exact - floor))
        used = sum(c for _, c in saturated) + sum(f for _, f, _ in floors)
        leftover = remaining - used
        floors.sort(key=lambda t: (-t[2], t[0]))
        bumped = []
        for k, (i, floor, frac) in enumerate(floors):
            amount = floor + (1 if k < leftover else 0)
            bumped.append((i, amount))
        result.extend(bumped)
    return result
