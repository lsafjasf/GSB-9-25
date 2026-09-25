"""bench_pathological.py — 病态模式与极长输入的实测耗时。

对比 本引擎(Pike VM, O(m*n)) 与 Python re(回溯法)。
re 侧用 signal.alarm 限时，超时记为 ">LIMIT"。

运行: python3 bench_pathological.py
"""
import re
import signal
import time

import regex_engine as E

LIMIT = 3  # 单次 re 运行的秒数上限


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def time_re(pattern, text):
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, LIMIT)
    try:
        t0 = time.perf_counter()
        re.search(pattern, text)
        return time.perf_counter() - t0
    except _Timeout:
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def time_engine(pattern, text):
    prog = E.compile(pattern)
    t0 = time.perf_counter()
    prog.search(text)
    return time.perf_counter() - t0


def fmt(t):
    return "%8.4fs" % t if t is not None else ">%ds(超时)" % LIMIT


def main():
    print("病态模式 1: 'a*'*k + 'b'  匹配  'a'*n （回溯法需枚举 C(n+k-1, k-1) 种切分）")
    print("%-8s %-10s %14s %14s" % ("k", "n", "re(回溯)", "本引擎"))
    for k, n in [(8, 22), (8, 26), (8, 30), (10, 30), (20, 100_000), (30, 1_000_000)]:
        pat = "a*" * k + "b"
        text = "a" * n
        t_re = time_re(pat, text) if n <= 10_000 else None
        t_en = time_engine(pat, text)
        print("%-8d %-10d %14s %14s" % (k, n, fmt(t_re) if n <= 10_000 else "(跳过)", fmt(t_en)))

    print()
    print("病态模式 2: 'a?'*k + 'a'*k  匹配  'a'*k （经典指数回溯）")
    print("%-8s %14s %14s" % ("k", "re(回溯)", "本引擎"))
    for k in (20, 23, 26, 29, 2000):
        pat = "a?" * k + "a" * k
        text = "a" * k
        t_re = time_re(pat, text) if k <= 100 else None
        t_en = time_engine(pat, text)
        print("%-8d %14s %14s" % (k, fmt(t_re) if k <= 100 else "(跳过)", fmt(t_en)))

    print()
    print("极长输入: '[a-z]+[0-9]*'  扫描  'ab'*n/2")
    print("%-12s %14s %14s" % ("n", "re(回溯)", "本引擎"))
    for n in (100_000, 1_000_000, 4_000_000):
        text = "ab" * (n // 2)
        t_re = time_re("[a-z]+[0-9]*", text)
        t_en = time_engine("[a-z]+[0-9]*", text)
        print("%-12d %14s %14s" % (n, fmt(t_re), fmt(t_en)))


if __name__ == "__main__":
    main()
