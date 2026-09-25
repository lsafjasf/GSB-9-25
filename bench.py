"""displaywrap 性能与内存基准。运行：python3 bench.py"""

import random
import time
import tracemalloc

from displaywrap import Wrapper, display_width, truncate, wrap

ATOMS = ["New York", "👨‍👩‍👧‍👦", "不可拆片段"]


def gen_text(n, seed=20260925):
    rnd = random.Random(seed)
    words = [
        "hello", "world", "display", "width", "wrap", "truncate",
        "你好", "世界", "终端", "排版", "表格", "混排",
        "😀", "🎉", "👨‍👩‍👧", "🇨🇳", "é", "a​b",
        "New York", "supercalifragilistic", "xylophone",
    ]
    parts = []
    total = 0
    while total < n:
        w = rnd.choice(words)
        parts.append(w)
        total += len(w) + 1
        if rnd.random() < 0.04:
            parts.append("\n")
        elif rnd.random() < 0.03:
            parts.append("\t")
        else:
            parts.append(" ")
    return "".join(parts)


def bench_once(text, width):
    t0 = time.perf_counter()
    lines = wrap(text, width, atoms=ATOMS)
    dt = time.perf_counter() - t0
    return dt, lines


def bench_stream(text, width, chunk=4096):
    w = Wrapper(width, atoms=ATOMS)
    out = []
    t0 = time.perf_counter()
    for i in range(0, len(text), chunk):
        out.extend(w.feed(text[i:i + chunk]))
    out.extend(w.finish())
    dt = time.perf_counter() - t0
    return dt, out


def fmt_rate(nchars, dt):
    return f"{nchars / dt / 1e6:8.2f} M字符/s  ({nchars / dt / 1e6:.2f} MB/s, ASCII 计)"


def main():
    width = 80
    print(f"Python 3.12, 行宽 {width}, atoms={ATOMS}")
    print("=" * 64)
    for n in (100_000, 1_000_000):
        text = gen_text(n)
        dw = display_width(text)
        dt1, lines1 = bench_once(text, width)
        dt2, lines2 = bench_stream(text, width)
        assert lines1 == lines2, "流式与一次性结果不一致！"
        print(f"文本 {len(text):>9,} 字符 / 显示宽 {dw:,} 列 / 输出 {len(lines1):,} 行")
        print(f"  一次性折行 : {dt1 * 1e3:8.1f} ms  {fmt_rate(len(text), dt1)}")
        print(f"  流式折行   : {dt2 * 1e3:8.1f} ms  {fmt_rate(len(text), dt2)}")
        t0 = time.perf_counter()
        truncate(text, width)
        print(f"  截断       : {(time.perf_counter() - t0) * 1e6:8.1f} µs")

    print("=" * 64)
    print("流式内存上界验证（输出行即产即弃，仅统计库内部分配）:")
    for n in (1_000_000, 4_000_000):
        text = gen_text(n)
        w = Wrapper(width, atoms=ATOMS)
        tracemalloc.start()
        for i in range(0, len(text), 4096):
            w.feed(text[i:i + 4096])
        w.finish()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"  输入 {n:>9,} 字符 -> 库内部峰值内存 {peak / 1024:8.1f} KiB")


if __name__ == "__main__":
    main()
