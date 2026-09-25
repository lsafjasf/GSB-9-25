"""进程内事件总线（仅标准库）。

核心语义（详细说明见 README.md）：

- 订阅顺序即投递顺序；同一轮投递在发布线程内串行执行，不做并发投递。
- 每轮投递基于发布开始时的快照迭代，但在调用每个订阅者前重新检查其
  活跃状态：取消立即生效，本轮剩余投递中绝不会再调用它。
- 发布期间新增的订阅者不参与本轮投递，下一轮才可见。
- 订阅者抛异常不会中断其他订阅者，异常以 DeliveryError 列表汇总返回。
- 支持弱引用订阅：被订阅对象回收后，订阅自动移除。
"""

from __future__ import annotations

import threading
import types
import weakref

__all__ = ["EventBus", "Subscription", "DeliveryError"]


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

    __slots__ = ("_bus", "event_type", "once", "filter", "_active")

    def __init__(self, bus: "EventBus", event_type, once: bool, filter):
        self._bus = bus
        self.event_type = event_type
        self.once = once
        self.filter = filter
        self._active = True

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
            f"once={self.once} active={self._active}>"
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
        self._subs: dict = {}  # event_type -> [Subscription, ...]（按订阅顺序）

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, event_type, handler, *, once: bool = False, filter=None):
        """按事件类型订阅，返回 Subscription 句柄。

        - once=True：首次实际投递前自动取消，全局至多投递一次。
        - filter：与处理器同签名的可调用对象，返回 False 则跳过本次投递。
        """
        if not callable(handler):
            raise TypeError("handler must be callable")
        sub = _StrongSubscription(self, event_type, handler, once, filter)
        with self._lock:
            self._subs.setdefault(event_type, []).append(sub)
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
        with self._lock:
            self._subs.setdefault(event_type, []).append(sub)
        return sub

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #
    def unsubscribe(self, subscription: Subscription) -> bool:
        """取消订阅。幂等：重复取消返回 False，不报错。

        立即生效：返回后，任何正在进行或之后的投递都不会再调用它。
        """
        with self._lock:
            if not subscription._active:
                return False
            subscription._active = False
            lst = self._subs.get(subscription.event_type)
            if lst is not None:
                try:
                    lst.remove(subscription)
                except ValueError:
                    pass
                if not lst:
                    del self._subs[subscription.event_type]
            return True

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(self, event_type, *args, **kwargs) -> list:
        """发布事件，按订阅顺序投递，返回 DeliveryError 列表（空列表=全部成功）。

        投递规则：
        - 迭代发布开始时的快照：本轮新增的订阅者不参与本轮。
        - 调用前重新检查活跃状态：本轮中被取消的订阅者（包括自取消）
          不会再被调用；其余订阅者不受影响，继续按序投递。
        - once 订阅在首次匹配投递前原子移除，并发发布下至多投递一次。
        - 订阅者/过滤器抛出的异常被收集，不影响后续订阅者。
        """
        with self._lock:
            snapshot = tuple(self._subs.get(event_type, ()))
        errors: list = []
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
            target = sub._target()
            if target is None:  # 弱引用恰好在此时失效
                self.unsubscribe(sub)
                continue
            try:
                target(*args, **kwargs)
            except Exception as exc:
                errors.append(DeliveryError(sub, exc))
        return errors

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def subscriber_count(self, event_type=None) -> int:
        """返回活跃订阅数；指定 event_type 时只统计该类型。"""
        with self._lock:
            if event_type is not None:
                return len(self._subs.get(event_type, ()))
            return sum(len(lst) for lst in self._subs.values())

    def clear(self) -> None:
        """取消全部订阅。"""
        with self._lock:
            for lst in self._subs.values():
                for sub in lst:
                    sub._active = False
            self._subs.clear()
