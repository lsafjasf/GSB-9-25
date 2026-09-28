"""进程内事件总线（仅标准库）。

语义概要（详见 README.md）：
- 订阅顺序即投递顺序；单个 publish 调用内串行投递，不做并发投递。
- 取消立即生效：投递循环在调用每个订阅者前检查其存活状态，
  本轮中已被取消（包括被前面订阅者取消、自取消）的订阅者一律跳过。
- 发布期间新增/移除订阅采用快照语义：新增者本轮不投递，移除者本轮不再调用，
  不会重复投递也不会漏投递。
- once 与 filter 组合时，过滤器通过（返回真值）才消耗一次性订阅；
  不匹配或过滤器抛异常的事件不会用掉这次机会。
- 订阅者（含过滤条件）抛出的异常不中断投递，全部汇总后由 publish 返回。
- 支持弱引用订阅：被订阅对象回收后订阅自动失效并从注册表清除。
"""

from __future__ import annotations

import inspect
import threading
import weakref


class Subscription:
    """订阅句柄。cancel() 幂等，重复调用不报错。"""

    __slots__ = (
        "_bus", "event_type", "once", "filter",
        "_weak", "_ref", "_strong",
        "_lock", "_cancelled", "_claimed",
    )

    def __init__(self, bus, event_type, callback, once, weak, filter):
        self._bus = bus
        self.event_type = event_type
        self.once = once
        self.filter = filter
        self._weak = weak
        self._lock = threading.Lock()
        self._cancelled = False
        self._claimed = False
        if weak:
            self._strong = None
            if inspect.ismethod(callback):
                self._ref = weakref.WeakMethod(callback, self._on_dead)
            else:
                self._ref = weakref.ref(callback, self._on_dead)
        else:
            self._strong = callback
            self._ref = None

    def _on_dead(self, _ref):
        # 弱引用对象被回收：自动从总线注销
        bus = self._bus
        if bus is not None:
            bus._remove(self)

    def callback(self):
        """返回当前可调用的回调；弱引用已死则返回 None。"""
        if self._weak:
            return self._ref()
        return self._strong

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self):
        """取消订阅，立即生效，幂等。"""
        bus = self._bus
        if bus is not None:
            bus._remove(self)

    def _claim_once(self):
        """once 订阅的原子认领：并发发布下保证至多投递一次。"""
        with self._lock:
            if self._claimed or self._cancelled:
                return False
            self._claimed = True
            return True


class EventBus:
    """线程安全的事件总线。注册表由锁保护，投递在锁外进行。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._subs = {}  # event_type -> list[Subscription]，按订阅顺序排列

    def subscribe(self, event_type, callback, *, once=False, weak=False, filter=None):
        """订阅事件。

        event_type: 任意可哈希对象（含空字符串）。
        callback:   可调用对象，发布时以 publish 的参数调用。
        once:       为 True 时仅投递一次（并发发布下至多一次），随后自动注销。
        weak:       为 True 时弱引用回调（绑定方法用 WeakMethod），
                    对象可回收后订阅自动失效；调用方需自行保持普通函数的引用。
        filter:     可选谓词，与 callback 收到相同参数，返回 False 则跳过本次投递。
                    与 once 组合时，过滤器通过才消耗一次性订阅。
        返回 Subscription 句柄，可用其 cancel() 取消。
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        if filter is not None and not callable(filter):
            raise TypeError("filter must be callable")
        sub = Subscription(self, event_type, callback, once, weak, filter)
        with self._lock:
            self._subs.setdefault(event_type, []).append(sub)
        return sub

    def unsubscribe(self, event_type, callback):
        """按回调取消订阅；不存在时不报错。重复取消安全。"""
        with self._lock:
            subs = self._subs.get(event_type)
            if not subs:
                return
            for sub in list(subs):
                cb = sub.callback()
                # 绑定方法用 == 比较（同一对象同一方法即相等）
                if cb is callback or cb == callback:
                    self._remove_locked(sub)

    def publish(self, event_type, *args, **kwargs):
        """发布事件，按订阅顺序串行投递。

        返回本次投递中订阅者/过滤条件抛出的异常列表（可能为空）。
        对没有订阅者的事件类型发布是合法空操作，返回空列表。
        """
        with self._lock:
            snapshot = list(self._subs.get(event_type, ()))
        errors = []
        for sub in snapshot:
            if sub.cancelled:
                # 取消立即生效：本轮不再调用（含被前面订阅者取消的情形）
                continue
            callback = sub.callback()
            if callback is None:
                # 弱引用已死，顺手清理
                self._remove(sub)
                continue
            if sub.filter is not None:
                # 先求值过滤器：未通过（或抛异常）不消耗 once 订阅
                try:
                    if not sub.filter(*args, **kwargs):
                        continue
                except Exception as exc:  # 不中断其他订阅者，汇总返回
                    errors.append(exc)
                    continue
            if sub.once and not sub._claim_once():
                continue
            if sub.once:
                self._remove(sub)
            try:
                callback(*args, **kwargs)
            except Exception as exc:  # 不中断其他订阅者，汇总返回
                errors.append(exc)
        return errors

    def subscriber_count(self, event_type=None):
        """返回当前活跃订阅数；event_type 为 None 时返回总数。"""
        with self._lock:
            if event_type is not None:
                return len(self._subs.get(event_type, ()))
            return sum(len(subs) for subs in self._subs.values())

    def _remove(self, sub):
        with self._lock:
            self._remove_locked(sub)

    def _remove_locked(self, sub):
        sub._cancelled = True
        subs = self._subs.get(sub.event_type)
        if subs is None:
            return
        try:
            subs.remove(sub)
        except ValueError:
            return
        if not subs:
            del self._subs[sub.event_type]
