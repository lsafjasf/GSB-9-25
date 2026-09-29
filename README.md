# rectpack — 固定宽度矩形条带打包库

Python 3，仅标准库，无第三方依赖。给定容器宽度与一组矩形，在条带中打包，
输出每个矩形的坐标，最小化所需高度。支持**可切换的排序/选择策略**、
**容器高度上限**与**多容器**场景。适用于导出版面、图片拼版。

## 运行命令

```bash
python3 -m unittest test_rectpack -v   # 约束断言测试（18 项）
python3 benchmark.py                   # 全策略 × 全数据集对比 + 多容器场景 + 规模耗时
python3 benchmark.py run --order area_desc --select baf --dataset mixed \
    --n 200 --max-height 500 --containers 3   # 按指定策略单次运行
```

## 快速上手

```python
from rectpack import SortOrder, SelectRule, pack, pack_multi

# 单容器：面积降序 + 最佳面积贴合，允许旋转
res = pack(200, [(80, 50), (120, 40), (30, 30)],
           sort=SortOrder.AREA_DESC, select=SelectRule.BAF, allow_rotate=True)
for p in res.placements:
    print(p.index, p.x, p.y, p.width, p.height, p.rotated)
print("未放置:", res.unplaced)          # 放不下的矩形明确列出，绝不悄悄丢弃
print("所需高度:", res.height, "利用率:", res.utilization)

# 多容器：单容器高度上限 500，最多 3 个容器
mres = pack_multi(200, rects, max_height=500, max_containers=3,
                  sort=SortOrder.HEIGHT_DESC, select=SelectRule.BSSF)
for i, c in enumerate(mres.containers):
    print(f"容器#{i}: 高度={c.height} 利用率={c.utilization:.2%}")
print("未放置:", mres.unplaced, "总利用率:", mres.utilization)
```

## 可切换策略

| 维度 | 取值 | 说明 |
|---|---|---|
| 放置顺序 `sort` | `SortOrder.AREA_DESC` | 面积降序 |
| | `SortOrder.HEIGHT_DESC` | 高度降序 |
| | `SortOrder.MAXSIDE_DESC` | 长边降序（默认，历史行为） |
| | `SortOrder.INPUT` | 严格按输入顺序（兼容参数 `fixed_order=True`） |
| 选择规则 `select` | `SelectRule.BSSF` | 最佳短边贴合：短边剩余最小的空位优先（默认） |
| | `SelectRule.BAF` | 最佳面积贴合：空位面积剩余最小者优先，短边剩余作平局裁决 |

## 高度上限与多容器

- `max_height`：单容器高度上限。`pack()` 下放不下的矩形进入 `unplaced`；
  `pack_multi()` 下触发开启新容器。
- `pack_multi(rects, max_containers=N)`：按确定顺序依次尝试已有容器，
  都放不下则开启新容器；达到 `max_containers` 上限或空容器也放不下
  （过宽/超高）时，矩形进入 `unplaced`。
- `MultiPackResult` 提供逐容器 `PackResult`、总高度与总利用率。

## 算法与复杂度

- **MaxRects 空闲矩形集**：维护一组极大空闲矩形；每个矩形按选择规则
  （BSSF/BAF）选空位放入，随后用已放矩形切分所有相交空闲矩形，
  并增量删除被包含的空闲矩形（被包含者在"剩余越小越优"的规则下
  永远不会胜出，删除不影响结果，只影响速度）。
- **确定性**：排序键（含下标升序裁决）与空位平局裁决（…→ y → x）
  均为确定规则，同一输入同一策略多次运行结果完全一致。
- **复杂度**：设空闲矩形数为 F。每个矩形查找 O(F)、切分+增量剪枝 O(F·k)
  （k 为相交空闲矩形数，通常很小），整体约 O(n·F)，实际接近 O(n²)。

## 边界行为（均有测试覆盖）

- **空集合**：返回 height=0、空放置列表、`utilization=0.0`。
- **零尺寸或负尺寸**：抛出 `ValueError` 并指明是哪个矩形。
- **单个矩形过宽/超高**：两个方向都放不进容器时进入 `unplaced`，其余正常放置。
- **非法容器宽度 / 高度上限 / 容器数上限**（≤0）：抛出 `ValueError`。

## 可断言约束（test_rectpack.py，18 项测试）

1. 两两不重叠；2. 均在容器内（含 `max_height` 上限）；3. 每个输入矩形
恰好在 placements 或 unplaced 出现一次（完整划分，多容器为跨容器划分）；
4. 旋转标注与尺寸一致；5. 全部 排序×选择 策略组合多次运行结果一致；
6. 利用率 ∈ [0, 1]。以上约束对**所有策略组合**均成立。

