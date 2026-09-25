"""benchmark.py — 百万级词表构建 + 不同阈值检索耗时实测。

用法:
    python3 benchmark.py              # 默认 1,000,000 词
    python3 benchmark.py --n 100000   # 小规模快速验证
"""

import argparse
import random
import resource
import string
import time

from spellcheck import SpellCorrector


def gen_vocab(n, seed=42):
    """生成 n 个唯一随机词（长度 3-12，小写字母），词频近似指数分布。"""
    rng = random.Random(seed)
    words = set()
    alpha = string.ascii_lowercase
    while len(words) < n:
        words.add("".join(rng.choices(alpha, k=rng.randint(3, 12))))
    return [(w, int(rng.expovariate(1 / 1000)) + 1) for w in sorted(words)]


def gen_queries(vocab_words, n_queries, seed=7):
    """从词表抽样并施加 0-3 个随机编辑（替换/插入/删除/换位），模拟真实输错。"""
    rng = random.Random(seed)
    queries = []
    for _ in range(n_queries):
        w = list(rng.choice(vocab_words))
        for _ in range(rng.randint(0, 3)):
            if not w:
                break
            op = rng.randint(0, 3)
            pos = rng.randrange(len(w))
            if op == 0:
                w[pos] = rng.choice(string.ascii_lowercase)
            elif op == 1:
                w.insert(pos, rng.choice(string.ascii_lowercase))
            elif op == 2:
                del w[pos]
            elif pos + 1 < len(w):
                w[pos], w[pos + 1] = w[pos + 1], w[pos]
        if w:
            queries.append("".join(w))
    return queries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1_000_000, help="词表规模")
    ap.add_argument("--queries", type=int, default=300, help="d=1/2 查询数")
    ap.add_argument("--queries-d3", type=int, default=100, help="d=3 查询数")
    args = ap.parse_args()

    print(f"[1/3] 生成 {args.n:,} 词词表 ...")
    t0 = time.perf_counter()
    vocab = gen_vocab(args.n)
    print(f"      完成，耗时 {time.perf_counter() - t0:.1f}s")

    print("[2/3] 构建 Trie 索引 ...")
    t0 = time.perf_counter()
    sc = SpellCorrector(vocab)
    build_s = time.perf_counter() - t0
    mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"      节点数 {sc._node_count:,}，构建耗时 {build_s:.1f}s，"
          f"峰值内存 {mem_mb:.0f} MB")

    print("[3/3] 检索基准 ...")
    vocab_words = [w for w, _ in vocab]
    queries = gen_queries(vocab_words, args.queries)
    queries_d3 = gen_queries(vocab_words, args.queries_d3, seed=8)

    print(f"{'阈值 d':<8}{'换位':<6}{'查询数':<8}{'总耗时(s)':<12}"
          f"{'平均(ms)':<10}{'P95(ms)':<10}{'平均候选数':<10}")
    for d, tr, qs in [(1, False, queries), (2, False, queries),
                      (3, False, queries_d3), (2, True, queries)]:
        lat = []
        n_cand = 0
        for q in qs:
            t0 = time.perf_counter()
            res = sc.suggest(q, max_distance=d, top_k=10, transpositions=tr)
            lat.append((time.perf_counter() - t0) * 1000)
            n_cand += len(res) if len(res) < 10 else -1  # 占位，下面单独统计
        n_cand = sum(len(sc.suggest(q, max_distance=d, top_k=None,
                                    transpositions=tr)) for q in qs[:20])
        n_cand = n_cand * len(qs) // 20  # 抽样 20 条估算
        lat.sort()
        p95 = lat[int(len(lat) * 0.95)]
        print(f"{d:<8}{str(tr):<6}{len(qs):<8}{sum(lat)/1000:<12.2f}"
              f"{sum(lat)/len(lat):<10.2f}{p95:<10.2f}{n_cand/len(qs):<10.1f}")


if __name__ == "__main__":
    main()
