"""Throughput benchmark: build a large log file, then measure verified reads.

Usage: python3 bench.py [size_mib]   (default 1024 MiB)
"""

import os
import sys
import tempfile
import time

import appendlog as al


def build_file(path: str, target_bytes: int) -> int:
    payload = os.urandom(1024)  # 1 KiB records
    batch = [payload] * 1024    # ~1 MiB+ per append_batch call
    written = 0
    with al.LogWriter(path) as w:
        while written < target_bytes:
            ack = w.append_batch(batch)
            written = ack.buffered_upto
        w.sync()
    return written


def main() -> None:
    size_mib = int(sys.argv[1]) if len(sys.argv) > 1 else 1024
    target = size_mib * 1024 * 1024
    tmp = tempfile.mkdtemp(prefix="appendlog-bench-")
    path = os.path.join(tmp, "bench.log")
    try:
        t0 = time.perf_counter()
        total = build_file(path, target)
        t1 = time.perf_counter()
        write_s = t1 - t0

        # Verified sequential read (strict mode, full CRC validation).
        t0 = time.perf_counter()
        count = 0
        bytes_read = 0
        for record in al.LogReader(path, mode=al.STRICT).iter_records():
            count += 1
            bytes_read += al.HEADER_SIZE + len(record.payload)
        t1 = time.perf_counter()
        read_s = t1 - t0

        mib = total / (1024 * 1024)
        print(f"file size      : {mib:.0f} MiB ({total} bytes, {count} records)")
        print(f"write (buffer+sync): {write_s:.2f}s -> {mib / write_s:.0f} MiB/s")
        print(f"read  (mmap+crc32) : {read_s:.2f}s -> {mib / read_s:.0f} MiB/s")
        print(f"records verified : {count}, all CRC32 checks passed")
    finally:
        os.unlink(path)
        os.rmdir(tmp)


if __name__ == "__main__":
    main()
