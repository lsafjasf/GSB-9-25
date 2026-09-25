"""生成差异消息样例集 SAMPLES.md（同时验证样例可复现）。"""
import time

from assertlib import (
    AssertionFailure,
    assert_almost_equal,
    assert_completes_within,
    assert_contains,
    assert_equal,
    assert_raises,
)

SCENARIOS = []


def scenario(title, note=""):
    def deco(fn):
        SCENARIOS.append((title, note, fn))
        return fn
    return deco


@scenario("嵌套字典：精确定位到键路径",
          "差异在深层嵌套字段，路径 root.user.profile.name 直接给出。")
def s1():
    actual = {"user": {"id": 7, "profile": {"name": "ann", "age": 30}}}
    expected = {"user": {"id": 7, "profile": {"name": "anna", "age": 30}}}
    assert_equal(actual, expected)


@scenario("列表中部插入一项",
          "识别为「自第 3 项起插入了一项」，而不是笼统的长度不同。")
def s2():
    actual = ["alpha", "beta", "gamma", "INSERTED", "delta", "epsilon"]
    expected = ["alpha", "beta", "gamma", "delta", "epsilon"]
    assert_equal(actual, expected)


@scenario("列表头部插入 + 中部删除",
          "插入与删除各自独立报告，均带下标。")
def s3():
    actual = ["NEW", "a", "b", "c", "d"]
    expected = ["a", "b", "X", "Y", "c", "d"]
    assert_equal(actual, expected)


@scenario("列表元素替换：递归到元素内部",
          "replace 区块按位置配对后递归，路径精确到元素字段。")
def s4():
    actual = [{"sku": "A1", "price": 100}, {"sku": "B2", "price": 250}]
    expected = [{"sku": "A1", "price": 100}, {"sku": "B2", "price": 200}]
    assert_equal(actual, expected)


@scenario("长字符串：截断但保留绝对偏移",
          "1206 字符的文本只显示差异处上下文窗口，偏移 601 与区间完整保留。")
def s5():
    expected = "lorem-" * 100 + "MIDDLE" + "ipsum-" * 100
    actual = expected.replace("MIDDLE", "M1DDLE")
    assert_equal(actual, expected)


@scenario("长列表：截断但保留下标路径",
          "1000 项列表只报告差异下标 root[777]，不打印整个列表。")
def s6():
    expected = list(range(1000))
    actual = list(range(1000))
    actual[777] = -1
    assert_equal(actual, expected)


@scenario("类型不匹配",
          "明确指出 expected int, got str。")
def s7():
    assert_equal({"count": "3"}, {"count": 3})


@scenario("近似相等：差值 / 容差 / 相对误差",
          "失败时给出 abs diff、tolerance 与 rel error。")
def s8():
    assert_almost_equal(3.14160, 3.14159, tol=1e-6)


@scenario("近似相等：类型不匹配",
          "非数值类型直接指出类型差异。")
def s9():
    assert_almost_equal("3.14", 3.14)


@scenario("抛错断言：抛了错误的异常类型",
          "期望 ValueError，实际 KeyError。")
def s10():
    def boom():
        raise KeyError("missing_key")
    assert_raises(ValueError, boom)


@scenario("超时断言",
          "函数未在 0.050s 内完成。")
def s11():
    assert_completes_within(0.05, time.sleep, 5)


@scenario("包含断言：长容器截断但保留长度",
          "1000 字符的字符串容器截断显示，len 1000 保留。")
def s12():
    assert_contains("a" * 1000, "needle")


@scenario("集合差异",
          "missing 与 unexpected 分组报告，排序后输出稳定。")
def s13():
    assert_equal({"gamma", "alpha", "delta"}, {"alpha", "beta", "gamma"})


def main():
    parts = ["# assertlib 差异消息样例集",
             "",
             "由 `python3 make_samples.py` 自动生成；每个样例均为真实失败输出。",
             ""]
    for i, (title, note, fn) in enumerate(SCENARIOS, 1):
        try:
            fn()
        except AssertionFailure as exc:
            message = str(exc)
        else:
            raise SystemExit(f"scenario {i} did not fail: {title}")
        parts.append(f"## {i}. {title}")
        parts.append("")
        if note:
            parts.append(note)
            parts.append("")
        parts.append("```")
        parts.append(message)
        parts.append("```")
        parts.append("")
    with open("SAMPLES.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print(f"SAMPLES.md written ({len(SCENARIOS)} scenarios)")


if __name__ == "__main__":
    main()
