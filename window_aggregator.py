"""事件窗口聚合库（仅标准库，整数时间）。

支持固定窗口（step == window_size）与滑动窗口（step < window_size，
也允许 step > window_size 的跳跃窗口），对乱序事件按宽限时间（grace）
容忍迟到，并对已输出窗口发出更新通知。

窗口语义
--------
- 窗口为半开区间 [start, end)，end = start + window_size。
- 窗口起点对齐到 0 的整数倍步长：start = k * step（k 为任意整数，含负数）。
- 边界归属：事件时间 t 恰好等于某窗口起点时，归入以 t 为起点的窗口；
  它不属于以 t 为终点的上一个窗口（因为区间左闭右开）。
- 事件 t 属于所有满足 start <= t < start + window_size 的窗口。

迟到与水位线
------------
- 水位线 watermark = 迄今为止见过的最大事件时间（单调不减）。
- 首次输出：当 watermark >= end 时，窗口首次通过 on_emit(result, is_update=False) 输出。
- 更新通知：宽限内（event_time >= watermark - grace）到达的迟到事件仍计入窗口；
  若该窗口已输出过，则再次触发 on_emit(result, is_update=True)。
- 定稿释放：当 watermark >= end + grace 时，窗口结果不再变化，
  通过 on_finalize(result) 通知后从内存中释放。
- 丢弃：event_time < watermark - grace 的事件被丢弃，dropped_count 加一。
  （可证明：凡是未被丢弃的事件，其所属窗口必然尚未定稿，因此一定能被正确计入。）
- flush()：流结束时调用，强制输出并定稿释放所有存活窗口。

顺序无关性
----------
只要数据流的乱序程度不超过 grace（即不存在被丢弃的事件），
同一批事件以任意顺序喂入，最终各窗口的 (count, total) 完全一致：
窗口状态只取决于事件集合本身，水位线只取决于最大事件时间。

内存上界
--------
任一时刻存活窗口数 <= (window_size + grace) // step + 1，与事件总量无关。
证明：存活窗口起点 s 满足 (1) s <= watermark（窗口由事件创建，事件时间 >= s）；
(2) 未定稿：s + window_size + grace > watermark。
故 s 落在长度 window_size + grace 的区间 (watermark - window_size - grace, watermark] 内，
起点间距为 step，个数至多为 floor((window_size + grace) / step) + 1。
"""


class WindowResult:
    """某一窗口在某一时刻的统计快照。"""

    __slots__ = ("start", "end", "count", "total")

    def __init__(self, start, end, count, total):
        self.start = start
        self.end = end
        self.count = count
        self.total = total

    def __eq__(self, other):
        return (
            isinstance(other, WindowResult)
            and (self.start, self.end, self.count, self.total)
            == (other.start, other.end, other.count, other.total)
        )

    def __hash__(self):
        return hash((self.start, self.end, self.count, self.total))

    def __repr__(self):
        return (
            "WindowResult(start=%r, end=%r, count=%r, total=%r)"
            % (self.start, self.end, self.count, self.total)
        )


def _check_int(name, value, minimum):
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("%s 必须是整数，得到 %r" % (name, value))
    if value < minimum:
        raise ValueError("%s 必须 >= %d，得到 %r" % (name, minimum, value))


class WindowAggregator:
    """固定/滑动窗口计数与求和聚合器。

    参数:
        window_size: 窗口长度（正整数）。
        step: 窗口步长（正整数）；缺省等于 window_size，即固定（滚动）窗口。
        grace: 宽限时间（非负整数），允许的最大迟到幅度。
        on_emit: 回调 fn(result, is_update)，窗口首次输出或结果被迟到事件更新时调用。
        on_finalize: 回调 fn(result)，窗口定稿并释放时调用。
    """

    def __init__(self, window_size, step=None, grace=0, on_emit=None, on_finalize=None):
        _check_int("window_size", window_size, 1)
        if step is None:
            step = window_size
        _check_int("step", step, 1)
        _check_int("grace", grace, 0)
        self.window_size = window_size
        self.step = step
        self.grace = grace
        self.on_emit = on_emit
        self.on_finalize = on_finalize
        # start -> [count, total]；只保存“存活”（未定稿）窗口
        self._windows = {}
        # 已首次输出但仍存活（等待定稿）的窗口起点集合
        self._emitted = set()
        self._watermark = None
        self.dropped_count = 0
        self.event_count = 0

    @property
    def watermark(self):
        """当前水位线（已见最大事件时间）；无事件时为 None。"""
        return self._watermark

    @property
    def live_window_count(self):
        """当前存活（占用内存）的窗口数。"""
        return len(self._windows)

    def memory_bound(self):
        """存活窗口数的上界：(window_size + grace) // step + 1。"""
        return (self.window_size + self.grace) // self.step + 1

    def add(self, event_time, value=1):
        """喂入一个事件。返回 True 表示被接受，False 表示因超出宽限被丢弃。"""
        _check_int("event_time", event_time, -(2**63))
        if self._watermark is None or event_time > self._watermark:
            self._watermark = event_time
        if event_time < self._watermark - self.grace:
            self.dropped_count += 1
            return False
        self.event_count += 1
        # 所有包含 event_time 的窗口起点：start = k*step 且
        # event_time - window_size < start <= event_time
        k_max = event_time // self.step
        k_min = -((self.window_size - 1 - event_time) // self.step)
        touched = []
        for k in range(k_min, k_max + 1):
            start = k * self.step
            state = self._windows.get(start)
            if state is None:
                state = self._windows[start] = [0, 0]
            state[0] += 1
            state[1] += value
            touched.append(start)
        self._advance(touched)
        return True

    def flush(self):
        """流结束：输出所有未输出窗口，并定稿释放全部存活窗口。"""
        for start in sorted(self._windows):
            if start not in self._emitted:
                self._emitted.add(start)
                if self.on_emit is not None:
                    self.on_emit(self._snapshot(start), False)
        for start in sorted(list(self._windows)):
            self._finalize(start)

    def snapshot(self):
        """返回当前所有存活窗口的快照列表（按起点排序），主要用于测试/调试。"""
        return [self._snapshot(s) for s in sorted(self._windows)]

    # ---- 内部 ----

    def _snapshot(self, start):
        count, total = self._windows[start]
        return WindowResult(start, start + self.window_size, count, total)

    def _advance(self, touched):
        wm = self._watermark
        emitted_now = []
        finalized = []
        for start in self._windows:
            end = start + self.window_size
            if end <= wm and start not in self._emitted:
                self._emitted.add(start)
                emitted_now.append(start)
            if end + self.grace <= wm:
                finalized.append(start)
        if self.on_emit is not None:
            for start in sorted(emitted_now):
                self.on_emit(self._snapshot(start), False)
            for start in sorted(touched):
                # 此前已输出、本次被迟到事件修改且未在本次重新首发的窗口 -> 更新通知
                if (
                    start in self._emitted
                    and start not in emitted_now
                    and start in self._windows
                ):
                    self.on_emit(self._snapshot(start), True)
        for start in sorted(finalized):
            self._finalize(start)

    def _finalize(self, start):
        result = self._snapshot(start)
        del self._windows[start]
        self._emitted.discard(start)
        if self.on_finalize is not None:
            self.on_finalize(result)
