"""benchmark.py — LU 与 QR 分解/求解耗时对比。

运行: python3 benchmark.py
"""

import random
import time

import matrix_decomp as md

SIZES = [32, 64, 128, 256, 512]
REPEAT = 3


def make_problem(n, seed):
    rng = random.Random(seed)
    A = [[rng.uniform(-1, 1) + (n if i == j else 0.0)
          for j in range(n)] for i in range(n)]
    b = [rng.uniform(-1, 1) for _ in range(n)]
    return A, b


def bench(fn, *args):
    best = float("inf")
    for _ in range(REPEAT):
        t0 = time.perf_counter()
        result = fn(*args)
        best = min(best, time.perf_counter() - t0)
    return best, result


def main():
    print(f"{'n':>5} | {'LU分解':>9} {'LU求解':>9} | "
          f"{'QR分解':>9} {'QR求解':>9} | {'QR/LU分解':>9} {'QR/LU总计':>9}")
    print("-" * 78)
    for n in SIZES:
        A, b = make_problem(n, seed=42 + n)
        t_lu_f, (LU, perm, _) = bench(md.lu_factor, A)
        t_lu_s, _ = bench(md.lu_solve, LU, perm, b)
        t_qr_f, (R, V, tau, _) = bench(md.qr_factor, A)
        t_qr_s, _ = bench(md.qr_solve, R, V, tau, b)
        lu_total, qr_total = t_lu_f + t_lu_s, t_qr_f + t_qr_s
        print(f"{n:>5} | {t_lu_f*1e3:>8.2f}m {t_lu_s*1e3:>8.2f}m | "
              f"{t_qr_f*1e3:>8.2f}m {t_qr_s*1e3:>8.2f}m | "
              f"{t_qr_f/t_lu_f:>8.2f}x {qr_total/lu_total:>8.2f}x")
    print("\n(时间为 3 次取最优; 理论浮点运算量: LU ~ n^3/3, "
          "Householder QR ~ 4n^3/3, 故 QR 分解约为 LU 的 4 倍)")


if __name__ == "__main__":
    main()
