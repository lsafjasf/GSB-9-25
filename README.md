# 矩形 / 线段相交分类、判定与裁剪工具

Python 3，仅标准库。布局命中检测场景，输入含零长度线段、退化矩形、共线与相切。
判定结果不止布尔值：`classify_*` 返回可解释的五类枚举 + 参数区间 + 分类依据。

## 文件

| 文件 | 内容 |
| --- | --- |
| `rectclip.py` | 库：点-矩形关系、线段-线段、线段-矩形、Liang-Barsky 裁剪、相交分类 |
| `test_degenerate.py` | 退化用例集（26 个 unittest，覆盖 R1–R5 与方向/参数顺序） |
| `test_classify.py` | 分类用例集（16 个 unittest，五类 + 退化 D1–D4 + 参数区间复算） |
| `fuzz_compare.py` | 对拍：精确 `Fraction` 参考（判定 + 分类）+ 逐点采样单向验证（默认 3 万例） |
| `bench.py` | 批量判定性能（默认 10 万条线段对单个矩形） |

## 运行命令

```bash
python3 -m unittest -v test_degenerate   # 退化用例集
python3 -m unittest -v test_classify     # 分类用例集
python3 fuzz_compare.py 100000           # 对拍（可选参数：用例数）
python3 bench.py 100000                  # 性能（可选参数：线段数）
```

## API

```python
point_rect_relation(px, py, rect) -> PointRectRelation   # INSIDE / BOUNDARY / OUTSIDE
point_in_rect(px, py, rect) -> bool
segments_intersect(p1, p2, p3, p4) -> bool
segment_rect_intersects(p1, p2, rect) -> bool
clip_segment_to_rect(p1, p2, rect) -> (t0, t1, q0, q1) | None
classify_segment_rect(p1, p2, rect) -> SegRectIntersection   # 五类 + 参数区间 + 依据
classify_segments(p1, p2, p3, p4) -> SegSegIntersection      # 五类 + 双侧参数区间 + 依据
```

- 矩形为 `(xmin, ymin, xmax, ymax)`，传入自动规范化，允许宽/高为 0。
- 点为 `(x, y)`，线段为两个点，允许零长度。
- 裁剪返回参数 `0 <= t0 <= t1 <= 1`，其中 `q = p1 + t*(p2-p1)`；
  `q0 -> q1` 与原线段同向，参数顺序保留。

## 相交分类（IntersectionKind）

五类互斥且穷尽，`kind is not DISJOINT` 与旧布尔 API 完全等价：

| 类别 | 含义 | 判定依据 |
| --- | --- | --- |
| `PROPER` | 真交 | 交集含矩形内部的点（正长度穿越，或点被包含） |
| `ENDPOINT_TOUCH` | 端点接触 | 唯一接触点且为线段端点（R4） |
| `COLLINEAR_OVERLAP` | 共线重叠 | 与边共线的正长度重叠，不进入内部（R3） |
| `TANGENT` | 相切 | 线段内部单点擦触边界（角点相切、退化矩形单点命中；R5） |
| `DISJOINT` | 不相交 | 最短距离 > eps |

返回结果含参数区间 `[t0, t1]`（原线段参数化 `p(t) = p1 + t*(p2-p1)`，
`0 <= t0 <= t1 <= 1`）与端点 `q0/q1`，方向与参数顺序跟随原线段，
与 `clip_segment_to_rect` 的区间一致；区间可复算：`q == p(t)` 且
`t` 可由 `q` 反投影还原。每条结果附 `reason` 字段说明分类依据。

退化输入的明确分类与依据：

| 规则 | 情形 | 分类 |
| --- | --- | --- |
| D1 | 零长度线段 + 矩形 | 点严格在内部 → `PROPER`（被包含，真交的退化形式）；点在边界 → `ENDPOINT_TOUCH`（点即两端点） |
| D2 | 点矩形 | 无内部无边，命中必为单点：线段内部命中 → `TANGENT`；端点命中 → `ENDPOINT_TOUCH` |
| D3 | 线矩形（宽/高为 0） | 共线正长度重叠 → `COLLINEAR_OVERLAP`；横穿为单点命中，规则同 D2 |
| D4 | 线段-线段 | 内部-内部单点必为穿越 → `PROPER`；接触点含任一端点 → `ENDPOINT_TOUCH`；共线正长度重叠 → `COLLINEAR_OVERLAP`。直线段间不存在"内部单点擦触"，`TANGENT` 不由线段-线段返回 |

