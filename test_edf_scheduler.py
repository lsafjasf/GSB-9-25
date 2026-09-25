"""Self-tests for edf_scheduler. Run: python3 -m unittest -v"""

import unittest

from edf_scheduler import (
    CANCELLED,
    CANCELLED_OVERDUE,
    NOT_FOUND,
    Scheduler,
    Task,
)


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def make_scheduler(now=0.0, alerts=None):
    clock = FakeClock(now)
    alert_log = alerts if alerts is not None else []
    sched = Scheduler(clock, on_alert=lambda t: alert_log.append(t.task_id))
    return clock, sched, alert_log


def drain(sched):
    out = []
    while True:
        task = sched.dequeue()
        if task is None:
            return out
        out.append(task.task_id)


class TestOrdering(unittest.TestCase):
    def test_earliest_deadline_first(self):
        _, sched, _ = make_scheduler()
        sched.submit(Task("c", estimated_duration=1, deadline=30))
        sched.submit(Task("a", estimated_duration=1, deadline=10))
        sched.submit(Task("b", estimated_duration=1, deadline=20))
        self.assertEqual(drain(sched), ["a", "b", "c"])

    def test_same_deadline_falls_back_to_submission_order(self):
        _, sched, _ = make_scheduler()
        for tid in ["t1", "t2", "t3", "t4"]:
            sched.submit(Task(tid, estimated_duration=1, deadline=100))
        self.assertEqual(drain(sched), ["t1", "t2", "t3", "t4"])

    def test_no_deadline_tasks_come_last_and_fifo(self):
        _, sched, _ = make_scheduler()
        sched.submit(Task("nd1", estimated_duration=1))
        sched.submit(Task("d1", estimated_duration=1, deadline=50))
        sched.submit(Task("nd2", estimated_duration=1))
        sched.submit(Task("d2", estimated_duration=1, deadline=10))
        sched.submit(Task("nd3", estimated_duration=1))
        self.assertEqual(drain(sched), ["d2", "d1", "nd1", "nd2", "nd3"])

    def test_mixed_same_deadline_and_no_deadline(self):
        _, sched, _ = make_scheduler()
        sched.submit(Task("nd", estimated_duration=1))
        sched.submit(Task("x", estimated_duration=1, deadline=7))
        sched.submit(Task("y", estimated_duration=1, deadline=7))
        sched.submit(Task("z", estimated_duration=1, deadline=3))
        self.assertEqual(drain(sched), ["z", "x", "y", "nd"])


class TestOverdueAndAlerts(unittest.TestCase):
    def test_overdue_task_marked_alerted_once_and_dropped(self):
        clock, sched, alerts = make_scheduler()
        sched.submit(Task("late", estimated_duration=5, deadline=10))
        sched.submit(Task("ok", estimated_duration=1, deadline=100))
        clock.advance(8)  # slack = 2 < duration 5 -> overdue
        first = sched.dequeue()
        self.assertEqual(first.task_id, "ok")  # overdue task not dispatched
        self.assertEqual(alerts, ["late"])
        self.assertEqual(sched.alert_count, 1)
        # Repeated dequeues and clock advances must not re-fire the alert.
        self.assertIsNone(sched.dequeue())
        clock.advance(1000)
        self.assertIsNone(sched.dequeue())
        self.assertEqual(alerts, ["late"])
        self.assertEqual(sched.alert_count, 1)

    def test_task_still_runnable_when_slack_equals_duration(self):
        clock, sched, alerts = make_scheduler()
        sched.submit(Task("edge", estimated_duration=5, deadline=10))
        clock.advance(5)  # slack == duration -> not overdue
        self.assertEqual(sched.dequeue().task_id, "edge")
        self.assertEqual(alerts, [])

    def test_clock_jump_crosses_multiple_tasks_in_one_dequeue_sweep(self):
        clock, sched, alerts = make_scheduler()
        sched.submit(Task("a", estimated_duration=2, deadline=10))
        sched.submit(Task("b", estimated_duration=2, deadline=20))
        sched.submit(Task("c", estimated_duration=2, deadline=30))
        sched.submit(Task("survivor", estimated_duration=1, deadline=10_000))
        clock.advance(100)  # one jump makes a, b, c all overdue
        self.assertEqual(sched.dequeue().task_id, "survivor")
        self.assertEqual(alerts, ["a", "b", "c"])  # EDF order, each once
        self.assertEqual(sched.alert_count, 3)
        clock.advance(1000)  # further time passes: no extra alerts
        self.assertIsNone(sched.dequeue())
        self.assertEqual(sched.alert_count, 3)

    def test_no_deadline_tasks_never_alert(self):
        clock, sched, alerts = make_scheduler()
        sched.submit(Task("nd", estimated_duration=10**9))
        clock.advance(10**9)
        self.assertEqual(sched.dequeue().task_id, "nd")
        self.assertEqual(alerts, [])


class TestCancel(unittest.TestCase):
    def test_cancelled_task_not_dequeued_and_no_alert(self):
        clock, sched, alerts = make_scheduler()
        sched.submit(Task("dead", estimated_duration=1, deadline=5))
        sched.submit(Task("live", estimated_duration=1, deadline=1000))
        self.assertEqual(sched.cancel("dead"), CANCELLED)
        clock.advance(100)  # would be overdue many times over
        self.assertEqual(drain(sched), ["live"])
        self.assertEqual(alerts, [])
        self.assertEqual(sched.alert_count, 0)

    def test_cancel_unknown_task(self):
        _, sched, _ = make_scheduler()
        self.assertEqual(sched.cancel("ghost"), NOT_FOUND)

    def test_cancel_overdue_task_has_deterministic_result(self):
        clock, sched, alerts = make_scheduler()
        task = Task("doomed", estimated_duration=5, deadline=10)
        sched.submit(task)
        clock.advance(8)
        sched.dequeue()  # drops it as overdue, fires the single alert
        self.assertEqual(task.overdue, True)
        self.assertEqual(sched.cancel("doomed"), NOT_FOUND)  # already left queue
        self.assertEqual(sched.alert_count, 1)

    def test_cancel_after_marked_overdue_via_flag(self):
        # A task that became overdue but is still tracked reports CANCELLED_OVERDUE.
        _, sched, _ = make_scheduler()
        task = Task("t", estimated_duration=1, deadline=5)
        sched.submit(task)
        task.overdue = True  # marked overdue by an external monitor
        self.assertEqual(sched.cancel("t"), CANCELLED_OVERDUE)

    def test_cancel_then_no_deadline_fifo_unchanged(self):
        _, sched, _ = make_scheduler()
        for tid in ["n1", "n2", "n3"]:
            sched.submit(Task(tid, estimated_duration=1))
        self.assertEqual(sched.cancel("n2"), CANCELLED)
        self.assertEqual(drain(sched), ["n1", "n3"])


if __name__ == "__main__":
    unittest.main()
