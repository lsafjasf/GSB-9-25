"""进程内事件总线（仅标准库），支持分层主题与通配符订阅。

核心语义（详细说明见 README.md）：

- 事件类型是分层主题（用 "." 分层，如 "user.created"）。订阅可以是
  精确主题，也可以含通配符："*" 匹配恰好一层，"#" 匹配零层或多层
  （只能出现在末尾）。
- 投递顺序确定：先精确订阅、后通配符订阅，各自按订阅先后串行投递；
  同一轮投递在发布线程内串行执行，不做并发投递。
- 每轮投递基于发布开始时的快照迭代，但在调用每个订阅者前重新检查其
  活跃状态：取消立即生效，本轮剩余投递中绝不会再调用它。
- 发布期间新增的订阅者不参与本轮投递，下一轮才可见。
- 订阅者抛异常不会中断其他订阅者，异常以 DeliveryError 列表汇总返回。
- 支持弱引用订阅：被订阅对象回收后，订阅自动移除。
- 通配符匹配规则可通过模块级函数 topic_matches(pattern, topic) 直接断言。
"""

from __future__ import annotations

import threading
import types
import weakref

__all__ = ["EventBus", "Subscription", "DeliveryError", "topic_matches"]

_SINGLE_LEVEL_WILDCARD = "*"
_MULTI_LEVEL_WILDCARD = "#"
_LEVEL_SEPARATOR = "."


def _is_pattern(event_type) -> bool:
    """event_type 是否含通配符（仅字符串主题支持通配）。"""
    return isinstance(event_type, str) and (
        _SINGLE_LEVEL_WILDCARD in event_type or _MULTI_LEVEL_WILDCARD in event_type
    )


def _validate_pattern(pattern: str) -> None:
    """校验通配符模式，非法时抛 ValueError。

    规则："*" / "#" 必须独占一层；"#" 只能出现在最后一层。
    """
    levels = pattern.split(_LEVEL_SEPARATOR)
    for i, level in enumerate(levels):
        if _MULTI_LEVEL_WILDCARD in level:
            if level != _MULTI_LEVEL_WILDCARD or i != len(levels) - 1:
                raise ValueError(
                    f"'#' must occupy a whole level and be the last level: {pattern!r}"
                )
        elif _SINGLE_LEVEL_WILDCARD in level and level != _SINGLE_LEVEL_WILDCARD:
            raise ValueError(
                f"'*' must occupy a whole level: {pattern!r}"
            )


def topic_matches(pattern: str, topic: str) -> bool:
    """判断主题是否匹配订阅模式（规则可独立断言）。

    - 精确模式：逐层相等才算匹配；
    - "*"：匹配恰好一层（任意值）；
    - "#"：匹配零层或多层，只能作为模式的最后一层。

    非字符串主题不参与通配匹配，一律返回 False。
    """
    if not isinstance(pattern, str) or not isinstance(topic, str):
        return False
    p_levels = pattern.split(_LEVEL_SEPARATOR)
    t_levels = topic.split(_LEVEL_SEPARATOR)
    for i, p_level in enumerate(p_levels):
        if p_level == _MULTI_LEVEL_WILDCARD:
            return True  # 已校验 "#" 必为末层，匹配剩余零层或多层
        if i >= len(t_levels):
            return False
        if p_level != _SINGLE_LEVEL_WILDCARD and p_level != t_levels[i]:
            return False
    return len(p_levels) == len(t_levels)


class DeliveryError:
    """一次投递失败的记录。publish() 的返回值元素。"""

    __slots__ = ("subscription", "exception")

    def __init__(self, subscription: "Subscription", exception: BaseException):
        self.subscription = subscription
        self.exception = exception

    def __repr__(self) -> str:
        return (
            f"DeliveryError(subscription={self.subscription!r}, "
            f"exception={self.exception!r})"
        )


