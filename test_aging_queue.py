import random
import unittest

from aging_queue import AgingPriorityQueue, Empty


class AgingPriorityQueueTest(unittest.TestCase):
    def test_static_priority_with_fifo_ties(self):
        queue = AgingPriorityQueue(aging_period=None)
        queue.enqueue("low-1", 2)
        queue.enqueue("high-1", 0)
        queue.enqueue("high-2", 0)
        queue.enqueue("mid-1", 1)
        queue.enqueue("low-2", 2)

        self.assertEqual(
            [queue.dequeue().item for _ in range(5)],
            ["high-1", "high-2", "mid-1", "low-1", "low-2"],
        )

    def test_aging_promotes_a_waiting_low_priority_task(self):
        queue = AgingPriorityQueue(aging_period=2)
        queue.enqueue("low", 2)
        for name in ("high-1", "high-2", "high-3", "high-4"):
            queue.enqueue(name, 0)

        results = [queue.dequeue() for _ in range(5)]
        self.assertEqual(
            [result.item for result in results],
            ["high-1", "high-2", "high-3", "high-4", "low"],
        )
        self.assertEqual(results[-1].effective_priority, 0)
        self.assertEqual(results[-1].waited_dequeues, 5)

    def test_same_effective_priority_always_uses_arrival_order(self):
        queue = AgingPriorityQueue(aging_period=1)

        queue.enqueue("service", 0)
        queue.enqueue("older", 3)
        self.assertEqual(queue.dequeue().item, "service")

        queue.enqueue("newer", 2)
        self.assertEqual(queue.dequeue().item, "older")
        self.assertEqual(queue.dequeue().item, "newer")

    def test_promoted_task_keeps_global_fifo_order(self):
        queue = AgingPriorityQueue(aging_period=1)
        queue.enqueue("service", 0)
        queue.enqueue("older", 2)
        self.assertEqual(queue.dequeue().item, "service")

        queue.enqueue("newer", 1)
        self.assertEqual(queue.dequeue().item, "older")
        self.assertEqual(queue.dequeue().item, "newer")

    def test_no_aging_keeps_static_priority(self):
        queue = AgingPriorityQueue(aging_period=None)
        queue.enqueue("low", 2)
        queue.enqueue("high-1", 0)
        queue.enqueue("high-2", 0)

        results = [queue.dequeue() for _ in range(3)]
        self.assertEqual([r.item for r in results], ["high-1", "high-2", "low"])
        self.assertEqual(results[-1].effective_priority, 2)

    def test_empty_queue_reports_a_clear_error(self):
        queue = AgingPriorityQueue()
        with self.assertRaises(Empty):
            queue.dequeue()

    def test_invalid_configuration_and_priority_are_rejected(self):
        with self.assertRaises(ValueError):
            AgingPriorityQueue(aging_period=0)
        queue = AgingPriorityQueue()
        with self.assertRaises(ValueError):
            queue.enqueue("bad", -1)

    def test_waiting_time_is_deterministic_and_bounded(self):
        for seed in range(20):
            queue = AgingPriorityQueue(aging_period=4)
            rng = random.Random(seed)
            observed = []
            max_size = 0
            task_id = 0
            aging_period = 4

            for _ in range(3000):
                if queue.empty() or rng.random() < 0.55:
                    priority = rng.randrange(5)
                    queue.enqueue(task_id, priority)
                    task_id += 1
                    max_size = max(max_size, queue.qsize())
                else:
                    result = queue.dequeue()
                    observed.append(result)

            while not queue.empty():
                result = queue.dequeue()
                observed.append(result)

            self.assertEqual(queue.qsize(), 0)
            for result in observed:
                bound = result.priority * aging_period + max_size
                self.assertLessEqual(result.waited_dequeues, bound)

    def test_same_inputs_produce_same_outputs(self):
        def run():
            queue = AgingPriorityQueue(aging_period=3)
            rng = random.Random(123456)
            task_id = 0
            output = []
            for _ in range(1000):
                if queue.empty() or rng.random() < 0.6:
                    queue.enqueue(task_id, rng.randrange(4))
                    task_id += 1
                else:
                    result = queue.dequeue()
                    output.append(
                        (
                            result.sequence,
                            result.priority,
                            result.effective_priority,
                            result.waited_dequeues,
                        )
                    )
            while not queue.empty():
                output.append(queue.dequeue().sequence)
            return output

        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
