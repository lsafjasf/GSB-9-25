"""金额分摊（修复版）：把总额按权重分摊到若干份，各份之和严格等于总额。

金额一律用整数最小单位（分）表示，全程整数运算，无浮点误差。

分配规则（最大余数法 / Hamilton 法，确定且可解释）：
1. 设总额 T（非零）、权重 w_i >= 0、权重和 W = sum(w_i) > 0。
2. 先按符号分离：对 |T| 做分摊，最后统一乘上符号，保证正负总额行为镜像一致。
3. 每份基础份额 base_i = (|T| * w_i) // W（向下取整，整数运算）。
4. 剩余余数 r = |T| - sum(base_i)，满足 0 <= r < 份数。
5. 余数按“最大余数法”逐份分配 1 个最小单位，优先级：
   - 小数余量 frac_i = (|T| * w_i) % W 大者优先；
   - frac 并列时权重大者优先；
   - 仍并列时原始下标小者优先（稳定决胜）。
   因此 (权重, 金额) 的多重集合与输入顺序无关。
6. 权重为 0 的份 base 与 frac 均为 0，必然分得 0。
7. 总额为正时余数 +1，总额为负时余数 -1，方向随符号翻转。

可审计性：返回每份的 Share(index, weight, amount, carried_remainder)，
carried_remainder=True 的份即余数（余额）落点，数量恒等于 |T| - sum(base_i)。

异常约定：
- 任一权重为负：ValueError，消息中列出全部负权重下标；
- 权重全为零（W == 0）且总额非零：ValueError（分摊未定义）；
- 总额为零：合法，所有份分得 0。
"""

from dataclasses import dataclass

__all__ = ["Share", "allocate"]


@dataclass(frozen=True)
class Share:
    """单份分摊结果（审计记录）。"""

    index: int             # 在输入 weights 中的下标
    weight: int            # 该份权重
    amount: int            # 分摊金额（最小单位，带符号）
    carried_remainder: bool  # 是否承担了 1 个最小单位的余数


def allocate(total, weights):
    """把 total（整数最小单位）按 weights 分摊，返回 list[Share]。

    不变量：
    - sum(share.amount) == total；
    - weight == 0 的份 amount == 0；
    - (weight, amount) 多重集合与输入顺序无关；
    - total >= 0 时所有 amount >= 0，total <= 0 时所有 amount <= 0。
    """
    weights = list(weights)
    for w in weights:
        if not isinstance(w, int) or isinstance(w, bool):
            raise TypeError(f"权重必须为整数，得到 {w!r}")
    if not isinstance(total, int) or isinstance(total, bool):
        raise TypeError(f"总额必须为整数，得到 {total!r}")

    negative = [i for i, w in enumerate(weights) if w < 0]
    if negative:
        raise ValueError(
            f"权重不允许为负，负权重下标: {negative}，"
            f"对应权重: {[weights[i] for i in negative]}"
        )

    n = len(weights)
    if n == 0:
        if total != 0:
            raise ValueError("份数为空但总额非零，无法分摊")
        return []

    if total == 0:
        return [Share(i, w, 0, False) for i, w in enumerate(weights)]

    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError(
            f"权重全为零，无法按比例分摊非零总额 {total}；"
            "请提供正权重，或在业务层明确均摊策略"
        )

    sign = 1 if total > 0 else -1
    magnitude = abs(total)

    bases = [0] * n
    fracs = [0] * n
    for i, w in enumerate(weights):
        if w:
            product = magnitude * w
            bases[i] = product // weight_sum
            fracs[i] = product % weight_sum

    remainder = magnitude - sum(bases)  # 0 <= remainder < n

    # 最大余数法：frac 降序 -> 权重降序 -> 下标升序，确定且可解释
    carrier_idx = sorted(range(n), key=lambda i: (-fracs[i], -weights[i], i))
    carried = [False] * n
    for i in carrier_idx[:remainder]:
        carried[i] = True
        bases[i] += 1

    return [
        Share(i, weights[i], sign * bases[i], carried[i])
        for i in range(n)
    ]
