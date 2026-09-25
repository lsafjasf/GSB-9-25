"""百万行性能基准：python3 bench.py [n_lines]

流式生成 n 行合成日志（约 40 种模板形状），统计耗时与内存。
"""
import resource
import random
import sys
import time
import tracemalloc

from logtpl import cluster_lines

SHAPES = [
    '{ts} INFO http {ip}:8080 "GET /api/users/{uid}" 200 {dur}ms',
    '{ts} INFO http {ip}:8080 "POST /api/orders/{oid}" 201 {dur}ms',
    '{ts} WARN http {ip}:8080 "GET /static/img/{uid}.png" 304 {dur}ms',
    '{ts} ERROR db query failed after {dur}s host=db-{h:02d} retries={r}',
    '{ts} ERROR cache miss key=user:{uid} ttl={dur}s',
    'job {uuid} finished in {dur}ms exit={r}',
    'alloc at 0x{addr:x} size={size}',
    'deploy version v{v1}.{v2}.{v3} to /opt/app/releases/v{v1}.{v2}.{v3}',
    'user=u{uid} action=login ip={ip} status=ok',
    'user=u{uid} action=purchase ip={ip} status=ok amount={amt}',
    'healthcheck ok',
    'metrics cpu={amt} mem={amt} disk={amt} host=node-{h}',
    '{ts} DEBUG gc pause {dur}ms heap={size}',
    'session {uuid} expired after {dur}s',
    'request trace_id={uuid} span={addr:x} took {dur}ms',
    '{ts} INFO kafka consumer lag={lag} partition={r} topic=events',
    'upload /data/files/{uid}/part-{r}.bin completed {size} bytes',
    '{ts} WARN retry {r}/5 for https://api.internal.example.com/v2/jobs/{oid}',
    'auth token {hex} revoked for user=u{uid}',
    '{ts} ERROR timeout after {dur}ms connecting to {ip}:5432',
] * 2  # 40 种形状


def gen_lines(n, seed=42):
    rng = random.Random(seed)
    for i in range(n):
        shape = SHAPES[i % len(SHAPES)]
        yield shape.format(
            ts='2026-09-25T10:%02d:%02dZ' % (rng.randrange(60), rng.randrange(60)),
            ip='10.%d.%d.%d' % (rng.randrange(256), rng.randrange(256), rng.randrange(256)),
            uid=rng.randrange(10**6), oid=rng.randrange(10**8),
            dur=rng.randrange(1, 5000), h=rng.randrange(32), r=rng.randrange(10),
            uuid='%08x-%04x-%04x-%04x-%012x' % tuple(rng.randrange(16**k) for k in (8, 4, 4, 4, 12)),
            addr=rng.randrange(16**12), size=rng.randrange(1, 1 << 20),
            v1=rng.randrange(5), v2=rng.randrange(20), v3=rng.randrange(50),
            amt=round(rng.uniform(0, 100), 2), lag=rng.randrange(10**5),
            hex='%032x' % rng.randrange(16**32),
        )


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    tracemalloc.start()
    t0 = time.perf_counter()
    result = cluster_lines(gen_lines(n))
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print('lines            : %d' % result.total)
    print('templates        : %d' % len(result.clusters))
    print('coverage         : %.2f%%' % (result.coverage * 100))
    print('avg placeholders : %.2f' % result.avg_placeholders)
    print('unclustered      : %d' % sum(c for _, c in result.unclustered))
    print('wall time        : %.2f s' % elapsed)
    print('throughput       : %.0f lines/s' % (n / elapsed))
    print('peak mem (alloc) : %.1f MB' % (peak / 1e6))
    print('max RSS          : %.1f MB' % (maxrss_kb / 1024))


if __name__ == '__main__':
    main()
