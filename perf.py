"""Benchmark: redact ~1M characters, report wall time and peak memory.

Run: python3 perf.py [target_chars]
"""

import json
import sys
import time
import tracemalloc

from redactor import Redactor, load_rules

PARAGRAPH = (
    "客户张三的手机号是13800138000，备用邮箱 zhangsan@example.com，"
    "身份证号 11010119900307771X。订单号 20260925000123，备注：无敏感信息。"
    "另一位客户李四使用 13912345678 与 lisi.work@mail-example.org 联系。"
    "普通文本填充，不包含任何需要脱敏的内容，用于模拟真实语料分布。\n"
)


def build_corpus(target: int) -> str:
    reps = target // len(PARAGRAPH) + 1
    return PARAGRAPH * reps


def main() -> None:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    text = build_corpus(target)
    rules = load_rules("rules.sample.json")
    redactor = Redactor(rules, key="perf-benchmark-key")

    tracemalloc.start()
    t0 = time.perf_counter()
    output, report = redactor.redact(text)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result = {
        "input_chars": len(text),
        "output_chars": len(output),
        "total_replacements": report["summary"]["total_replacements"],
        "total_shadowed": report["summary"]["total_shadowed"],
        "wall_time_seconds": round(elapsed, 3),
        "peak_memory_mb": round(peak / 1024 / 1024, 1),
        "chars_per_second": int(len(text) / elapsed),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
