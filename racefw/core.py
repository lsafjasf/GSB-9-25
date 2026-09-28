"""racefw.core — 确定性伪随机并发调度器（基于生成器的协作式线程）。

设计要点：
- 每个“线程”是一个 Python 生成器，在关键位置通过 `yield from sched.preempt(...)`
  或同步原语内部隐式让出控制权；
- 调度器用带种子的 random.Random 决定下一步运行哪个线程，
  因此「种子 + 场景」完全决定交错顺序与结论，可精确复现；
- 每一步都记录 Event（谁、何时、对哪个资源、在哪个源码位置），
  失败时可直接按事件序列还原交错。
"""
import dis
import os
import random
import sys


class _Block:
    """线程让出控制权时携带的阻塞令牌。"""
    __slots__ = ("resource",)

    def __init__(self, resource):
        self.resource = resource


class DeadlockError(Exception):
    """所有线程均被阻塞（无可运行线程）时抛出。"""

    def __init__(self, scheduler):
        self.scheduler = scheduler
        parts = ", ".join(
            f"{scheduler.threads[t]['name']} waits for {res}"
            for t, res in scheduler.blocked.items()
        )
        super().__init__(f"deadlock detected: {parts}")


def _gen_location(gen):
    """取线程生成器「当前停下点」对应的场景源码位置 file:line。

    生成器在 ``yield``/``yield from`` 处挂起时，``gi_frame`` 停在外层
    （场景）帧上，``f_lineno`` 就是场景侧让出点的真实行号；尚未启动时
    帧停在 ``def`` 行，则用字节码行号表取函数体首条语句的行号。
    """
    frame = gen.gi_frame
    if frame is None:
        return None
    if frame.f_lasti == 0:
        first = frame.f_code.co_firstlineno
        for _, lineno in dis.findlinestarts(frame.f_code):
            if lineno != first:
                first = lineno
                break
        lineno = first
    else:
        lineno = frame.f_lineno
    return f"{os.path.basename(frame.f_code.co_filename)}:{lineno}"


class Event:
    __slots__ = ("seq", "clock", "tid", "kind", "resource", "detail", "location")

    def __init__(self, seq, clock, tid, kind, resource, detail, location):
        self.seq = seq            # 事件序号
        self.clock = clock        # 逻辑时钟
        self.tid = tid            # 线程 id
        self.kind = kind          # switch/preempt/lock.acquired/...
        self.resource = resource  # 资源名（锁/信号量/条件变量）
        self.detail = detail      # 附加说明
        self.location = location  # 源码位置 file:line

    def __repr__(self):
        return (f"Event(seq={self.seq}, tid={self.tid}, kind={self.kind}, "
                f"resource={self.resource}, loc={self.location})")


