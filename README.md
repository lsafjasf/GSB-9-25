# racefw — 并发竞态复现框架（纯 Python 3 标准库）

用「确定性伪随机调度」替代「多跑几次碰运气」：给定**种子 + 场景**，
重复运行必然得到**相同的交错与相同的结论**；失败时输出完整事件序列，
可据此直接还原交错。

## 结构

```
racefw/
  core.py        # Scheduler（pct/random/rr 策略）、Event、DeadlockError、format_trace
  primitives.py  # Lock / Semaphore / Condition（生成器式，内部自动产生切换点）
  explore.py     # explore()：多种子调度搜索 + 覆盖统计
examples/
  lost_update.py   # 丢失更新（read / 切换窗口 / write）
  deadlock.py      # 双锁反序死锁
  double_init.py   # check-then-act 重复初始化
tests/
  selftest.py      # 自测：确定性 / 复现 / 覆盖 / 原语正确性 / 真实并发对照
```

## 运行

```bash
python3 tests/selftest.py        # 全部自测（14 项检查）
python3 examples/lost_update.py  # 单独复现丢失更新 + 真实线程压测对照
python3 examples/deadlock.py     # 单独复现死锁
python3 examples/double_init.py  # 单独复现重复初始化
```

## 用法

线程是生成器函数，在关键位置 `yield from sched.preempt("说明")` 让出控制权；
同步原语以 `yield from lock.acquire()` 形式使用，内部自带切换点：

```python
from racefw import Scheduler, Lock, explore, format_trace

def scenario(sched):
    lock = Lock(sched, "L")
    def worker():
        yield from lock.acquire()
        yield from sched.preempt("critical section")
        yield from lock.release()
    sched.spawn(worker, name="w0")
    sched.spawn(worker, name="w1")

res = explore(scenario, runs=100, strategy="pct",
              bug_fn=lambda s: "..." or None)   # 判定缺陷
print(res.report())                              # 覆盖统计
seed, kind, s = res.failures[0]
print(format_trace(s))                           # 完整事件序列
```

## 可复现性

调度决策全部来自 `random.Random(seed)`：同种子同场景 ⇒ 事件序列逐字节相同
（自测第 1 项验证）。复现某个失败只需 `Scheduler(seed=<失败种子>)` 重跑。

## 事件序列

每个事件包含：序号、逻辑时钟、线程、类型（switch / preempt / lock.try /
lock.acquired / lock.release / block / wake / cond.* / sem.* / pct / finish）、
资源名、源码位置（file:line）。其中 switch / finish / block / pct（PCT 降级）
四类事件的位置直接取自线程生成器的挂起帧（`gi_frame.f_lineno`），即切换落向、
线程结束、阻塞发起、优先级降级发生时场景侧的真实行；未启动线程则定位到
函数体首条语句，无需再靠上一条 preempt 事件反推。例如死锁复现输出可直读：
`t2 获 B → t1 获 A → t1 请求 B 阻塞 → t2 请求 A 阻塞 → DeadlockError`。

## 调度搜索与覆盖统计

`explore()` 用种子 `[seed0, seed0+runs)` 逐次运行，统计：

- `distinct_interleavings`：按调度决策序列（每步选中的线程）去重的交错数；
- `distinct_traces`：按完整事件序列去重的轨迹数；
- `distinct_outcomes`：结果分布；`failures`：命中缺陷的种子列表。

探索策略（`strategy` 参数）：

- `pct`（默认）：PCT 算法——给线程随机优先级，每步以
  `pct_change_points/expected_steps` 的概率降低刚运行线程的优先级。
  少量变更点即可高效触发深层交错，适合找 bug；
- `random`：每步在可运行线程中均匀随机，交错多样性最高（自测中
  60 次运行探索出 52 种不同交错），适合铺覆盖；
- `rr`：轮转，用于对照调试。

实测（`python3 tests/selftest.py`）：丢失更新场景 60 次 PCT 运行探索
35 种交错、43 次命中缺陷；死锁 30 次运行 15 次命中；重复初始化 20 次
运行 8 次命中。

## 为什么普通压测难以复现

三个示例的竞态窗口都只有「两条字节码之间」。真实线程（GIL，默认 5ms
切换间隔）几乎总是让一个线程整体跑完，自测实测：真实线程压测
丢失更新 0/300、死锁 0/100、重复初始化 0/300；框架在相同场景下
确定性命中（如丢失更新 141/200）。且真实死锁会永久挂起只能超时
间接观察，框架则由调度器直接检测「无可运行线程」并抛出 `DeadlockError`。

## 与真实并发对照

`tests/selftest.py` 第 5 项：同一段 read-modify-write 逻辑分别用真实
`threading` 跑 300 次、用框架 PCT 跑 200 次，断言
**真实线程观察到的结果集合 ⊆ 框架探索到的结果集合**（实测真实只出现
`counter=8`，框架覆盖全部合法结果 `counter=4..8`）。
