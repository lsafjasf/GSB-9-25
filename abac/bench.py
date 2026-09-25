"""Synthetic rule-set generator and latency benchmark."""

import random
import statistics
import time

from .engine import Policy

RULES_PER_BUCKET = 20


def index_dims(count):
    """Pick action/type cardinalities so buckets stay ~RULES_PER_BUCKET big.

    Real rule sets grow new actions and resource types as they grow; this
    mirrors that so the benchmark measures steady-state lookup cost.
    """
    n_actions = max(1, int((count / RULES_PER_BUCKET) ** 0.5))
    n_types = max(1, count // RULES_PER_BUCKET // n_actions)
    return n_actions, n_types


def generate_rules(count, seed=42):
    """Generate `count` conflict-free rules spread over the index buckets."""
    n_actions, n_types = index_dims(count)
    rng = random.Random(seed)
    rules = []
    for i in range(count):
        rules.append({
            "id": "r%d" % i,
            "effect": "deny" if i % 7 == 0 else "allow",
            "priority": rng.randint(0, 100),
            "conditions": {
                "action": "action_%d" % (i % n_actions),
                "subject": {
                    "role": "role_%d" % (i % 5),
                    # unique threshold per rule keeps condition sets distinct,
                    # so the generated set is conflict-free by construction
                    "clearance": {"op": "ge", "value": i},
                },
                "resource": {
                    "type": "type_%d" % ((i // n_actions) % n_types),
                    "classification": {"op": "le", "value": rng.randint(0, 10)},
                },
                "environment": {"hour": {"op": "between", "value": [0, 24]}},
            },
        })
    return rules


def generate_request(i, n_actions, n_types):
    return {
        "action": "action_%d" % (i % n_actions),
        "subject": {"role": "role_%d" % (i % 5), "clearance": 10 ** 9},
        "resource": {"type": "type_%d" % (i % n_types), "classification": 0},
        "environment": {"hour": 12},
    }


def run_bench(sizes, n_requests, on_missing_attribute="skip"):
    rows = []
    for size in sizes:
        policy = Policy.from_json(generate_rules(size),
                                  on_missing_attribute=on_missing_attribute)
        n_actions, n_types = index_dims(size)
        requests = [generate_request(i, n_actions, n_types)
                    for i in range(n_requests)]
        for req in requests[:100]:  # warmup
            policy.decide(req, explain=False)
        samples = []
        for req in requests:
            start = time.perf_counter()
            policy.decide(req, explain=False)
            samples.append((time.perf_counter() - start) * 1e6)
        samples.sort()
        rows.append({
            "rules": size,
            "requests": n_requests,
            "avg_us": statistics.fmean(samples),
            "p50_us": samples[len(samples) // 2],
            "p95_us": samples[int(len(samples) * 0.95)],
            "max_us": samples[-1],
        })
    return rows
