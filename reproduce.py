"""复现 buggy_allocate 的五类现网问题。运行：python3 reproduce.py"""
from buggy_allocate import buggy_allocate

failures = 0

def check(name, fn):
    global failures
    try:
        fn()
        print(f"[未复现] {name}")
    except AssertionError as e:
        failures += 1
        print(f"[复现成功] {name}: {e}")


# 问题 1：各份之和与总额差几分（float 精度误差使差额超出修补能力）
def bug1():
    total, weights = 10**18, [1, 1, 1]
    shares = buggy_allocate(total, weights)
    assert sum(shares) == total, (
        f"总额 {total}，实分 {sum(shares)}（{shares[0]} ...），"
        f"差 {total - sum(shares)} 个最小单位：float 除法精度误差 + int() 截断，"
        f"差额超过份数后修补循环直接放弃")
check("问题1 总和与总额不符", bug1)


# 问题 2：权重全为零时除零
def bug2():
    try:
        buggy_allocate(100, [0, 0, 0])
        raise RuntimeError("unexpected")
    except ZeroDivisionError as e:
        raise AssertionError(f"权重全为零触发 ZeroDivisionError: {e}")
check("问题2 权重全零除零", bug2)


# 问题 3：负数总额时误差分配方向出错
def bug3():
    total, weights = -100, [1, 1, 1]
    shares = buggy_allocate(total, weights)
    assert sum(shares) == total, (
        f"负数总额 {total}，实分 {sum(shares)}（{shares}）："
        f"int() 向零截断后差额为负，range(负数) 为空，{total - sum(shares)} 个单位被静默丢弃")
check("问题3 负数总额方向错误", bug3)


# 问题 4：分配结果随输入顺序变化
def bug4():
    total = 11
    a = buggy_allocate(total, [3, 1, 1])
    b = buggy_allocate(total, [1, 1, 3])  # 同样的权重多重集合，仅顺序不同
    pa = sorted(zip([3, 1, 1], a))
    pb = sorted(zip([1, 1, 3], b))
    assert pa == pb, (
        f"权重 3 的份在队首分得 {a[0]}，在队尾分得 {b[2]}："
        f"余数永远补给排在前面的份，结果随输入顺序变化（{a} vs {b}）")
check("问题4 结果随输入顺序变化", bug4)


# 问题 5：无法追溯余数落在哪一份
def bug5():
    shares = buggy_allocate(100, [1, 1, 1])
    assert all(isinstance(s, int) for s in shares)
    raise AssertionError(
        f"返回值为裸 int 列表 {shares}，没有任何字段标注哪一份承担了余数，无法审计")
check("问题5 余数去向不可追溯", bug5)


print(f"\n共复现 {failures}/5 类问题")
raise SystemExit(0 if failures == 5 else 1)
