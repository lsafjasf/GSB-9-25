"""策略对比基准 + 可切换单次运行。

用法：
  python3 benchmark.py                      # 全策略 × 全数据集对比 + 多容器场景
  python3 benchmark.py run --order area_desc --select baf \
      --dataset mixed --n 200 --max-height 500 --containers 3 --rotate
"""

import argparse
import random
import time

from rectpack import (SelectRule, SortOrder, StripPacker, pack, pack_multi)

ORDERS = [SortOrder.AREA_DESC, SortOrder.HEIGHT_DESC, SortOrder.MAXSIDE_DESC]
SELECTS = [SelectRule.BSSF, SelectRule.BAF]


def gen_dataset(kind, n, seed):
    rng = random.Random(seed)
    if kind == "uniform":          # 均匀中小矩形
        return [(rng.randint(10, 50), rng.randint(10, 50)) for _ in range(n)]
    if kind == "mixed":            # 少量大板 + 大量小板
        out = [(rng.randint(80, 160), rng.randint(80, 160)) for _ in range(n // 10)]
        out += [(rng.randint(5, 30), rng.randint(5, 30)) for _ in range(n - n // 10)]
        return out
    if kind == "skewed":           # 细长条（旋转收益场景）
        return [(rng.randint(60, 180), rng.randint(4, 12)) for _ in range(n)]
    if kind == "squares":          # 方形为主
        return [(lambda s: (s, s))(rng.randint(8, 60)) for _ in range(n)]
    raise ValueError(kind)


def compare_strategies(width=200, n=200, seeds=range(5), rotate=False):
    """每种 排序×选择 策略在各数据集上的利用率 / 高度 / 耗时对比。"""
    print(f"=== 策略对比（容器宽 {width}，n={n}，{len(list(seeds))} 个种子取平均，"
          f"{'旋转' if rotate else '不旋转'}） ===")
    header = (f"{'数据集':<9}{'排序策略':<13}{'选择策略':<9}"
              f"{'高度':>9}{'利用率':>9}{'耗时(ms)':>10}{'未放置':>7}")
    print(header)
    for kind in ("uniform", "mixed", "skewed", "squares"):
        for order in ORDERS:
            for sel in SELECTS:
                hs, us, ts, ups = [], [], [], 0
                for seed in seeds:
                    rects = gen_dataset(kind, n, seed)
                    t0 = time.perf_counter()
                    res = pack(width, rects, allow_rotate=rotate,
                               sort=order, select=sel)
                    ts.append((time.perf_counter() - t0) * 1000)
                    hs.append(res.height)
                    us.append(res.utilization)
                    ups += len(res.unplaced)
                m = len(list(seeds))
                print(f"{kind:<9}{order.value:<13}{sel.value:<9}"
                      f"{sum(hs)/m:>9.1f}{sum(us)/m:>9.2%}"
                      f"{sum(ts)/m:>10.2f}{ups:>7}")
        print("-" * len(header.expandtabs()))


def compare_multi_container(width=200, max_height=400, n=300, seeds=range(5)):
    """高度上限 + 多容器场景：各策略所需容器数 / 总利用率 / 未放置数 / 耗时。"""
    print(f"\n=== 多容器场景（容器宽 {width}，单容器高度上限 {max_height}，"
          f"n={n}，{len(list(seeds))} 个种子取平均） ===")
    print(f"{'数据集':<9}{'排序策略':<13}{'选择策略':<9}"
          f"{'容器数':>7}{'总利用率':>9}{'未放置':>7}{'耗时(ms)':>10}")
    for kind in ("uniform", "mixed", "skewed", "squares"):
        for order in ORDERS:
            for sel in SELECTS:
                cs, us, ts, ups = [], [], [], 0
                for seed in seeds:
                    rects = gen_dataset(kind, n, seed)
                    t0 = time.perf_counter()
                    res = pack_multi(width, rects, sort=order, select=sel,
                                     max_height=max_height)
                    ts.append((time.perf_counter() - t0) * 1000)
                    cs.append(len(res.containers))
                    us.append(res.utilization)
                    ups += len(res.unplaced)
                m = len(list(seeds))
                print(f"{kind:<9}{order.value:<13}{sel.value:<9}"
                      f"{sum(cs)/m:>7.1f}{sum(us)/m:>9.2%}"
                      f"{ups:>7}{sum(ts)/m:>10.2f}")
        print()


def scale_timing():
    print("=== 规模与耗时（容器宽 400，uniform 5~60，不旋转，BSSF/长边降序） ===")
    print(f"{'n':>7}{'耗时(ms)':>12}{'高度':>10}{'利用率':>9}{'ns/矩形':>12}")
    for n in (1000, 2000, 4000, 8000):
        rects = gen_dataset("uniform", n, seed=99)
        t0 = time.perf_counter()
        res = pack(400, rects)
        dt = (time.perf_counter() - t0) * 1000
        assert not res.unplaced
        print(f"{n:>7}{dt:>12.1f}{res.height:>10.0f}{res.utilization:>9.2%}"
              f"{dt * 1e6 / n:>12.0f}")


def run_single(args):
    rects = gen_dataset(args.dataset, args.n, args.seed)
    packer = StripPacker(args.width, allow_rotate=args.rotate,
                         sort=SortOrder(args.order),
                         select=SelectRule(args.select),
                         max_height=args.max_height)
    t0 = time.perf_counter()
    if args.containers is not None or args.max_height is not None:
        res = packer.pack_multi(rects, max_containers=args.containers)
        dt = (time.perf_counter() - t0) * 1000
        print(f"策略: order={args.order} select={args.select} "
              f"rotate={args.rotate} max_height={args.max_height}")
        print(f"容器数: {len(res.containers)}  总利用率: {res.utilization:.2%}  "
              f"耗时: {dt:.2f} ms")
        for i, c in enumerate(res.containers):
            print(f"  容器#{i}: 高度={c.height:.0f} 利用率={c.utilization:.2%} "
                  f"已放置={len(c.placements)}")
        print(f"未放置矩形: {res.unplaced}")
    else:
        res = packer.pack(rects)
        dt = (time.perf_counter() - t0) * 1000
        print(f"策略: order={args.order} select={args.select} rotate={args.rotate}")
        print(f"高度: {res.height:.0f}  利用率: {res.utilization:.2%}  "
              f"耗时: {dt:.2f} ms")
        print(f"未放置矩形: {res.unplaced}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("run", help="按指定策略单次运行")
    rp.add_argument("--order", default="maxside_desc",
                    choices=[o.value for o in SortOrder])
    rp.add_argument("--select", default="bssf",
                    choices=[s.value for s in SelectRule])
    rp.add_argument("--dataset", default="uniform",
                    choices=["uniform", "mixed", "skewed", "squares"])
    rp.add_argument("--n", type=int, default=200)
    rp.add_argument("--width", type=float, default=200)
    rp.add_argument("--seed", type=int, default=0)
    rp.add_argument("--rotate", action="store_true")
    rp.add_argument("--max-height", type=float, default=None)
    rp.add_argument("--containers", type=int, default=None,
                    help="容器数量上限（配合 --max-height 使用）")
    args = ap.parse_args()
    if args.cmd == "run":
        run_single(args)
    else:
        compare_strategies()
        compare_multi_container()
        scale_timing()


if __name__ == "__main__":
    main()
