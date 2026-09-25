"""重构后的任务提交：结果对象 + 明确无竞态的取消语义 + 旧回调接口适配层。

取消语义（先到先得，first-wins）：
- 所有状态迁移共用同一把条件锁，只有第一次进入终态的迁移生效；
- cancel() 与任务完成/失败竞争时，谁先拿到锁谁决定终态，结果确定；
- 取消生效后：不再写结果、不再触发回调；资源在 finally 中一律释放。
"""
import threading
from enum import Enum

from resources import ResourceTracker, TaskResource


class TaskState(Enum):
    PENDING = "pending"        # 未开始
    RUNNING = "running"        # 执行中
    SUCCESS = "success"        # 成功
    FAILED = "failed"          # 失败
    CANCELLED = "cancelled"    # 已取消


_TERMINAL = (TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED)


class CancelledError(Exception):
    """任务被取消（含任务内协作式取消）。"""


class TaskResult:
    """结果对象：可等待、可取状态、可取错误。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._state = TaskState.PENDING
        self._result = None
        self._error = None

    # ---- 内部：状态迁移（先到先得，仅第一次终态迁移生效）----
    def _transition(self, target, result=None, error=None):
        with self._cond:
            if self._state in _TERMINAL:
                return False
            self._state = target
            self._result = result
            self._error = error
            self._cond.notify_all()
            return True

    def _mark_running(self):
        with self._cond:
            if self._state is TaskState.PENDING:
                self._state = TaskState.RUNNING
                self._cond.notify_all()
                return True
            return False  # 启动前已被取消

    # ---- 对外接口 ----
    @property
    def state(self):
        with self._cond:
            return self._state

    def done(self):
        return self.state in _TERMINAL

    def wait(self, timeout=None):
        """等待进入终态；返回是否已结束（超时返回 False）。"""
        with self._cond:
            if self._state not in _TERMINAL:
                self._cond.wait_for(lambda: self._state in _TERMINAL, timeout)
            return self._state in _TERMINAL

    def result(self, timeout=None):
        """等待并返回结果；失败抛原异常（类型与消息不变），取消抛 CancelledError。"""
        if not self.wait(timeout):
            raise TimeoutError("task did not finish in time")
        if self._state is TaskState.FAILED:
            raise self._error
        if self._state is TaskState.CANCELLED:
            raise self._error
        return self._result

    @property
    def error(self):
        """终态时的错误对象（成功为 None）；类型与消息和任务内抛出的一致。"""
        with self._cond:
            return self._error


class CancelToken:
    """协作式取消令牌：任务内可查询、可自取消。"""

    def __init__(self):
        self._event = threading.Event()
        self._on_cancel = None  # 由 submit 注入，指向 handle.cancel

    def _set(self):
        self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def throw_if_cancelled(self):
        if self.cancelled:
            raise CancelledError("task cancelled")

    def cancel(self):
        """任务内自取消：等价于外部调用 handle.cancel()。"""
        if self._on_cancel is not None:
            self._on_cancel()


class TaskHandle:
    """提交后返回的句柄：持有结果对象，提供幂等取消。"""

    def __init__(self, result, token):
        self.result_object = result
        self._token = token
        self._finished = threading.Event()

    def cancel(self):
        """先到先得：成功迁移到 CANCELLED 返回 True；任务已进入终态返回 False。幂等。"""
        won = self.result_object._transition(
            TaskState.CANCELLED, error=CancelledError("task cancelled")
        )
        self._token._set()  # 无论输赢都置标志，通知协作式取消
        return won

    def join(self, timeout=None):
        """等待工作线程完全退出（含资源释放）；返回是否已结束。"""
        return self._finished.wait(timeout)


def submit(fn, callback=None, tracker=None, start_gate=None):
    """提交任务。fn(resource, token) -> value。

    - 返回 TaskHandle，handle.result_object 为 TaskResult；
    - callback(error, result) 为可选的旧式回调：仅在任务成功/失败且
      该终态迁移生效时触发一次；取消绝不触发回调；
    - start_gate：可选 threading.Event，用于测试确定地观察 PENDING 状态。
    """
    tracker = tracker if tracker is not None else ResourceTracker()
    result = TaskResult()
    token = CancelToken()
    handle = TaskHandle(result, token)
    token._on_cancel = handle.cancel

    def run():
        resource = TaskResource(tracker)
        target = None
        value = None
        error = None
        try:
            if start_gate is not None:
                start_gate.wait()
            if result._mark_running():  # 启动前已取消则跳过执行
                value = fn(resource, token)
                target = TaskState.SUCCESS
        except CancelledError as exc:
            # 协作式/自取消：视为取消而非失败
            error = exc
            target = TaskState.CANCELLED
        except BaseException as exc:
            error = exc
            target = TaskState.FAILED
        finally:
            resource.close()  # 成功/失败/取消一律先释放句柄与缓冲
        # 资源释放后再提交终态：wait() 返回时成功/失败路径资源必已释放
        committed = result._transition(target, result=value, error=error) if target else False
        # 回调只在“完成/失败”赢得迁移时触发；取消后绝不触发
        if callback is not None and committed:
            if target is TaskState.SUCCESS:
                callback(None, value)
            elif target is TaskState.FAILED:
                callback(error, None)
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    finished = threading.Event()
    handle._finished = finished
    thread.start()
    return handle


def submit_with_callback(fn, callback):
    """旧回调接口的适配层：fn(resource) -> value，callback(error, result)。

    返回 cancel()，签名与 legacy_task.submit_task 完全一致；
    错误对象的类型与消息原样透传，不包装、不丢失。
    """
    handle = submit(lambda resource, token: fn(resource), callback)
    return handle.cancel