class Scheduler:
    """可注入的确定性调度器。

    strategy:
      - "pct"    : 随机优先级 + 少量优先级变更点（PCT 算法，默认，找 bug 快）。
                   每个决策点以 pct_change_points/expected_steps 的概率
                   降低刚运行线程的优先级，逼出线程切换。
      - "random" : 每个决策点在可运行线程中均匀随机选择
      - "rr"     : 轮转（用于对照/调试）
    """

    def __init__(self, seed=0, strategy="pct", pct_change_points=3,
                 expected_steps=200, name=""):
        self.seed = seed
        self.strategy = strategy
        self.rng = random.Random(seed)
        self.name = name or f"sched-{seed}"
        self.events = []
        self.clock = 0
        self.threads = {}        # tid -> {name, gen, done}
        self.runnable = []       # 可运行 tid 列表
        self.blocked = {}        # tid -> 阻塞资源名
        self.current = None
        self.env = {}            # 场景共享状态（供结果判定读取）
        self.decisions = []      # 每个调度决策点选中的 tid（交错签名）
        self._pct_prob = pct_change_points / max(expected_steps, 1)
        self._priorities = {}

    # ------------------------------------------------------------------ #
    # 线程管理
    # ------------------------------------------------------------------ #
    def spawn(self, fn, *args, name=None):
        """注册一个线程：fn 必须是生成器函数。"""
        tid = f"T{len(self.threads)}"
        self.threads[tid] = {"name": name or tid, "gen": fn(*args), "done": False}
        self.runnable.append(tid)
        return tid

    # ------------------------------------------------------------------ #
    # 事件记录
    # ------------------------------------------------------------------ #
    def log(self, kind, detail="", resource=None, tid=None, location=None, depth=1):
        self.clock += 1
        if location is None:
            frame = sys._getframe(depth)
            location = f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}"
        ev = Event(len(self.events), self.clock,
                   tid if tid is not None else self.current,
                   kind, resource, detail, location)
        self.events.append(ev)
        return ev

    def preempt(self, note=""):
        """显式切换点：记录当前源码位置并让出控制权。"""
        self.log("preempt", detail=note, location=self._user_location(2))
        yield

    @staticmethod
    def _user_location(depth=1):
        """沿调用栈找到最外层非框架（场景侧）帧，返回 file:line。

        场景可能经原语（Lock/Semaphore/Condition）间接产生切换点，
        因此跳过 racefw 包内帧，定位到场景文件的真实行。
        """
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        frame = sys._getframe(depth)
        while frame and os.path.dirname(os.path.abspath(
                frame.f_code.co_filename)) == pkg_dir:
            frame = frame.f_back
        if frame is None:  # 退化：无场景帧时用直接调用方
            frame = sys._getframe(depth)
        return f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}"

    def wake(self, tid, note=""):
        """唤醒被阻塞线程（由原语内部调用）。"""
        self.blocked.pop(tid, None)
        self.runnable.append(tid)
        self.log("wake", detail=f"wakes {self.threads[tid]['name']}: {note}", depth=2)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self):
        while self.runnable:
            tid = self._choose()
            self.current = tid
            self.decisions.append(tid)
            th = self.threads[tid]
            # 线程将从其生成器挂起点恢复，该行就是切换落向的真实场景位置
            loc = _gen_location(th["gen"])
            self.log("switch", detail=f"=> run {th['name']}", tid=tid, location=loc)
            try:
                token = next(th["gen"])
            except StopIteration as stop:
                self.log("finish", detail=f"return {stop.value!r}", tid=tid, location=loc)
                th["done"] = True
                self.current = None
                continue
            if isinstance(token, _Block):
                self.blocked[tid] = token.resource
                # 阻塞发生在场景侧发起阻塞调用的 yield from 行
                self.log("block", resource=token.resource, tid=tid,
                         location=_gen_location(th["gen"]))
            else:
                self.runnable.append(tid)
            self.current = None
        if self.blocked:
            raise DeadlockError(self)
        return self.env

    def _choose(self):
        if self.strategy == "random":
            tid = self.rng.choice(self.runnable)
        elif self.strategy == "pct":
            for t in self.runnable:
                self._priorities.setdefault(t, self.rng.random())
            if self.decisions and self.rng.random() < self._pct_prob:
                last = self.decisions[-1]
                if last in self.runnable:
                    low = min(self._priorities.values())
                    self._priorities[last] = low - 1.0
                    self.log("pct", detail=f"priority change: demote {last}",
                             tid=last,
                             location=_gen_location(self.threads[last]["gen"]))
            tid = max(self.runnable, key=lambda t: self._priorities[t])
        elif self.strategy == "rr":
            tid = self.runnable[0]
        else:
            raise ValueError(f"unknown strategy: {self.strategy}")
        self.runnable.remove(tid)
        return tid

    @property
    def schedule_signature(self):
        """本次运行的交错签名（每个决策点选中的线程序列）。"""
        return tuple(self.decisions)


def format_trace(sched, max_events=None):
    """把事件序列格式化为可读文本，可直接据此还原交错。"""
    events = sched.events if max_events is None else sched.events[:max_events]
    lines = []
    for ev in events:
        lines.append(
            f"{ev.seq:>4} clk={ev.clock:<4} {(ev.tid or '--'):>3} "
            f"{ev.kind:<14} {(ev.resource or ''):<12} {ev.detail:<52} @ {ev.location}"
        )
    return "\n".join(lines)
