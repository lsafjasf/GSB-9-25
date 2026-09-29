# event_bus — 进程内事件总线

纯 Python 3 标准库实现，线程安全。模块间用事件解耦，同时解决两个常见痛点：
订阅者忘记取消导致的对象泄漏 / 重复投递，以及多线程并发发布。
支持分层主题与通配符订阅，模块多时不必逐事件类型订阅。

## 文件

- `event_bus.py` — 库源码（无第三方依赖）
- `test_event_bus.py` — 自测（unittest，43 个用例）

## 运行

```bash
python3 -m unittest test_event_bus -v
```

## 快速上手

```python
from event_bus import EventBus

bus = EventBus()

# 订阅 / 一次性订阅 / 条件过滤
sub = bus.subscribe("user.created", lambda u: print("created", u))
bus.subscribe_once("user.created", lambda u: print("only first"))
bus.subscribe("user.created", lambda u: print("adult", u),
              filter=lambda u: u["age"] >= 18)

# 通配符订阅："*" 匹配一层，前缀通配，"#" 匹配末尾零层或多层
bus.subscribe("user.*", lambda u: print("any user event"))
bus.subscribe("order.#", lambda u: print("whole order subtree"))

# 弱引用订阅：obj 被回收后订阅自动移除，不再投递
bus.subscribe_weak("user.created", obj.on_user_created)

errors = bus.publish("user.created", {"name": "a", "age": 20})
# errors 是 DeliveryError 列表，空列表表示全部成功

sub.cancel()  # 取消；重复调用不报错

# 查询当前订阅关系
bus.subscriptions()                  # 全部活跃订阅（先精确、后通配符）
bus.subscriptions("user.created")    # 会收到该事件的订阅，顺序即投递顺序
```

## 分层主题与通配符

事件类型是按 `.` 分层的主题（如 `user.created`）。订阅时 event_type
含 `*` 或 `#` 即视为通配符模式，否则精确匹配：

- `*`：匹配恰好一层，可出现在任意层，如 `user.*`、`*.created`、`user.*.admin`；
- `#`：匹配零层或多层，只能作为最后一层，如 `user.#` 匹配
  `user`、`user.created`、`user.created.admin`；
- `*` / `#` 必须独占一层，`#` 必须在末尾，否则 `subscribe` 抛 `ValueError`；
- 非字符串事件类型不参与通配，维持精确匹配语义。

匹配规则由模块级函数 `topic_matches(pattern, topic)` 实现，可脱离总线
直接断言（见 `TestTopicMatchingRules`）。

同一事件的投递顺序是确定的：**先精确订阅、后通配符订阅，各自按订阅先后**。
通配符订阅与精确订阅一样支持 `once`、`filter`、弱引用与取消，语义不变。

## 生命周期

一次订阅的生命周期：`subscribe()` 创建并激活 → 参与投递 →
以下任一事件使其失效（`sub.active` 变为 `False`，且幂等）：

1. `sub.cancel()` 或 `bus.unsubscribe(sub)` 显式取消；
2. `once=True` 的订阅在首次匹配投递前被自动消耗；
3. 弱引用订阅的目标对象被垃圾回收（通过 weakref 回调立即注销）。

失效的订阅会被移出订阅列表，不再占用总线资源。

## 投递语义（确定规则）

- **顺序**：先精确订阅、后通配符订阅，各自按订阅先后投递；同一轮投递在
  发布线程内**串行**执行，不做并发投递。多个线程并发 `publish` 时，
  同一订阅者可能被多个线程并发调用，订阅者需自行保证线程安全
  （`once` 订阅例外，见下）。
- **取消立即生效**：每轮投递基于发布开始时的快照迭代，但在调用每个订阅者
  之前重新检查其活跃状态，且调用的"承诺"与取消互斥：`unsubscribe` 返回时，
  其他线程已承诺的调用会被等待至结束。因此取消返回后，任何正在进行或
  之后的投递都绝不会再调用它（订阅者内自取消不等待自身，不会死锁）。
- **本轮新增不参与本轮**：发布期间新增的订阅者从下一轮起生效，
  不会重复投递也不会漏投递。
- **once**：在首次匹配（过滤器通过）投递前于锁内原子移除，
  即使多线程并发发布同一事件，也保证全局至多投递一次；
  过滤器未通过不消耗 once。
- **递归发布**：订阅者中可安全地再次 `publish`（含同类型事件），
  语义为深度优先：内层投递完整结束后，外层继续按快照投递。
- **异常汇总**：订阅者或过滤器抛出的异常不会中断其他订阅者，
  全部收集为 `DeliveryError(subscription, exception)` 列表由 `publish` 返回。
- **空事件类型**：空字符串是合法事件类型；向无订阅者的类型发布
  正常返回空错误列表。
- **线程安全**：订阅列表的所有读写由同一把 `RLock` 保护，并发
  `publish` / `subscribe` / `unsubscribe` 不会破坏列表结构；
  用户代码（处理器、过滤器）一律在锁外执行，订阅者内可自由再订阅、
  取消、递归发布，不会死锁。

## 测试覆盖

- 基本语义：订阅顺序即投递顺序、once、过滤器、取消、重复取消、空事件类型
- 通配符：匹配规则独立断言（精确 / 单层 `*` / 多层 `#` / 非法模式拒绝）、
  先精确后通配的投递顺序、通配符与 once / filter / 弱引用 / 取消的组合、
  非字符串事件类型不受影响
- 订阅查询：全量查询、按主题查询（顺序即投递顺序）、取消后不再出现
- 投递中修改订阅列表：取消后续订阅者、自取消、新增订阅者、递归发布同类事件
- 异常：处理器/过滤器异常汇总且不中断他人
- 弱引用：对象可回收性测试（`gc.collect` 后弱引用置空、订阅自动移除）、
  强订阅阻止回收的对照测试
- 并发：8 线程 × 250 次发布计数校验、16 线程竞发 once 恰好一次、
  发布与订阅/取消高压并发下列表结构完整、取消与发布竞态下取消后绝不再调用
