"""重构前的回调式任务提交实现（保留为回归基线，不要修改）。

已知问题（即重构动机）：
- 结果与错误经嵌套回调传递，错误路径散落在 run() 的多处；
- 取消之后回调仍可能被触发（run 内不检查取消标志）；
- 取消路径上资源（句柄/缓冲）不释放，造成泄漏。
"""
import threading

from resources import ResourceTracker, TaskResource

legacy_tracker = ResourceTracker()


def submit_task(fn, callback):
    """提交任务，完成或出错时调用 callback(error, result)。返回 cancel()。"""
    cancelled = {"flag": False}
    resource = TaskResource(legacy_tracker)

    def run():
        try:
            result = fn(resource)
            # 问题 1：不检查取消标志，取消后仍会触发回调
            callback(None, result)
        except Exception as exc:  # 问题 2：错误路径散落
            callback(exc, None)
        finally:
            # 问题 3：一旦取消，资源不释放 -> 泄漏
            if not cancelled["flag"]:
                resource.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def cancel():
        cancelled["flag"] = True

    return cancel
