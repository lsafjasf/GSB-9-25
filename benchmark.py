"""大状态快照写入/恢复性能基准（仅标准库）。

运行：python3 benchmark.py [大小MiB] [轮数]
例：  python3 benchmark.py 50 5
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import platform
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snapshot_lib import load_state, save_state  # noqa: E402
from snapshot_lib.snapshot import _canonical_json  # noqa: E402


def _timed(fn, rounds):
    times = []
    result = None
    for _ in range(rounds):
        gc.collect()
        start = time.perf_counter()
        result = fn()
        if hasattr(os, "sync"):
            pass  # 不做全局 sync，避免污染单次计时
        times.append(time.perf_counter() - start)
    return min(times), sum(times) / len(times), result


def main(size_mib=50, rounds=5):
    payload = "测" * (size_mib * 1024 * 1024 // 3)
    state = {"session": {"messages": [{"role": "user", "content": payload}]}, "notes": []}

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "big.snap")

        # 先编码一次拿到实际字节数（canonical JSON + UTF-8 后的快照大小）。
        encoded = None
        from snapshot_lib import encode_snapshot

        encoded = encode_snapshot(state)
        snap_bytes = len(encoded)

        # 纯校验开销（对已编码字节算 sha256），便于说明 checksum 成本。
        def hash_only():
            return hashlib.sha256(encoded).hexdigest()

        # 纯 JSON 序列化开销（save 的组成部分之一）。
        def dump_only():
            return _canonical_json(state)

        best_hash, avg_hash, _ = _timed(hash_only, rounds)
        best_dump, avg_dump, _ = _timed(dump_only, rounds)
        best_save, avg_save, _ = _timed(lambda: save_state(path, state), rounds)
        best_load, avg_load, snap = _timed(lambda: load_state(path), rounds)

        # 正确性确认
        assert snap.state == state

        mib = 1024 * 1024

        def line(name, best, avg, nbytes):
            return (
                f"{name:<26} best={best*1000:8.1f} ms  avg={avg*1000:8.1f} ms"
                f"  throughput={nbytes/best/mib:7.1f} MiB/s(best)"
            )

        print("=" * 78)
        print(f"Python {platform.python_version()} on {platform.platform()}")
        fs = os.statvfs(d)
        print(
            f"state logical size ≈ {len(payload.encode('utf-8'))/mib:.1f} MiB, "
            f"snapshot file = {snap_bytes/mib:.2f} MiB, rounds={rounds} (取 best/avg)"
        )
        print(f"tmp fs block size={fs.f_bsize} bytes, cwd={d}")
        print("=" * 78)
        print(line("sha256 over snapshot", best_hash, avg_hash, snap_bytes))
        print(line("canonical json dumps", best_dump, avg_dump, snap_bytes))
        print(line("atomic save (含fsync)", best_save, avg_save, snap_bytes))
        print(line("load (读+校验+JSON)", best_load, avg_load, snap_bytes))
        print("-" * 78)
        print(
            f"校验占 save 比 ≈ {best_hash/best_save*100:.0f}% (best/best), "
            f"占 load 比 ≈ {best_hash/best_load*100:.0f}% (best/best)"
        )
        print(
            "注：save 含一次 sha256（编码时）+ 写文件 fsync + 目录 fsync；"
            "load 含一次 sha256 + JSON 解析；均为单遍流式哈希量级。"
        )


if __name__ == "__main__":
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(size, rounds)
