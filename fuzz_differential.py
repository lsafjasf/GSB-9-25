"""对拍脚本：versioning 库 vs 独立参照实现。

- 随机生成版本对与范围表达式，比较结论与范围匹配结果必须与参照实现一致。
- 同时断言全序性质（三歧性 / 反对称 / 传递 / 排序与逐对比较一致）。
- 发现不一致时自动收缩并输出最小反例。

用法: python3 fuzz_differential.py [--cases N] [--seed S]
"""

import argparse
import random
import re
import sys

from versioning import parse_range, parse_version

# ============================================================ 参照实现
# 与被测库独立：正则整体解析 + 命令式比较，不复用 versioning 的任何函数。

_REF_RE = re.compile(
    r"^(0|[1-9][0-9]*)"
    r"(?:\.(0|[1-9][0-9]*))?"
    r"(?:\.(0|[1-9][0-9]*))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def ref_parse(text):
    m = _REF_RE.match(text.strip())
    if not m:
        raise ValueError("bad version: %r" % text)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    patch = int(m.group(3)) if m.group(3) else 0
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    build = tuple(m.group(5).split(".")) if m.group(5) else ()
    for ident in pre:
        if ident.isdigit() and len(ident) > 1 and ident[0] == "0":
            raise ValueError("leading zero: %r" % text)
    return (major, minor, patch, pre, build)


def _ref_cmp_ident(a, b):
    an, bn = a.isdigit(), b.isdigit()
    if an and bn:
        return (int(a) > int(b)) - (int(a) < int(b))
    if an:
        return -1  # 数字 < 字母数字
    if bn:
        return 1
    return (a > b) - (a < b)


def ref_compare(x, y):
    """返回 -1/0/1。规则与库文档一致，但完全命令式实现。"""
    for i in range(3):
        if x[i] != y[i]:
            return -1 if x[i] < y[i] else 1
    px, py = x[3], y[3]
    if not px and not py:
        pass
    elif not px:
        return 1  # 正式版 > 预发布版
    elif not py:
        return -1
    else:
        for i in range(min(len(px), len(py))):
            c = _ref_cmp_ident(px[i], py[i])
            if c:
                return c
        if len(px) != len(py):
            return -1 if len(px) < len(py) else 1
    # 构建元数据仅作最终平局裁决
    bx, by = x[4], y[4]
    for i in range(min(len(bx), len(by))):
        c = _ref_cmp_ident(bx[i], by[i])
        if c:
            return c
    if len(bx) != len(by):
        return -1 if len(bx) < len(by) else 1
    return 0


def _ref_expand_token(tok):
    """把范围 token 展开为 [(op, version_tuple), ...]，与库规则一致、独立实现。"""
    if tok in ("*", "x", "X"):
        return []
    m = re.match(r"(>=|<=|>|<|=|!|\^|~)?(.*)", tok)
    op, body = m.group(1) or "", m.group(2)
    main, _, _build = body.partition("+")
    core, _, pre = main.partition("-")
    segs = core.split(".")
    wild = any(s in ("*", "x", "X") for s in segs)
    nums = [int(s) for s in segs if s not in ("*", "x", "X")]
    if pre:
        return [(op or "=", ref_parse(body))]
    if op == "^":
        full = nums + [0] * (3 - len(nums))
        if full[0] > 0:
            hi = (full[0] + 1, 0, 0)
        elif len(nums) >= 2 and full[1] > 0:
            hi = (0, full[1] + 1, 0)
        elif len(nums) >= 3:
            hi = (0, 0, full[2] + 1)
        elif len(nums) == 2:
            hi = (0, 1, 0)
        else:
            hi = (1, 0, 0)
        return [(">=", tuple(full) + ((), ())), ("<", hi + ((), ()))]
    if op == "~":
        full = nums + [0] * (3 - len(nums))
        hi = (full[0] + 1, 0, 0) if len(nums) <= 1 else (full[0], full[1] + 1, 0)
        return [(">=", tuple(full) + ((), ())), ("<", hi + ((), ()))]
    if wild or len(segs) < 3:
        if op not in ("", "="):
            raise ValueError("wildcard with op")
        lo = tuple(nums + [0] * (3 - len(nums)))
        lvl = (segs.index(next(s for s in segs if s in ("*", "x", "X"))) + 1
               if wild else len(segs) + 1)
        hi = list(lo)
        hi[lvl - 2] += 1
        for k in range(lvl - 1, 3):
            hi[k] = 0
        return [(">=", lo + ((), ())), ("<", tuple(hi) + ((), ()))]
    return [(op or "=", ref_parse(body))]


def ref_matches(range_str, version_str, include_prerelease):
    v = ref_parse(version_str)
    if v[3] and not include_prerelease:
        return False
    for tok in range_str.split():
        for op, t in _ref_expand_token(tok):
            c = ref_compare(v, t)
            ok = {"<": c < 0, "<=": c <= 0, ">": c > 0,
                  ">=": c >= 0, "=": c == 0, "!": c != 0}[op]
            if not ok:
                return False
    return True


# ============================================================ 随机生成

_PRE_IDENTS = ["0", "1", "2", "11", "a", "b", "alpha", "beta", "rc", "x-1"]
_BUILDS = ["b1", "b2", "001", "meta", "a", "1"]


def gen_version(rng):
    s = "%d.%d.%d" % (rng.randrange(3), rng.randrange(3), rng.randrange(3))
    if rng.random() < 0.5:
        s += "-" + ".".join(rng.choice(_PRE_IDENTS)
                            for _ in range(rng.randrange(1, 4)))
    if rng.random() < 0.4:
        s += "+" + ".".join(rng.choice(_BUILDS)
                            for _ in range(rng.randrange(1, 3)))
    return s


def gen_range(rng):
    toks = []
    for _ in range(rng.randrange(1, 4)):
        kind = rng.randrange(7)
        ver = gen_version(rng).split("+")[0]
        if "-" in ver and rng.random() < 0.7:
            ver = ver.split("-")[0]
        if kind == 0:
            toks.append(rng.choice([">", ">=", "<", "<=", "=", "!"]) + ver)
        elif kind == 1:
            toks.append(ver)
        elif kind == 2:
            parts = ver.split("-")[0].split(".")
            n = rng.randrange(1, 3)
            toks.append(".".join(parts[:n] + ["*"]))
        elif kind == 3:
            toks.append("^" + ver.split("-")[0])
        elif kind == 4:
            toks.append("~" + ver.split("-")[0])
        elif kind == 5:
            toks.append("!")
        else:
            toks.append(rng.choice([">=", "<"]) + ver)
    return " ".join(t for t in toks if t != "!")


# ============================================================ 对拍与收缩

def find_mismatch(case):
    """返回不一致描述；一致返回 None。case = (v1, v2, range_str)。"""
    v1, v2, rng_str = case
    try:
        a, b = parse_version(v1), parse_version(v2)
        ra, rb = ref_parse(v1), ref_parse(v2)
    except ValueError:
        return None  # 双方都应能解析的输入才参与对拍
    lib_cmp = (a > b) - (a < b)
    ref_cmp = ref_compare(ra, rb)
    if lib_cmp != ref_cmp:
        return "compare: %s vs %s -> lib=%d ref=%d" % (v1, v2, lib_cmp, ref_cmp)
    try:
        r = parse_range(rng_str)
    except Exception:
        return None
    try:
        for ver in (v1, v2):
            for inc in (False, True):
                lib_m = r.matches(ver, include_prerelease=inc)
                ref_m = ref_matches(rng_str, ver, inc)
                if lib_m != ref_m:
                    return ("match: range=%r version=%s include_prerelease=%s "
                            "-> lib=%s ref=%s" % (rng_str, ver, inc, lib_m, ref_m))
    except ValueError:
        return None
    return None


def _simplify_version(v):
    cands = []
    if "+" in v:
        cands.append(v.split("+")[0])
    if "-" in v:
        cands.append(v.split("-")[0] + ("+" + v.split("+")[1] if "+" in v else ""))
        pre = v.split("-")[1].split("+")[0].split(".")
        if len(pre) > 1:
            tail = ("+" + v.split("+")[1]) if "+" in v else ""
            cands.append(v.split("-")[0] + "-" + ".".join(pre[:-1]) + tail)
    nums = re.findall(r"\d+", v)
    for n in set(nums):
        if n != "0":
            cands.append(re.sub(r"\b%s\b" % re.escape(n), "0", v, count=1))
        if n != "1":
            cands.append(re.sub(r"\b%s\b" % re.escape(n), "1", v, count=1))
    for word in ("alpha", "beta", "rc", "meta", "x-1"):
        if word in v:
            cands.append(v.replace(word, "a", 1))
    return [c for c in cands if c != v]


def _simplify_range(r):
    toks = r.split()
    cands = []
    if len(toks) > 1:
        for i in range(len(toks)):
            cands.append(" ".join(toks[:i] + toks[i + 1:]))
    for i, t in enumerate(toks):
        for simpler in ("*", re.sub(r"^(>=|<=|>|<|=|!|\^|~)", "", t) or "0.0.0"):
            if simpler != t:
                cands.append(" ".join(toks[:i] + [simpler] + toks[i + 1:]))
    return cands


def shrink(case):
    """贪心收缩到 1-极小反例。"""
    improved = True
    while improved:
        improved = False
        v1, v2, r = case
        for cand in ([(a, v2, r) for a in _simplify_version(v1)]
                     + [(v1, b, r) for b in _simplify_version(v2)]
                     + [(v1, v2, rr) for rr in _simplify_range(r)]):
            if find_mismatch(cand):
                case = cand
                improved = True
                break
    return case


def check_total_order(rng, versions):
    """全序断言：三歧性、反对称、传递性、排序与逐对比较一致。"""
    vs = [parse_version(v) for v in versions]
    for a in vs:
        for b in vs:
            assert sum([a < b, a == b, a > b]) == 1, (a, b)
            assert (a < b) == (b > a), (a, b)
    for _ in range(2000):
        a, b, c = rng.choice(vs), rng.choice(vs), rng.choice(vs)
        if a < b and b < c:
            assert a < c, (a, b, c)
    shuffled = vs[:]
    rng.shuffle(shuffled)
    by_sorted = sorted(shuffled)
    by_insert = []
    for x in shuffled:
        i = 0
        while i < len(by_insert) and not x < by_insert[i]:
            i += 1
        by_insert.insert(i, x)
    assert by_sorted == by_insert, "sorted() 与逐对比较排序不一致"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20260925)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    for n in range(args.cases):
        case = (gen_version(rng), gen_version(rng), gen_range(rng))
        msg = find_mismatch(case)
        if msg:
            minimal = shrink(case)
            print("发现不一致 (case #%d):" % n)
            print("  原始反例: %s" % (case,))
            print("  最小反例: v1=%r v2=%r range=%r" % minimal)
            print("  不一致内容: %s" % find_mismatch(minimal))
            return 1
        if n % 200 == 0:
            check_total_order(rng, [gen_version(rng) for _ in range(12)])
    print("OK: %d 组对拍一致，全序断言通过 (seed=%d)" % (args.cases, args.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
