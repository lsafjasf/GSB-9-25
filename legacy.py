"""重构前的回调式任务提交实现。

仅作为等价性回归测试的基线保留，不再用于新代码。
已知问题（重构动机）：
- 结果与错误通过嵌套回调传递，错误路径散落在多处；
- cancel() 只置标志位，回调仍可能在取消后被触发；
- 句柄释放依赖 worker 正常走到 finally，异常路径上容易遗漏。
"""

import threading


class _LegacyHandle:
    """模拟需要显式释放的句柄/缓冲。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class LegacyTaskRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._finished = False
        self.resources = []

    def submit(self, fn, on_success, on_error):
        """fn() 的结果通过 on_success(value) / on_error(exc) 回调返回。"""

        def run():
            handle = _LegacyHandle()
            self.resources.append(handle)
            try:
                try:
                    value = fn()
                except Exception as exc:  # 错误路径 1：任务体抛异常
                    with self._lock:
                        if self._finished or self._cancelled:
                            return
                        self._finished = True
                    on_error(exc)
                    return
                with self._lock:
                    if self._finished or self._cancelled:
                        return
                    self._finished = True
                on_success(value)
            finally:
                handle.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def cancel(self):
        with self._lock:
            self._cancelled = True