class Subscription:
    """订阅句柄。cancel() 幂等且立即生效。"""

    __slots__ = ("_bus", "event_type", "once", "filter", "wildcard", "_active",
                 "_committed")

    def __init__(self, bus: "EventBus", event_type, once: bool, filter):
        self._bus = bus
        self.event_type = event_type
        self.once = once
        self.filter = filter
        self.wildcard = _is_pattern(event_type)
        self._active = True
        self._committed = {}  # 线程 ident -> 该线程已承诺、未完成的调用数

    @property
    def active(self) -> bool:
        """订阅是否仍然有效（未被取消、once 未消耗、弱引用未失效）。"""
        return self._active

    def cancel(self) -> None:
        """取消订阅。重复取消不报错；本轮投递中立即不再被调用。"""
        self._bus.unsubscribe(self)

    def _matches(self, args, kwargs) -> bool:
        return self.filter is None or bool(self.filter(*args, **kwargs))

    def _target(self):
        """返回当前可调用的处理器；弱引用已失效时返回 None。"""
        raise NotImplementedError

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} event_type={self.event_type!r} "
            f"once={self.once} wildcard={self.wildcard} active={self._active}>"
        )


class _StrongSubscription(Subscription):
    """强引用订阅：只要订阅存在，处理器（及其绑定对象）就不会被回收。"""

    __slots__ = ("handler",)

    def __init__(self, bus, event_type, handler, once, filter):
        super().__init__(bus, event_type, once, filter)
        self.handler = handler

    def _target(self):
        return self.handler


class _WeakSubscription(Subscription):
    """弱引用订阅：不阻止处理器所属对象被回收，回收后订阅自动移除。"""

    __slots__ = ("_ref",)

    def __init__(self, bus, event_type, handler, once, filter):
        super().__init__(bus, event_type, once, filter)
        self._ref = self._make_ref(handler)

    def _make_ref(self, handler):
        def _on_dead(ref):
            # 目标对象回收后，立即从总线注销（回调中拿锁是安全的：
            # RLock 可重入，且其他线程持锁时只是短暂阻塞）。
            self._bus.unsubscribe(self)

        if isinstance(handler, types.MethodType):
            return weakref.WeakMethod(handler, _on_dead)
        return weakref.ref(handler, _on_dead)

    def _target(self):
        return self._ref()


