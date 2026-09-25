# assertlib 差异消息样例集

由 `python3 make_samples.py` 自动生成；每个样例均为真实失败输出。

## 1. 嵌套字典：精确定位到键路径

差异在深层嵌套字段，路径 root.user.profile.name 直接给出。

```
assert_equal failed
x root.user.profile.name
    expected: 'anna'
    actual:   'ann'
```

## 2. 列表中部插入一项

识别为「自第 3 项起插入了一项」，而不是笼统的长度不同。

```
assert_equal failed
x root
    list: inserted 1 item(s) at index 3 (subsequent items shifted right)
    inserted: ['INSERTED']
```

## 3. 列表头部插入 + 中部删除

插入与删除各自独立报告，均带下标。

```
assert_equal failed
x root
    list: inserted 1 item(s) at index 0 (subsequent items shifted right)
    inserted: ['NEW']
x root
    list: deleted 2 item(s) at index 2 (subsequent items shifted left)
    deleted: ['X', 'Y']
```

## 4. 列表元素替换：递归到元素内部

replace 区块按位置配对后递归，路径精确到元素字段。

```
assert_equal failed
x root[1].price
    expected: 200
    actual:   250
```

## 5. 长字符串：截断但保留绝对偏移

1206 字符的文本只显示差异处上下文窗口，偏移 601 与区间完整保留。

```
assert_equal failed
x root
    string differs at offset 601 (expected len 1206, actual len 1206)
    expected[581:621]: ...'-lorem-lorem-lorem-MIDDLEipsum-ipsum-ips'...
    actual  [581:621]: ...'-lorem-lorem-lorem-M1DDLEipsum-ipsum-ips'...
```

## 6. 长列表：截断但保留下标路径

1000 项列表只报告差异下标 root[777]，不打印整个列表。

```
assert_equal failed
x root[777]
    expected: 777
    actual:   -1
```

## 7. 类型不匹配

明确指出 expected int, got str。

```
assert_equal failed
x root.count
    type mismatch: expected int, got str
    expected: 3
    actual:   '3'
```

## 8. 近似相等：差值 / 容差 / 相对误差

失败时给出 abs diff、tolerance 与 rel error。

```
assert_almost_equal failed
x root
    expected:  3.14159
    actual:    3.1416
    abs diff:  1.0000000000065512e-05
    tolerance: 1e-06 (abs)
    rel error: 3.1831015505096186e-06
```

## 9. 近似相等：类型不匹配

非数值类型直接指出类型差异。

```
assert_almost_equal failed
x root
    type mismatch: both sides must be numbers (expected: float, actual: str)
    expected: 3.14
    actual:   '3.14'
```

## 10. 抛错断言：抛了错误的异常类型

期望 ValueError，实际 KeyError。

```
assert_raises failed
x root
    expected ValueError, but KeyError was raised
    actual: KeyError: 'missing_key'
```

## 11. 超时断言

函数未在 0.050s 内完成。

```
assert_completes_within failed
x root
    did not complete within 0.050s (limit exceeded, function still running)
```

## 12. 包含断言：长容器截断但保留长度

1000 字符的字符串容器截断显示，len 1000 保留。

```
assert_contains failed
x root
    item not found: 'needle'
    container (str, len 1000): 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa... <truncated, 1002 chars total>
```

## 13. 集合差异

missing 与 unexpected 分组报告，排序后输出稳定。

```
assert_equal failed
x root
    missing:   ['beta']
    unexpected: ['delta']
```