## 策略对比（容器宽 200，n=200，5 个随机种子取平均，不旋转）

以下为 `python3 benchmark.py` 的真实输出（耗时会随机器波动，其余列可复现）：

```
数据集      排序策略         选择策略            高度      利用率    耗时(ms)    未放置
uniform  area_desc    bssf         991.6   90.59%      5.94      0
uniform  area_desc    baf          995.8   90.21%      5.81      0
uniform  height_desc  bssf         944.4   95.08%      2.89      0
uniform  height_desc  baf          947.8   94.73%      2.97      0
uniform  maxside_desc bssf         964.6   93.07%      4.73      0
uniform  maxside_desc baf          963.8   93.15%      4.77      0
mixed    area_desc    bssf        1963.8   87.36%      5.41      0
mixed    area_desc    baf         1972.6   86.92%      5.45      0
mixed    height_desc  bssf        1974.0   86.85%      4.33      0
mixed    height_desc  baf         1974.0   86.85%      5.38      0
mixed    maxside_desc bssf        1961.6   87.55%      4.56      0
mixed    maxside_desc baf         1956.4   87.75%      5.55      0
skewed   area_desc    bssf        1101.2   87.03%      6.15      0
skewed   area_desc    baf         1102.8   86.91%      6.08      0
skewed   height_desc  bssf        1111.4   86.23%      6.48      0
skewed   height_desc  baf         1115.4   85.92%      5.98      0
skewed   maxside_desc bssf        1090.6   87.88%      6.51      0
skewed   maxside_desc baf         1091.0   87.85%      7.42      0
squares  area_desc    bssf        1468.6   95.63%      2.86      0
squares  area_desc    baf         1468.6   95.63%      2.86      0
squares  height_desc  bssf        1468.6   95.63%      2.86      0
squares  height_desc  baf         1468.6   95.63%      3.09      0
squares  maxside_desc bssf        1468.6   95.63%      2.79      0
squares  maxside_desc baf         1468.6   95.63%      2.80      0
```

观察：uniform 数据集上 `height_desc` 明显最优（95.1%）；mixed/skewed 上
`maxside_desc` 略优；squares 上各策略等价（方形无方向性）。BSSF 与 BAF
差距很小，BAF 在 mixed 上略占优。没有单一策略在所有数据集上最优，
因此策略可切换有实际价值。

## 多容器场景（容器宽 200，单容器高度上限 400，n=300，5 个种子取平均）

```
数据集      排序策略         选择策略         容器数     总利用率    未放置    耗时(ms)
uniform  area_desc    bssf         4.0   92.64%      0      6.03
uniform  height_desc  bssf         4.0   95.42%      0      3.29
uniform  maxside_desc bssf         4.0   93.01%      0      4.95
mixed    area_desc    bssf         8.4   85.83%      0      4.22
mixed    height_desc  bssf         8.4   84.25%      0      3.14
mixed    maxside_desc bssf         8.4   83.87%      0      4.15
skewed   area_desc    bssf         5.0   87.29%      0      5.27
skewed   height_desc  bssf         5.0   86.42%      0      4.93
skewed   maxside_desc bssf         5.0   87.59%      0      2.35
squares  area_desc    bssf         5.8   96.51%      0      2.24
squares  height_desc  bssf         5.8   96.51%      0      2.38
squares  maxside_desc bssf         5.8   96.51%      0      2.29
```

（BAF 行从略，完整输出见 `python3 benchmark.py`。）所有场景未放置数均为 0；
设置 `max_containers` 收紧上限时，放不下的矩形会明确列入 `unplaced`。

## 规模与耗时（容器宽 400，uniform 5~60，不旋转，BSSF/长边降序）

| n | 耗时 | 所需高度 | 利用率 |
|---|---|---|---|
| 1000 | 100 ms | 2310 | 94.3% |
| 2000 | 376 ms | 4681 | 94.7% |
| 4000 | 1570 ms | 9381 | 95.8% |
| 8000 | 5927 ms | 18645 | 96.6% |

上千矩形在百毫秒级完成；耗时近似随 n² 增长（空闲矩形数随 n 增长所致）。

## 可复现性

排序键与平局裁决均为确定规则，不依赖哈希序或时钟。验证方式：

```bash
python3 benchmark.py > a.txt && python3 benchmark.py > b.txt
diff a.txt b.txt   # 除耗时列外完全一致
```
