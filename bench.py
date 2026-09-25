"""GB 级吞吐基准：写入（含 fsync）与逐条校验读取。

运行：python3 bench.py [目标大小MiB，默认 1024]
"""

import os
import sys
import tempfile
import time

from alog import MODE_SKIP, LogReader, LogWriter


def bench(target_mib: int = 1024, record_size: int = 4096):
    target = target_mib * 1024 * 1024
    payload = os.urandom(record_size)
    batch = [payload] * 64                      # 每批 64 条

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bench.log")

        # ---- 写入 ----
        t0 = time.perf_counter()
        written = 0
        records = 0
        with LogWriter(path) as w:
            while written < target:
                ack = w.append_batch(batch)
                written = ack.buffered_end
                records += len(batch)
            w.fsync()
        t_write = time.perf_counter() - t0
        size = os.path.getsize(path)

        # ---- 读取 + 逐条校验（页缓存热数据）----
        t0 = time.perf_counter()
        n = 0
        nbytes = 0
        for off, p in LogReader(path, mode=MODE_SKIP):
            n += 1
            nbytes += len(p)
        t_read = time.perf_counter() - t0

        # ---- 冷读（丢弃页缓存后）----
        try:
            with open(path, "rb") as f:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        t0 = time.perf_counter()
        n2 = 0
        for off, p in LogReader(path, mode=MODE_SKIP):
            n2 += 1
        t_read_cold = time.perf_counter() - t0

    gib = size / (1024 ** 3)
    print(f"文件大小        : {gib:.2f} GiB ({size} 字节)")
    print(f"记录数          : {n} 条 x ~{record_size} B")
    print(f"写入(含fsync)   : {t_write:.2f} s -> {gib / t_write * 1024:.0f} MiB/s")
    print(f"读取校验(热缓存): {t_read:.2f} s -> {gib / t_read * 1024:.0f} MiB/s")
    print(f"读取校验(冷缓存): {t_read_cold:.2f} s -> {gib / t_read_cold * 1024:.0f} MiB/s")
    assert n == records and n2 == records


if __name__ == "__main__":
    mib = int(sys.argv[1]) if len(sys.argv) > 1 else 1024
    bench(mib)
