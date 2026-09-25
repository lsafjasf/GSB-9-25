"""旧版（有缺陷）的金额分摊实现，仅用于复现现网问题，请勿在生产使用。

已知缺陷（对应现网五类问题）：
1. 使用浮点乘除 + round，各份之和与总额可能差几分；
2. 权重全为零时除零（ZeroDivisionError）；
3. 负数总额时余数仍按 +1 方向分配，误差方向出错；
4. 余数固定分给“排在前面的份”，分配结果随输入顺序变化；
5. 只返回裸整数列表，无法追溯余额落在哪一份上。
"""


def allocate(total_cents, weights):
    """按权重分摊总额（旧版，有缺陷）。返回每份金额的整数列表。"""
    weight_sum = sum(weights)
    shares = []
    for w in weights:
        # 缺陷 1：浮点运算 + 四舍五入，累积误差导致总和偏离总额
        # 缺陷 2：weight_sum 为 0 时抛 ZeroDivisionError
        shares.append(round(total_cents * w / weight_sum))
    # 缺陷 3：只处理 diff > 0 的补差；负数总额经 round 后常出现 diff < 0，
    # 此时差额被直接丢弃，且补偿方向（+1）与总额符号无关，方向错误
    diff = total_cents - sum(shares)
    i = 0
    while diff > 0 and shares:
        # 缺陷 4：差额固定补给下标靠前的份，结果随输入顺序变化
        shares[i % len(shares)] += 1
        diff -= 1
        i += 1
    # 缺陷 5：只返回金额，不携带权重与余数承担信息，无法审计
    return shares