class EventBus:
    """线程安全的进程内事件总线。

    线程模型：
    - 所有订阅列表的读写都由同一把 RLock 保护，并发 publish /
      subscribe / unsubscribe 不会破坏列表结构。
    - 锁只保护结构，不在持锁期间调用用户代码（处理器与过滤器都在
      锁外执行），因此订阅者里可以安全地再订阅、取消、递归发布。
    - 同一轮投递在发布线程内按订阅顺序串行执行；多个线程并发
      publish 时，同一订阅者可能被多个线程并发调用，订阅者需自行
      保证线程安全（once 订阅除外，保证全局至多投递一次）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._subs: dict = {}  # event_type -> [Subscription, ...]（按订阅顺序）
        self._wild_subs: list = []  # 通配符订阅，按订阅顺序

    def _add_sub(self, sub: "Subscription") -> None:
        with self._lock:
            if sub.wildcard:
                _validate_pattern(sub.event_type)
                self._wild_subs.append(sub)
            else:
                self._subs.setdefault(sub.event_type, []).append(sub)

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, event_type, handler, *, once: bool = False, filter=None):
        """按主题订阅，返回 Subscription 句柄。

        event_type 为含 "*" / "#" 的字符串时按通配符模式订阅
        （"*" 匹配一层，"#" 匹配末尾零层或多层），否则精确匹配。

        - once=True：首次实际投递前自动取消，全局至多投递一次。
        - filter：与处理器同签名的可调用对象，返回 False 则跳过本次投递。
        """
        if not callable(handler):
            raise TypeError("handler must be callable")
        sub = _StrongSubscription(self, event_type, handler, once, filter)
        self._add_sub(sub)
        return sub

    def subscribe_once(self, event_type, handler, *, filter=None):
        """一次性订阅的便捷写法。"""
        return self.subscribe(event_type, handler, once=True, filter=filter)

    def subscribe_weak(self, event_type, handler, *, once: bool = False, filter=None):
        """弱引用订阅。handler 通常是绑定方法；所属对象被回收后订阅自动移除。

        注意：调用方需保证处理器本身（对普通函数而言）另有强引用，
        否则订阅会立即失效。典型用法是 subscribe_weak(evt, obj.method)。
        """
        if not callable(handler):
            raise TypeError("handler must be callable")
        sub = _WeakSubscription(self, event_type, handler, once, filter)
        self._add_sub(sub)
        return sub

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #
    def unsubscribe(self, subscription: Subscription) -> bool:
        """取消订阅。幂等：重复取消返回 False，不报错。

        立即生效：返回后，任何正在进行或之后的投递都不会再调用它；
        其他线程已承诺但尚未完成的调用会被等待至结束，因此返回即保证
        该订阅者不再被调用。（在订阅者内部自取消不会等待自身，不会死锁。）
        """
        with self._lock:
            if not subscription._active:
                return False
            subscription._active = False
            if subscription.wildcard:
                lst = self._wild_subs
            else:
                lst = self._subs.get(subscription.event_type)
            if lst is not None:
                try:
                    lst.remove(subscription)
                except ValueError:
                    pass
                if not subscription.wildcard and not lst:
                    del self._subs[subscription.event_type]
            me = threading.get_ident()
            while any(t != me for t in subscription._committed):
                self._idle.wait()
            return True

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(self, event_type, *args, **kwargs) -> list:
        """发布事件，返回 DeliveryError 列表（空列表=全部成功）。

        投递规则：
        - 顺序确定：先精确订阅、后通配符订阅，各自按订阅先后串行投递。
        - 迭代发布开始时的快照：本轮新增的订阅者不参与本轮。
        - 调用前重新检查活跃状态：本轮中被取消的订阅者（包括自取消）
          不会再被调用；其余订阅者不受影响，继续按序投递。
        - once 订阅在首次匹配投递前原子移除，并发发布下至多投递一次。
        - 订阅者/过滤器抛出的异常被收集，不影响后续订阅者。
        """
        with self._lock:
            snapshot = tuple(self._subs.get(event_type, ()))
            if self._wild_subs and isinstance(event_type, str):
                snapshot += tuple(
                    sub for sub in self._wild_subs
                    if topic_matches(sub.event_type, event_type)
                )
        errors: list = []
        me = threading.get_ident()
        for sub in snapshot:
            if not sub._active:  # 快速路径：已被取消（无锁读单个布尔值）
                continue
            try:
                if not sub._matches(args, kwargs):
                    continue
            except Exception as exc:  # 过滤器异常同样汇总，不中断投递
                errors.append(DeliveryError(sub, exc))
                continue
            with self._lock:
                if not sub._active:  # 锁内复查，取消立即生效
                    continue
                if sub.once:
                    self.unsubscribe(sub)  # 原子消耗，保证至多一次
                # 承诺本次调用：与取消互斥，取消方会等待已承诺的调用完成
                sub._committed[me] = sub._committed.get(me, 0) + 1
            try:
                target = sub._target()
                if target is None:  # 弱引用恰好在此时失效
                    self.unsubscribe(sub)
                    continue
                try:
                    target(*args, **kwargs)
                except Exception as exc:
                    errors.append(DeliveryError(sub, exc))
            finally:
                with self._lock:
                    remaining = sub._committed[me] - 1
                    if remaining:
                        sub._committed[me] = remaining
                    else:
                        del sub._committed[me]
                        if not sub._committed:
                            self._idle.notify_all()
        return errors

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def subscriber_count(self, event_type=None) -> int:
        """返回活跃订阅数；指定 event_type 时统计会收到该事件的订阅数
        （含匹配该主题的通配符订阅）。"""
        with self._lock:
            if event_type is not None:
                count = len(self._subs.get(event_type, ()))
                if isinstance(event_type, str):
                    count += sum(
                        1 for sub in self._wild_subs
                        if topic_matches(sub.event_type, event_type)
                    )
                return count
            return sum(len(lst) for lst in self._subs.values()) + len(self._wild_subs)

    def subscriptions(self, event_type=None) -> list:
        """查询当前订阅关系，返回活跃 Subscription 列表。

        - event_type=None：返回全部订阅（先精确、后通配符，各自按订阅顺序）。
        - 指定 event_type：返回会收到该事件的订阅，顺序即实际投递顺序。
        """
        with self._lock:
            if event_type is None:
                subs = [s for lst in self._subs.values() for s in lst]
                subs += self._wild_subs
            else:
                subs = list(self._subs.get(event_type, ()))
                if isinstance(event_type, str):
                    subs += [s for s in self._wild_subs
                             if topic_matches(s.event_type, event_type)]
            return [s for s in subs if s._active]

    def clear(self) -> None:
        """取消全部订阅。"""
        with self._lock:
            for lst in self._subs.values():
                for sub in lst:
                    sub._active = False
            for sub in self._wild_subs:
                sub._active = False
            self._subs.clear()
            self._wild_subs.clear()
