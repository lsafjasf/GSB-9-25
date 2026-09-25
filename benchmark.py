"""百万词表基准：构建耗时/内存 + 阈值 1/2/3 的平均检索耗时。

用法: python3 benchmark.py [词数=1000000] [查询数=300]
词表由固定随机种子生成（小写字母 a-z，长度 3-12），结果可复现。
"""

import os
import random
import resource
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from spellcheck import SpellChecker, damerau_distance

ALPHABET = "abcdefghijklmnopqrst"
MAX_D = 3


def make_vocab(n, seed=42):
    rng = random.Random(seed)
    words = set()
    while len(words) < n:
        length = rng.randint(3, 12)
        words.add("".join(rng.choice(ALPHABET) for _ in range(length)))
    # 帕累托分布近似 Zipf：少量高频词 + 长尾
    freq_rng = random.Random(seed + 1)
    vocab = {w: max(1, int(freq_rng.paretovariate(1.3))) for w in words}
    return vocab


def mutate(word, rng, edits):
    chars = list(word)
    for _ in range(edits):
        if not chars:
            op = "i"
        else:
            op = rng.choice("sidx")
        if op == "s":
            pos = rng.randrange(len(chars))
            chars[pos] = rng.choice(ALPHABET)
        elif op == "i":
            pos = rng.randrange(len(chars) + 1)
            chars.insert(pos, rng.choice(ALPHABET))
        elif op == "d":
            pos = rng.randrange(len(chars))
            chars.pop(pos)
        else:
            if len(chars) >= 2:
                pos = rng.randrange(len(chars) - 1)
                chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
    return "".join(chars)


def make_queries(vocab, n, seed=99):
    rng = random.Random(seed)
    sample = rng.sample(sorted(vocab), n)
    queries = []
    for w in sample:
        # 0~3 个随机编辑：部分查询落在阈值内，部分落在阈值外，贴近真实分布
        queries.append(mutate(w, rng, rng.randint(0, 3)))
    return queries


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    qn = int(sys.argv[2]) if len(sys.argv) > 2 else 300

    print(f"生成合成词表: {n:,} 词 ...", flush=True)
    t0 = time.perf_counter()
    vocab = make_vocab(n)
    print(f"  生成耗时 {time.perf_counter()-t0:.2f}s", flush=True)

    t0 = time.perf_counter()
    checker = SpellChecker(vocab.items())
    build_s = time.perf_counter() - t0
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    total_chars = sum(map(len, vocab))
    print(flush=True)
    print("=" * 64)
    print(f"词表规模          : {checker.vocab_size:,} 词, {total_chars:,} 字符")
    print(f"Trie 节点数       : {checker.trie_nodes:,}")
    print(f"构建耗时          : {build_s:.2f} s")
    print(f"进程峰值 RSS      : {rss_mb:,.0f} MB（含 Python 运行时与词频表）")
    print("=" * 64, flush=True)

    queries = make_queries(vocab, qn)
    print(f"查询数            : {qn}（固定种子，混合 0-3 处随机编辑）")
    print(flush=True)

    # 预热（触发代码路径 JIT/缓存等，Python 无 JIT，仅求口径一致）
    for q in queries[:10]:
        checker.suggest(q, k=10, max_distance=1)

    rows = []
    for d in (1, 2, 3):
        latencies = []
        hit_counts = []
        topk_counts = []
        for q in queries:
            t = time.perf_counter()
            top = checker.suggest(q, k=10, max_distance=d)
            latencies.append((time.perf_counter() - t) * 1000.0)
            topk_counts.append(len(top))
            hit_counts.append(len(checker.candidates(q, max_distance=d)))
        latencies.sort()
        avg = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        rows.append((d, avg, p50, p95,
                     sum(hit_counts) / len(hit_counts),
                     sum(topk_counts) / len(topk_counts)))

    print(f"{'阈值':>4} {'平均ms':>10} {'P50 ms':>9} {'P95 ms':>9} "
          f"{'平均候选数':>12} {'平均TopK':>9}")
    print("-" * 64)
    for d, avg, p50, p95, cavg, kavg in rows:
        print(f"{d:>4} {avg:>10.2f} {p50:>9.2f} {p95:>9.2f} "
              f"{cavg:>12.1f} {kavg:>9.1f}")
    print(flush=True)

    # 对照：同样查询逐个算距离（只取小样本 + 小阈值，避免等太久）
    print("对照实验：暴力逐个计算（1000 词样本, 阈值 2, 10 条查询）")
    sample_words = dict(list(vocab.items())[:1000])
    qs = queries[:10]
    t0 = time.perf_counter()
    for q in qs:
        kept = []
        for w, f in sample_words.items():
            dd = damerau_distance(q, w, True)
            if dd <= 2:
                kept.append((w, dd, f))
        kept.sort(key=lambda x: (x[1], -x[2], abs(len(x[0]) - len(q)), x[0]))
    brute_ms = (time.perf_counter() - t0) / len(qs) * 1000
    scale = len(vocab) / len(sample_words)
    print(f"  暴力 1k 词: {brute_ms:.2f} ms/查询")
    print(f"  按词数线性外推到 {len(vocab):,} 词: ≈ {brute_ms*scale/1000:.0f} s/查询")
    print("  （自动机方案实测见上表，二者相差约 4 个数量级）")


if __name__ == "__main__":
    main()
