"""百万行日志性能基准：python3 bench.py [行数，默认 1000000]"""
import os
import random
import resource
import sys
import time
import tracemalloc

from logtpl import Clusterer

TEMPLATES = [
    'user=u{u} logged in from 10.{a}.{b}.{c} in {d}ms',
    'GET /api/v1/items/{i} 200 {d}ms',
    'POST /api/v1/orders {i} 201 {d}ms',
    'worker=w{w} job=job-{h} done',
    'msg="user \\"u{u}\\" timed out" level=WARN',
    '{{"user":"u{u}","id":{i},"ok":true}}',
    'txn {h} failed after {r} retries',
    'req id={u}-{u}4{u}-8{u}-{u} path=/var/log/app.{i}.log',
    'cache hit key=k{i} ttl={d}s',
    '10.{a}.{b}.{c}:8080 -> 192.168.{a}.{c}:443',
    'session {h} opened by u{u}',
    'disk /dev/sda{w} usage at {d}% threshold=90%',
    'a={i} b={u} c={d}',
    'c={d} b={u} a={i}',
    'slow query took {d}ms on table t{w}',
]


def gen_lines(n, seed=42):
    rng = random.Random(seed)
    for k in range(n):
        t = TEMPLATES[k % len(TEMPLATES)]
        yield t.format(u=rng.randint(1, 10**6), i=rng.randint(1, 10**8),
                       a=rng.randint(0, 255), b=rng.randint(0, 255),
                       c=rng.randint(1, 254), d=rng.randint(1, 9999),
                       w=rng.randint(0, 8), h='%08x' % rng.randint(0, 2**32 - 1),
                       r=rng.randint(1, 5))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    c = Clusterer()
    tracemalloc.start()
    t0 = time.perf_counter()
    for line in gen_lines(n):
        c.add(line)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rep = c.report()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print('lines            : %d' % n)
    print('elapsed          : %.2f s  (%.0f lines/s)' % (dt, n / dt))
    print('python peak mem  : %.1f MB (tracemalloc)' % (peak / 1e6))
    print('process max RSS  : %.1f MB (ru_maxrss)' % (rss_kb / 1024))
    print('templates        : %d valid, %d total'
          % (len(rep.templates), rep.num_templates_total))
    print('coverage         : %.2f%%' % (rep.coverage * 100))
    print('avg placeholders : %.2f' % rep.avg_placeholders)
    print('unclustered      : %d' % len(rep.unclustered))
    print('reorder groups   : %d' % len(rep.reorder_groups))
    print('python           : %s' % sys.version.split()[0])
    print('machine          : %d CPUs, %s' % (os.cpu_count(), os.uname().machine))


if __name__ == '__main__':
    main()
