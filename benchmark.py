"""benchmark —— 利用率对比与规模耗时。

用法：
  python3 benchmark.py                 人类可读的实测输出
  python3 benchmark.py --markdown      打印 README 用的 Markdown 表格
  python3 benchmark.py --update-docs   用实测数据重写 README.md 中的表格
  python3 benchmark.py --check-docs    断言 README.md 表格逐行与脚本输出一致
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rectpack import pack, pack_naive

README_PATH = Path(__file__).with_name("README.md")

# 利用率对比的全量配置：(数据集, 容器宽, 数据生成函数, allow_rotation)。
# 每个数据集的旋转关闭/开启两档都必须列出，不做筛选——README 表格据此生成。
COMPARE_CASES = [
    ("均匀小矩形", 100, lambda: gen_uniform(300, 1, 5, 30), False),
    ("均匀小矩形", 100, lambda: gen_uniform(300, 1, 5, 30), True),
    ("大小混合", 200, lambda: gen_mixed(400, 2), False),
    ("大小混合", 200, lambda: gen_mixed(400, 2), True),
    ("细长条", 150, lambda: gen_stripes(300, 3), False),
    ("细长条", 150, lambda: gen_stripes(300, 3), True),
    ("均匀中方块", 300, lambda: gen_uniform(800, 4, 10, 45), True),
]
SCALE_NS = (500, 1000, 2000, 5000)
SCALE_SEED = 10


# ---------------------------------------------------------------- 数据生成

def gen_uniform(n, seed, lo, hi):
    rng = random.Random(seed)
    return [(rng.randint(lo, hi), rng.randint(lo, hi)) for _ in range(n)]


def gen_mixed(n, seed):
    """少量大面板 + 大量小面板，模拟导出版面。"""
    rng = random.Random(seed)
    rects = []
    for i in range(n):
        if i % 20 == 0:
            rects.append((rng.randint(60, 120), rng.randint(40, 90)))
        else:
            rects.append((rng.randint(5, 30), rng.randint(5, 30)))
    return rects


def gen_stripes(n, seed):
    """细长条为主（旋转模式收益大的场景）。"""
    rng = random.Random(seed)
    return [(rng.randint(40, 90), rng.randint(4, 12)) for _ in range(n)]


# ---------------------------------------------------------------- 取数

@dataclass(frozen=True)
class CompareRow:
    """一组利用率对比的可复现结果（不含耗时，耗时随机器波动）。"""
    name: str
    n: int
    rotation: bool
    fast_height: float
    fast_utilization: float
    naive_height: float
    naive_utilization: float
    gain_h: float


def run_compare(name, width, make_rects, allow_rotation):
    rects = make_rects()
    fast = pack(width, rects, allow_rotation=allow_rotation)
    naive = pack_naive(width, rects, allow_rotation=allow_rotation)
    gain_h = (naive.height - fast.height) / naive.height * 100
    return CompareRow(name, len(rects), allow_rotation,
                      fast.height, fast.utilization,
                      naive.height, naive.utilization, gain_h)


def run_scale(n, seed):
    """返回 (n, 耗时秒, 所需高度, 利用率)；后两列确定性可复现。"""
    rects = gen_mixed(n, seed)
    t0 = time.perf_counter()
    res = pack(400, rects, allow_rotation=True)
    dt = time.perf_counter() - t0
    return n, dt, res.height, res.utilization


def collect_compare_rows():
    return [run_compare(*case) for case in COMPARE_CASES]


def collect_scale_records():
    return [run_scale(n, SCALE_SEED) for n in SCALE_NS]


# ---------------------------------------------------------------- Markdown

def _md_row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


COMPARE_HEADER = ["数据集", "n", "旋转", "MaxRects 利用率",
                  "朴素 Shelf 利用率", "所需高度降低"]
SCALE_HEADER = ["n", "耗时", "平均每矩形", "利用率"]


def render_compare_md(rows):
    lines = [_md_row(COMPARE_HEADER), "|" + "---|" * len(COMPARE_HEADER)]
    for r in rows:
        lines.append(_md_row([
            r.name, r.n, "是" if r.rotation else "否",
            f"{r.fast_utilization:.2%}", f"{r.naive_utilization:.2%}",
            f"{r.gain_h:.1f}%",
        ]))
    return "\n".join(lines)


def render_scale_md(records):
    lines = [_md_row(SCALE_HEADER), "|" + "---|" * len(SCALE_HEADER)]
    for n, dt, _height, utilization in records:
        lines.append(_md_row([
            n, f"{dt:.3f}s", f"{dt / n * 1e6:.0f}µs",
            f"{utilization:.2%}",
        ]))
    return "\n".join(lines)


# ---------------------------------------------------- README 生成与逐行校验

def _replace_block(text, tag, body):
    pattern = re.compile(
        rf"(<!-- BENCHMARK:{tag} BEGIN -->\n).*?(<!-- BENCHMARK:{tag} END -->)",
        re.S)
    new, count = pattern.subn(
        lambda m: m.group(1) + body + "\n" + m.group(2), text)
    assert count == 1, f"README.md 缺少 {tag} 表格生成标记（<!-- BENCHMARK:{tag} ... -->）"
    return new


def _block_lines(text, tag):
    match = re.search(
        rf"<!-- BENCHMARK:{tag} BEGIN -->\n(.*?)<!-- BENCHMARK:{tag} END -->",
        text, re.S)
    assert match, f"README.md 缺少 {tag} 表格生成标记"
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip()]


def _cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line):
    return bool(re.fullmatch(r"\|[\s:|-]+\|?", line.strip()))


def _pct(cell):
    return float(cell.rstrip("%"))


def check_readme(path=README_PATH):
    """逐行断言 README.md 表格与脚本真实输出一致；不一致抛 AssertionError。"""
    text = Path(path).read_text(encoding="utf-8")

    # 1) 利用率对比：每一行的每一列都必须精确对上脚本输出（含细长条旋转开/关两档）
    rows = collect_compare_rows()
    lines = _block_lines(text, "COMPARE")
    assert _cells(lines[0]) == COMPARE_HEADER, "利用率对比表头与脚本不一致"
    body = [line for line in lines[1:] if not _is_separator(line)]
    assert len(body) == len(rows), (
        f"利用率对比表 {len(body)} 行，脚本输出 {len(rows)} 行"
        "（每个数据集的旋转关/开两档都必须列出）")
    for line, r in zip(body, rows):
        cells = _cells(line)
        assert int(cells[1]) == r.n, line
        assert cells[0] == r.name, line
        assert cells[2] == ("是" if r.rotation else "否"), line
        assert abs(_pct(cells[3]) - r.fast_utilization * 100) < 0.005, line
        assert abs(_pct(cells[4]) - r.naive_utilization * 100) < 0.005, line
        assert abs(_pct(cells[5]) - r.gain_h) < 0.05, line

    # 2) 正文给出的高度降低区间必须能直接从上表复现
    gains = [r.gain_h for r in rows]
    lo, hi = min(gains), max(gains)
    assert f"{lo:.1f}%–{hi:.1f}%" in text, (
        f"正文必须写明高度降低区间 {lo:.1f}%–{hi:.1f}%（取表中最右列最小/最大值）")

    # 3) 规模耗时：n、利用率为确定性列，严格逐行对账；耗时仅校验为正且两列自洽
    records = collect_scale_records()
    lines = _block_lines(text, "SCALE")
    assert _cells(lines[0]) == SCALE_HEADER, "规模耗时表头与脚本不一致"
    body = [line for line in lines[1:] if not _is_separator(line)]
    assert len(body) == len(records), (
        f"规模耗时表 {len(body)} 行，脚本输出 {len(records)} 行")
    for line, (n, _dt, _height, utilization) in zip(body, records):
        cells = _cells(line)
        assert int(cells[0]) == n, line
        seconds = float(cells[1][:-1])       # 去掉末尾 "s"
        per_rect = float(cells[2][:-2])      # 去掉末尾 "µs"
        assert seconds > 0 and per_rect > 0, line
        assert abs(per_rect - seconds * 1e6 / n) <= 2.0, line
        assert abs(_pct(cells[3]) - utilization * 100) < 0.005, line

    return rows, records


def update_readme(path=README_PATH):
    text = Path(path).read_text(encoding="utf-8")
    text = _replace_block(text, "COMPARE",
                          render_compare_md(collect_compare_rows()))
    text = _replace_block(text, "SCALE",
                          render_scale_md(collect_scale_records()))
    Path(path).write_text(text, encoding="utf-8")
    check_readme(path)


# ---------------------------------------------------------------- 文本报告

def print_compare_report():
    print("=== 利用率对比（MaxRects vs 按面积排序的朴素 Shelf） ===")
    for name, width, make_rects, allow_rotation in COMPARE_CASES:
        rects = make_rects()
        t0 = time.perf_counter()
        fast = pack(width, rects, allow_rotation=allow_rotation)
        t1 = time.perf_counter()
        naive = pack_naive(width, rects, allow_rotation=allow_rotation)
        t2 = time.perf_counter()
        gain_h = (naive.height - fast.height) / naive.height * 100
        print(f"{name:<28} n={len(rects):<5} 旋转={'是' if allow_rotation else '否'}  "
              f"MaxRects: 高={fast.height:<7} 利用率={fast.utilization:.2%}  "
              f"朴素: 高={naive.height:<7} 利用率={naive.utilization:.2%}  "
              f"高度降低={gain_h:5.1f}%  耗时={t1 - t0:.3f}s/{t2 - t1:.3f}s"
              f"  未放置={len(fast.unplaced)}")


def print_scale_report():
    print("=== 规模与耗时（宽 400，混合数据，允许旋转） ===")
    for n, dt, height, utilization in collect_scale_records():
        print(f"n={n:<6} 耗时={dt:7.3f}s  高度={height:<8} "
              f"利用率={utilization:.2%}  平均每矩形={dt / n * 1e6:8.1f}µs")


def main(argv=None):
    parser = argparse.ArgumentParser(description="矩形打包利用率基准")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--markdown", action="store_true",
                       help="打印 README 用 Markdown 表格")
    group.add_argument("--update-docs", action="store_true",
                       help="用实测数据重写 README.md 表格并自检")
    group.add_argument("--check-docs", action="store_true",
                       help="断言 README.md 表格逐行与脚本输出一致")
    args = parser.parse_args(argv)

    if args.markdown:
        print(render_compare_md(collect_compare_rows()))
        print()
        print(render_scale_md(collect_scale_records()))
    elif args.update_docs:
        update_readme()
        print(f"已重新生成 {README_PATH.name} 中的表格，逐行校验通过")
    elif args.check_docs:
        try:
            check_readme()
        except AssertionError as exc:
            print(f"文档表格与脚本输出不一致：{exc}", file=sys.stderr)
            raise SystemExit(1)
        print("README 表格逐行校验通过（利用率对比 7 行 + 规模耗时 4 行）")
    else:
        print_compare_report()
        print()
        print_scale_report()


if __name__ == "__main__":
    main()
