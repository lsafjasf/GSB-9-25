"""多级分摊：总额先按部门、再按人逐级递归拆分，每级之和严格等于上拨金额。

在单级分摊（src/allocation.py 的最大余数法）之上组合出树形多级分摊：
每个内部节点拿到上拨金额后，用同一套确定规则把金额按子节点权重拆分，
因此不变量逐级继承：

- 严格相等：任一内部节点的子级金额之和 == 该节点的上拨金额（逐节点成立，
  故等深树的每一层之和 == 总额）；
  每一级哪几份承担了余数（每份恒承担 1 个最小单位，方向随总额符号）；
- 顺序无关：余数优先级的最终决胜键是子树指纹 fingerprint()（由权重、
  标签与子结构递归构成），与输入顺序无关——任意打乱输入顺序，每个节点
  分到的金额完全不变。仅当两个兄弟节点的权重、标签、子树完全相同时才
  不可区分，而二者结果本就相同，交换无可观测差异。

异常约定与单级一致：负权重 / 非整数权重 TypeError 或 ValueError；
某内部节点子权重全为零但上拨金额非零时抛 ValueError（分摊未定义）。
"""

from dataclasses import dataclass

from allocation import allocate

__all__ = [
    "Node",
    "AllocatedNode",
    "allocate_tree",
    "fingerprint",
    "levels",
    "remainder_carriers",
    "assert_tree_invariants",
]


@dataclass(frozen=True)
class Node:
    """分摊树输入节点。weight 为该节点在父级分摊中的权重；children 为空即叶子。"""

    weight: int
    children: tuple = ()
    label: str = ""


@dataclass(frozen=True)
class AllocatedNode:
    """分摊结果节点（审计记录），与输入树同构。"""

    label: str
    weight: int            # 在父级分摊中的权重
    amount: int            # 本分分摊到的金额（最小单位，带符号）
    carried_remainder: bool  # 在父级分摊中是否承担了 1 个最小单位的余数
    children: tuple        # tuple[AllocatedNode, ...]


def allocate_tree(total, root):
    """把 total（整数最小单位）沿树逐级分摊，返回 AllocatedNode 结果树。

    root.weight 不参与计算（根无上级），root.amount 恒等于 total。
    """
    if not isinstance(root, Node):
        raise TypeError(f"root 必须为 Node，得到 {root!r}")
    _validate(root, path="root")
    return _alloc(total, root, carried=False)


def fingerprint(node):
    """节点的顺序无关指纹：(权重, 标签, 子指纹多重集)，可哈希、可比较。

    作为余数优先级的最终决胜键：两个节点指纹相同当且仅当它们的权重、
    标签与整个子树结构（不计子级顺序）完全相同，此时二者结果必然相同。
    """
    return (
        node.weight,
        node.label,
        tuple(sorted(fingerprint(c) for c in node.children)),
    )


def levels(result):
    """按深度分层返回节点列表：levels[0] 为 [根]，levels[1] 为部门级，依此类推。"""
    out = []
    frontier = [result]
    while frontier:
        out.append(list(frontier))
        frontier = [c for node in frontier for c in node.children]
    return out


def remainder_carriers(result):
    """逐级汇总余数承担者，返回 {深度: [(路径, 承担金额), ...]}。

    路径形如 "集团/华东部/张三"；承担金额恒为 ±1 个最小单位（符号随总额）。
    根节点无上级分摊，不可能是余数承担者。
    """
    carriers = {}
    unit = 1 if result.amount >= 0 else -1  # 余数方向随总额符号

    def walk(node, depth, path):
        if node.carried_remainder:
            carriers.setdefault(depth, []).append((path, unit))
        for child in node.children:
            walk(child, depth + 1, f"{path}/{child.label or child.weight}")

    walk(result, 0, result.label or "root")
    return carriers


def assert_tree_invariants(result, total):
    """校验多级分摊不变量，任一不满足即抛 AssertionError。返回 None。

    - 根金额 == total；
    - 每个内部节点：子级金额之和 == 该节点上拨金额（每级严格相等）；
    - 权重为零的节点分得零；
    - 符号一致：total >= 0 时各节点 amount >= 0，total <= 0 时 <= 0；
    - 余数承担者数量 == |上拨金额| - sum(子级基础份额)，且承担者权重为正。
    """
    assert result.amount == total, f"根金额 {result.amount} != 总额 {total}"

    def walk(node, path):
        if node.weight == 0 and node is not result:
            assert node.amount == 0, f"{path}: 零权重分得 {node.amount}"
        if total > 0:
            assert node.amount >= 0, f"{path}: 正总额分出负数 {node.amount}"
        elif total < 0:
            assert node.amount <= 0, f"{path}: 负总额分出正数 {node.amount}"
        if node.children:
            child_sum = sum(c.amount for c in node.children)
            assert child_sum == node.amount, (
                f"{path}: 子级之和 {child_sum} != 上拨金额 {node.amount}"
            )
            carriers = [c for c in node.children if c.carried_remainder]
            assert all(c.weight > 0 for c in carriers), f"{path}: 零权重承担余数"
            assert len(carriers) < len(node.children) or len(node.children) == 1, (
                f"{path}: 余数承担者 {len(carriers)} 不少于份数"
            )
        for child in node.children:
            walk(child, f"{path}/{child.label or child.weight}")

    walk(result, result.label or "root")


def _validate(node, path):
    if not isinstance(node.weight, int) or isinstance(node.weight, bool):
        raise TypeError(f"{path}: 权重必须为整数，得到 {node.weight!r}")
    if node.weight < 0:
        raise ValueError(f"{path}: 权重不允许为负，得到 {node.weight}")
    if not isinstance(node.children, (tuple, list)):
        raise TypeError(f"{path}: children 必须为 tuple/list")
    for i, child in enumerate(node.children):
        if not isinstance(child, Node):
            raise TypeError(f"{path}.children[{i}]: 必须为 Node")
        _validate(child, f"{path}/{child.label or i}")


def _alloc(amount, node, carried):
    if not node.children:
        return AllocatedNode(node.label, node.weight, amount, carried, ())
    shares = allocate(
        amount,
        [c.weight for c in node.children],
        tie_keys=[fingerprint(c) for c in node.children],
    )
    children = tuple(
        _alloc(s.amount, child, s.carried_remainder)
        for s, child in zip(shares, node.children)
    )
    return AllocatedNode(node.label, node.weight, amount, carried, children)
