"""demo.py — 残差与条件数数据展示 + 病态判定用例。

运行: python3 demo.py
"""

import random

import matrix_decomp as md


def show(name, A, b, method="lu"):
    r = md.solve(A, b, method=method)
    cond = f"{r.condition_estimate:.3e}" if r.condition_estimate != float("inf") else "inf"
    res = f"{r.relative_residual:.3e}" if r.relative_residual == r.relative_residual else "nan"
    print(f"{name:<28} {method:<3} {r.status.value:<16} {cond:<10} {res:<10} {r.message}")


def main():
    rng = random.Random(2026)
    print(f"{'用例':<28} {'方法':<3} {'结论':<16} {'条件数估计':<10} {'相对残差':<10} 说明")
    print("-" * 100)

    n = 10
    show("单位矩阵 I(10)", [[float(i == j) for j in range(n)] for i in range(n)],
         list(range(1, n + 1)))

    show("零矩阵 0(4)", [[0.0] * 4 for _ in range(4)], [1.0] * 4)

    show("需行交换 [[0,1],[1,0]]", [[0.0, 1.0], [1.0, 0.0]], [3.0, 7.0])

    d = [1e-5, 1e-2, 1.0, 1e2, 1e5]
    show("对角尺度 1e-5..1e5", [[d[i] if i == j else 0.0 for j in range(5)]
                               for i in range(5)],
         [d[i] * (i + 1.0) for i in range(5)])

    base = [[rng.uniform(-1, 1) + (6 if i == j else 0.0) for j in range(6)]
            for i in range(6)]
    scales = [10.0 ** (2 * i - 5) for i in range(6)]
    A = [[base[i][j] * scales[i] for j in range(6)] for i in range(6)]
    x_true = [rng.uniform(-1, 1) for _ in range(6)]
    show("行尺度跨 1e-5..1e5", A,
         [sum(A[i][j] * x_true[j] for j in range(6)) for i in range(6)])

    h12 = [[1.0 / (i + j + 1) for j in range(12)] for i in range(12)]
    show("Hilbert-12 (病态)", h12, [sum(row) for row in h12])

    show("近似相关列 (病态)", [[1.0, 1.0], [1.0, 1.0 + 1e-14]], [2.0, 2.0])

    show("重复行 (精确奇异)", [[1.0, 2.0], [2.0, 4.0]], [3.0, 6.0])

    m, k = 50, 3
    A = [[rng.uniform(-1, 1) for _ in range(k)] for _ in range(m)]
    xt = [1.5, -2.0, 0.75]
    show("最小二乘 50x3", A,
         [sum(A[i][j] * xt[j] for j in range(k)) for i in range(m)], method="qr")

    n = 30
    A = [[rng.uniform(-1, 1) + (n if i == j else 0.0) for j in range(n)]
         for i in range(n)]
    xt = [rng.uniform(-10, 10) for _ in range(n)]
    b = [sum(A[i][j] * xt[j] for j in range(n)) for i in range(n)]
    show("随机良态 n=30 (LU)", A, b)
    show("随机良态 n=30 (QR)", A, b, method="qr")


if __name__ == "__main__":
    main()
