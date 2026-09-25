"""病态模式与极长输入的耗时实测。

用法: python3 benchmark.py [--with-re]
  --with-re  额外对参照实现 re 跑小规模对照（re 是回溯实现，病态规模下会指数爆炸，
             因此只跑到安全规模为止）。
"""
import sys
import time

import regex_engine as engine


def bench(label, pattern, text, expect=None):
    start = time.perf_counter()
    result = engine.compile(pattern).search(text)
    elapsed = time.perf_counter() - start
    note = '' if expect is None or result == expect else '  <-- 结果与预期不符!'
    print(f'  {label:<46} {elapsed * 1000:>9.2f} ms  -> {result}{note}')
    return elapsed


def bench_re(label, pattern, text):
    import re
    start = time.perf_counter()
    result = re.search(pattern, text) is not None
    elapsed = time.perf_counter() - start
    print(f'  {label:<46} {elapsed * 1000:>9.2f} ms  -> {result}')


def main():
    with_re = '--with-re' in sys.argv

    print('== 病态模式 1: a?^n a^n 对 a^n（经典指数回溯陷阱）==')
    for n in (25, 50, 100, 200, 400, 800):
        bench(f'n={n}', 'a?' * n + 'a' * n, 'a' * n, expect=True)
    if with_re:
        for n in (20, 22, 24):
            bench_re(f'[re 对照] n={n}', 'a?' * n + 'a' * n, 'a' * n)

    print('== 病态模式 2: 多重 .* 嵌套前缀, 长失配输入 ==')
    for n in (1000, 5000, 20000):
        bench(f'".*.*.*.*b" 对 a*{n}', '.*.*.*.*b', 'a' * n, expect=False)

    print('== 病态模式 3: 交替量词链 (a*a*)* 风格的无组版本 ==')
    for n in (1000, 5000, 20000):
        bench(f'"a*a*a*a*a*a*b" 对 a*{n}', 'a*a*a*a*a*a*b', 'a' * n, expect=False)

    print('== 极长输入 ==')
    bench('"[0-9]+x" 对 a*1_000_000（失配）', '[0-9]+x', 'a' * 1_000_000, expect=False)
    bench('"a+b" 对 a*999_999 + b（末尾命中）', 'a+b', 'a' * 999_999 + 'b', expect=True)
    bench('"^a+$" 对 a*1_000_000（锚点全程）', '^a+$', 'a' * 1_000_000, expect=True)
    bench('"x?y?z?" 对 a*1_000_000（空匹配即中）', 'x?y?z?', 'a' * 1_000_000, expect=True)


if __name__ == '__main__':
    main()
