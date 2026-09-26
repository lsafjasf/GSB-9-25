"""Benchmark: save/load timing for large session states + checksum overhead.

Run:  python3 bench.py
"""

import hashlib
import json
import os
import platform
import shutil
import tempfile
import time

import snapshot

REPEATS = 3


def make_state(target_bytes):
    """Build a realistic chat-history state of roughly ``target_bytes``."""
    item = {
        "role": "assistant",
        "content": "lorem ipsum dolor sit amet " * 20,  # ~540 chars
        "tool_calls": [{"name": "read", "args": {"path": "/tmp/x"}}],
        "ts": 1700000000,
    }
    item_size = len(json.dumps(item, ensure_ascii=False))
    count = max(1, target_bytes // item_size)
    return {"history": [dict(item, i=i) for i in range(count)],
            "meta": {"tags": [], "schema": 3}}


def timeit(fn, repeats=REPEATS):
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - start)
    return best, result


def main():
    print(f"Python {platform.python_version()} on {platform.machine()}, "
          f"best of {REPEATS} runs")
    header = (f"{'payload':>10} | {'save':>9} | {'load':>9} | "
              f"{'sha256':>9} | {'json dumps':>10} | {'json loads':>10} | "
              f"{'checksum % of save':>18}")
    print(header)
    print("-" * len(header))

    tmpdir = tempfile.mkdtemp(prefix="snapbench-")
    try:
        for target in (1 << 20, 16 << 20, 32 << 20):
            state = make_state(target)
            path = os.path.join(tmpdir, f"bench-{target}.snap")

            save_t, _ = timeit(lambda: snapshot.save(path, state))
            size = os.path.getsize(path)
            load_t, loaded = timeit(lambda: snapshot.load(path))
            assert loaded == state

            payload = snapshot._serialize(state)
            hash_t, _ = timeit(lambda: hashlib.sha256(payload).hexdigest())
            dumps_t, _ = timeit(lambda: snapshot._serialize(state))
            loads_t, _ = timeit(lambda: json.loads(payload.decode("utf-8")))

            mib = size / (1 << 20)
            print(f"{mib:>8.1f}M | {save_t*1e3:>7.1f}ms | {load_t*1e3:>7.1f}ms | "
                  f"{hash_t*1e3:>7.1f}ms | {dumps_t*1e3:>8.1f}ms | "
                  f"{loads_t*1e3:>8.1f}ms | {hash_t/save_t*100:>17.1f}%")
            os.unlink(path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print("Notes:")
    print("- 'save' = serialize + sha256 + temp-file write + fsync + os.replace.")
    print("- 'load' = read + sha256 verify + JSON parse + upgrade check.")
    print("- 'sha256' alone = cost of the integrity pass over the payload;")
    print("  it is the only checksum work, so 'checksum % of save' bounds its")
    print("  share of a full atomic save (JSON serialization dominates).")


if __name__ == "__main__":
    main()
