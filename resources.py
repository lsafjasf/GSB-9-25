"""任务资源：句柄 + 缓冲，以及用于检测泄漏的追踪器。仅标准库。"""
import tempfile
import threading


class ResourceTracker:
    """记录当前存活的资源，测试用它断言“无泄漏”。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._live = []

    def acquire(self, resource):
        with self._lock:
            self._live.append(resource)

    def release(self, resource):
        with self._lock:
            self._live.remove(resource)

    @property
    def live_count(self):
        with self._lock:
            return len(self._live)


class TaskResource:
    """每个任务独占的资源：一个文件句柄 + 一个内存缓冲。"""

    def __init__(self, tracker):
        self._tracker = tracker
        self.handle = tempfile.TemporaryFile()  # 句柄
        self.buffer = bytearray()               # 缓冲
        self.closed = False
        tracker.acquire(self)

    def write(self, data: bytes):
        if self.closed:
            raise ValueError("resource is closed")
        self.buffer.extend(data)
        self.handle.write(data)

    def close(self):
        """幂等释放：句柄关闭、缓冲清空、从追踪器移除。"""
        if not self.closed:
            self.closed = True
            self.handle.close()
            self.buffer.clear()
            self._tracker.release(self)
