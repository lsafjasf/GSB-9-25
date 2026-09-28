# rectpack —— 矩形打包布局库

把一批尺寸不同的面板放入固定宽度、高度不限的容器，输出每个矩形的坐标，
用于导出版面与图片拼版。仅依赖 Python 3 标准库（3.8+）。

## 运行命令

```bash
python3 -m unittest test_rectpack -v   # 约束断言测试（14 个用例，含文档表格对账）
python3 benchmark.py                   # 利用率对比 + 规模耗时（人类可读输出）
python3 benchmark.py --markdown        # 生成下表用的 Markdown（表格唯一事实来源）
python3 benchmark.py --check-docs      # 逐行断言下表与脚本输出一致
```

## 用法

```python
from rectpack import Rect, pack

res = pack(100, [Rect(60, 40, "a"), (30, 25)],  # Rect 或 (w, h) 元组
           allow_rotation=True,   # 允许旋转 90°，输出标注 rotated
           fixed_order=False)     # False 时按尺寸重排以提高利用率
for p in res.placed:      # PlacedRect(rid, x, y, w, h, rotated)
    ...
for u in res.unplaced:    # UnplacedRect(rid, w, h, reason) —— 绝不悄悄丢弃
    ...
res.height                # 所需高度
res.utilization           # 面积利用率 = 已放置面积 / (宽 × 高)
```

## 算法与复杂度

- **核心**：MaxRects（Best Short Side Fit）。维护空闲矩形集合，每个矩形
  选择"短边剩余最小"的空位放置（平局依次比较剩余面积、y、x，保证确定性），
  放置后做最大切分并增量剪除被包含的空闲矩形。
- **基线**：按面积降序的 Shelf（层架）算法，用于利用率对比。
- **复杂度**：设 n 为矩形数、F 为空闲矩形数（剪枝后通常几十到几百），
  时间 O(n·F)，最坏 O(n²)；空间 O(F)。
- **确定性**：同一输入多次运行结果完全一致（测试 `test_determinism` 验证）。

## 边界情形的确定行为

| 情形 | 行为 |
|---|---|
| 矩形宽/高 ≤ 0 | 抛出 `ValueError` 并说明是哪个矩形 |
| 容器宽度 ≤ 0 | 抛出 `ValueError` |
| 集合为空 | 合法：高度 0、利用率 0.0、空列表 |
| 旋转后仍宽于容器 | 列入 `unplaced`，reason=`exceeds container width` |
| 找不到空间 | 列入 `unplaced`，reason=`no space left` |

## 利用率对比（`python3 benchmark.py` 实测）

选取口径：下表由 `python3 benchmark.py --markdown` 直接生成，列出脚本
`COMPARE_CASES` 中的全部 7 组配置——三个数据集的旋转关闭/开启各跑一档，
另加仅开旋转的均匀中方块；不做任何筛选或省略。数据生成种子固定，结果
可复现；`--check-docs` 会逐行断言表内每行与脚本输出一致。

<!-- BENCHMARK:COMPARE BEGIN -->
| 数据集 | n | 旋转 | MaxRects 利用率 | 朴素 Shelf 利用率 | 所需高度降低 |
|---|---|---|---|---|---|
| 均匀小矩形 | 300 | 否 | 95.85% | 61.76% | 35.6% |
| 均匀小矩形 | 300 | 是 | 97.19% | 61.76% | 36.5% |
| 大小混合 | 400 | 否 | 94.56% | 64.70% | 31.6% |
| 大小混合 | 400 | 是 | 97.47% | 64.70% | 33.6% |
| 细长条 | 300 | 否 | 95.54% | 67.82% | 29.0% |
| 细长条 | 300 | 是 | 94.60% | 67.82% | 28.3% |
| 均匀中方块 | 800 | 是 | 96.42% | 64.59% | 33.0% |
<!-- BENCHMARK:COMPARE END -->

全部 7 行中，所需高度降低区间为 **28.3%–36.5%**（取上表最右列的最小、
最大值）。最低一档 28.3% 出现在**细长条数据集且开启旋转**：该数据集矩形
本就规整（宽 40–90、高 4–12），MaxRects 不旋转时已能排得很满，旋转反而
轻微打乱长边优先的布局，利用率 94.60% 也略低于关闭旋转时的 95.54%；
收益最高的 36.5% 为均匀小矩形 + 旋转。

## 规模与耗时（宽 400，混合数据，允许旋转）

同样由 `python3 benchmark.py --markdown` 生成。耗时与"平均每矩形"是本机
实测值，随机器波动、不可逐字复现（`--check-docs` 只校验其为正且两列自洽）；
n 与利用率为确定性列，逐行对账。

<!-- BENCHMARK:SCALE BEGIN -->
| n | 耗时 | 平均每矩形 | 利用率 |
|---|---|---|---|
| 500 | 0.058s | 116µs | 91.70% |
| 1000 | 0.179s | 179µs | 94.93% |
| 2000 | 0.614s | 307µs | 97.73% |
| 5000 | 1.774s | 355µs | 98.47% |
<!-- BENCHMARK:SCALE END -->

增长略超线性，符合 O(n·F) 中 F 随占用缓慢上升的预期。

## 文件

- `rectpack.py` —— 库源码（`pack` / `pack_naive` / 数据模型）
- `test_rectpack.py` —— 约束断言测试：两两不重叠、均在容器内、
  未放置明确列出、账目平衡、确定性、旋转标注、边界情形
- `benchmark.py` —— 利用率对比与规模耗时数据生成，并可
  `--update-docs`/`--check-docs` 回写、校验 README 表格
