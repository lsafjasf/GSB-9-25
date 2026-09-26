"""最早截止优先（EDF）调度器。

仅依赖标准库；时间一律由外部注入的 Clock 提供，方便在测试中确定性地
推进与跳跃时间。
"""

from .model import Alert, Clock, FakeClock, Status, Task
from .scheduler import DequeueResult, Scheduler, tolerable_wait

__all__ = [
    "Alert",
    "Clock",
    "DequeueResult",
    "FakeClock",
    "Scheduler",
    "Status",
    "Task",
    "tolerable_wait",
]