## 退化规则（显式定义，全库一致）

| 规则 | 情形 | 结论 |
| --- | --- | --- |
| R1 | 零长度线段 | 视为一个点；点在矩形上即相交。裁剪返回同一点 |
| R2 | 点矩形 / 线矩形 | 不特判：点在其上只可能是"边界"；线段到区域最短距离 `<= eps` 即相交 |
| R3 | 线段与边共线重叠 | 距离为 0，相交；裁剪返回重叠段，方向跟随原线段 |
| R4 | 端点在角上 / 边上 | 属"边界"，相交；裁剪保留该端点 |
| R5 | 相切（仅接触一点） | 相交；裁剪返回零长度结果段（`t0 == t1`，`q0 == q1`） |

## 容差

默认 `EPS = 1e-9`（绝对容差，`rectclip.EPS`，每个函数可覆盖）。

- 依据：布局坐标量级约 1e-3 ~ 1e6，float64 舍入误差约 1e-13 ~ 1e-10；
  1e-9 远高于舍入噪声，又远低于有语义的几何间隙（一般 >= 1e-6）。
- 统一闭规则：最短距离 `<= eps` 即相交，因此**相切算相交**（精确相切距离为 0）。
- 取舍：距离落在 `(0, eps]` 的"近失"也会被算成相切；需要更严格的结论可调小 `eps`。
- 裁剪时 `eps` 只用于"平行且在窗外"的拒绝；裁剪边界本身用精确矩形，
  保证结果端点严格落在矩形上。

## 对拍方法

1. **精确分数算术**（`fraction.Fraction`）独立重写 orientation / on-segment 判定作为真值，
   与 float+eps 实现逐例比较。随机坐标为整数 [-2000, 2000]，
   非零相交距离下界约 1/线段长（>= 2.5e-4 ≫ eps），eps 邻域不会制造分歧。
   分类同样对拍：精确 `Fraction` 版 Liang-Barsky + 同一套分类规则作为参考，
   逐例比较类别与参数区间（容差 1e-9）。
2. **逐点采样**（256 个均匀点 + 端点）做单向验证：采样发现严格内部点
   （留 1e-6 边距）则精确实现必须判相交——即"真实相交不允许漏判"。
   采样会漏相切/薄穿越，所以不做反方向断言。
3. 生成器按概率注入零长度线段（~10%）、点/线退化矩形（~15%），
   并大量复用角点坐标制造共线、端点相接、切角。
4. 同时校验裁剪：相交与裁剪非空等价、`t` 参数恒等式、`t0 <= t1`、
   结果端点在矩形上、裁剪后方向与原线段一致；分类区间与裁剪区间相同，
   且参数区间可复算（`q = p(t)`、`t` 可由 `q` 反投影还原）。

实测 100,000 例（含 13,730 零长度线段、21,120 退化矩形）与精确参考零分歧，
22,626 个采样确认的穿越用例无一漏判。分类分布（float 实现与精确参考逐例一致）：

```
线段-矩形:  proper 24499 / endpoint_touch 15988 / collinear_overlap 6533
            / tangent 942 / disjoint 52038
线段-线段:  proper 7482 / endpoint_touch 28888 / collinear_overlap 22107
            / tangent 0 / disjoint 41523        （10 万例，含 13739 条零长度线段）
```

## 性能数据

环境：Python 3.12，100,000 条随机线段对单个矩形 `(0,0,100,50)`（14,040 条相交）。

| 操作 | 总耗时 | 单条 | 吞吐 |
| --- | --- | --- | --- |
| `segment_rect_intersects` | 274.7 ms | 2.75 µs | 36.4 万条/秒 |
| `clip_segment_to_rect` | 46.3 ms | 0.46 µs | 216 万条/秒 |

算法复杂度：判定最多 2 次点-矩形 + 4 条边的线段距离（Ericson 参数化最近点，
常数次乘加），裁剪为 4 对边界的 Liang-Barsky——均为 O(1)/条，批量 **O(N)**，
额外空间 O(1)。判定的距离公式天然处理零长度线段，无需退化分支。
