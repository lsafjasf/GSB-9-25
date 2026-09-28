"""任务提交框架（重构后）：结果对象 + 明确无竞态的取消语义。

状态机：
    PENDING -> RUNNING -> SUCCEEDED | FAILED | CANCELLED
    PENDING -> CANCELLED

取消语义（先到先得，first-CAS-wins）：
- 所有进入终止态的迁移都是同一把锁保护下的单次“检查并设置”，
  谁先完成迁移谁生效，因此“取消”与“正好完成”竞争时结果唯一确定，
  不存在“既成功又取消”的中间态。
- cancel() 返回 True 表示本次调用赢得竞争并使任务进入 CANCELLED；
  返回 False 表示任务已进入终止态（幂等，可安全重复调用）。
- 取消生效后：worker 不再写结果，适配层不再触发成功/失败回调；
  已获取的句柄与缓冲在 finally 中释放，与成功/失败路径完全一致。
"""

from __future__ import annotations

import enum
import threading


class TaskState(enum.Enum):
    PENDING = "pending"        # 未开始
    RUNNING = "running"        # 执行中
    SUCCEEDED = "succeeded"    # 成功
    FAILED = "failed"          # 失败
    CANCELLED = "cancelled"    # 已取消


_TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})


class CancelledError(Exception):
    """对已取消的任务取结果时抛出。"""


class TaskResult:
    """可等待、可查状态、可取错误的结果对象。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._state = TaskState.PENDING
        self._value = None
        self._error = None
        # worker 完成资源清理后置位；独立使用的 TaskResult 默认已置位
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()

    # ---- 内部状态迁移：返回 True 表示本次调用赢得竞争 ----

    def _mark_running(self):
        with self._cond:
            if self._state is not TaskState.PENDING:
                return False
            self._state = TaskState.RUNNING
            return True

    def _settle(self, state, value=None, error=None):
        with self._cond:
            if self._state in _TERMINAL:
                return False
            self._state = state
            self._value = value
            self._error = error
            self._cond.notify_all()
            return True

    # ---- 公共 API ----

    def state(self):
        with self._cond:
            return self._state

    def done(self):
        return self.state() in _TERMINAL

    def cancel(self):
        """请求取消。返回 True 表示本次调用使任务进入 CANCELLED。"""
        return self._settle(TaskState.CANCELLED)

    def wait(self, timeout=None):
        """等待进入终止态；返回是否在超时前完成。"""
        with self._cond:
            if self._state not in _TERMINAL:
                self._cond.wait_for(lambda: self._state in _TERMINAL, timeout)
            return self._state in _TERMINAL

    def join(self, timeout=None):
        """等待进入终止态且 worker 资源清理完成；返回是否在超时前完成。"""
        if not self.wait(timeout):
            return False
        return self._cleanup_done.wait(timeout)

    def result(self, timeout=None):
        """成功返回值；失败抛原异常（类型与消息不丢失）；已取消抛 CancelledError。"""
        if not self.wait(timeout):
            raise TimeoutError("task did not finish in time")
        with self._cond:
            if self._state is TaskState.CANCELLED:
                raise CancelledError("task was cancelled")
            if self._state is TaskState.FAILED:
                raise self._error
            return self._value

    def error(self, timeout=None):
        """失败时返回原异常对象，否则返回 None。"""
        if not self.wait(timeout):
            raise TimeoutError("task did not finish in time")
        with self._cond:
            return self._error


class TaskContext:
    """传给任务体的上下文，支持任务内自取消与取消感知。"""

    def __init__(self, result):
        self._result = result

    def cancel(self):
        return self._result.cancel()

    @property
    def cancelled(self):
        return self._result.state() is TaskState.CANCELLED


class _Resource:
    """模拟需要显式释放的句柄/缓冲。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TaskRunner:
    """提交任务并管理其生命周期与资源。"""

    def __init__(self, start_gate=None):
        # start_gate：仅供测试使用，用于确定性地观察 PENDING 状态
        # 以及“取消发生在任务开始前”的时序。
        self._start_gate = start_gate
        self.resources = []
        self._resources_lock = threading.Lock()

    def _acquire(self):
        resource = _Resource()
        with self._resources_lock:
            self.resources.append(resource)
        return resource

    def submit(self, fn):
        """fn: callable(ctx) -> value。返回 TaskResult。"""
        result = TaskResult()
        result._cleanup_done.clear()

        def run():
            if self._start_gate is not None:
                self._start_gate.wait()
            if not result._mark_running():
                # 开始前已取消：不获取任何资源，但同样要留下清理终态，
                # 否则 join() 的等待方会永久挂起。
                result._cleanup_done.set()
                return
            handle = self._acquire()
            try:
                value = fn(TaskContext(result))
            except Exception as exc:
                # 与取消竞争时先到先得：若取消已生效，错误被丢弃
                result._settle(TaskState.FAILED, error=exc)
            else:
                result._settle(TaskState.SUCCEEDED, value=value)
            finally:
                handle.close()  # 成功/失败/取消三条路径统一释放
                result._cleanup_done.set()

        threading.Thread(target=run, daemon=True).start()
        return result

    # ---- 适配层：保留原回调式接口 ----

    def submit_callback(self, fn, on_success=None, on_error=None, on_cancel=None):
        """与旧接口等价的回调式提交。

        保证：三个回调中恰好有一个被调用一次；取消生效后
        不会再触发 on_success / on_error。
        """
        result = self.submit(fn)

        def watch():
            result.wait()
            state = result.state()
            if state is TaskState.SUCCEEDED:
                if on_success is not None:
                    on_success(result.result())
            elif state is TaskState.FAILED:
                if on_error is not None:
                    on_error(result.error())
            elif on_cancel is not None:
                on_cancel()

        threading.Thread(target=watch, daemon=True).start()
        return result
