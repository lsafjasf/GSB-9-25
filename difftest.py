"""difftest.py — 与参照实现（Python re）对拍。

随机生成模式与输入，比较 引擎 与 re.search 的匹配结论（匹配区间）。
发现不一致时自动收缩，输出最小反例（最小模式 + 最小输入）。

用法:
  python3 difftest.py [--n 5000] [--seed 1] [--demo-shrink]

--demo-shrink 用一个"故意写错的参照"（. 也匹配换行）演示最小反例输出。
"""
import argparse
import random
import re
import sys
import warnings

import regex_engine as E

# re 3.12 对部分未来语法（如 '[['、'--'）只发 FutureWarning，语义与引擎一致，屏蔽噪音。
warnings.filterwarnings("ignore", category=FutureWarning)

# 生成器只产生双方语义一致的语法子集：字面/转义、.、字符类、* + ?、^ $。
# 覆盖非 ASCII（汉字、阿拉伯数字、全角/不换行空白等）与控制字符转义，
# 保证 \d \w \s 的 Unicode 语义及其取反、\n \t 等转义都能被随机用例命中。
LIT_CHARS = list("abcx01_ ") + list(".*+?[]^$\\-") + list("中é٣²")
CONTROL_ESCAPES = ["\\a", "\\f", "\\n", "\\r", "\\t", "\\v"]
# 注意：不包含 \b——它在类内是退格符、类外是 re 的单词边界断言（子集外），
# 非法模式变异把类内转义暴露到类外时会造成"re 接受、引擎报错"的假不一致；
# [\b] 的语义由 test_regex.py 单测覆盖。
CLASS_ITEMS = ["a", "b", "c", "0", "1", " ", ".", "a-c", "0-9", "x",
               "\\d", "\\w", "\\s", "\\D", "\\W", "\\S",
               "中", "é", "٣", "一-鿿", "À-ÿ", "٠-٩",
               "\\n", "\\t", "\\r", "]", "-", "^"]
INPUT_ALPHABET = list("abc01 ._\n\t") + ["中", "é", "٣", "۵", "²",
                                         "\xa0", "\u3000", "\x1c", "\x0b"]


def escape_lit(c):
    return "\\" + c if c in ".*+?[]^$\\" else c


def gen_class(rng):
    items = [rng.choice(CLASS_ITEMS) for _ in range(rng.randint(1, 3))]
    # 保证合法：']' 若出现必须放最前，'-' 若出现必须放最后，'^' 不能放最前
    first = [x for x in items if x == "]"][:1]
    last = [x for x in items if x == "-"][:1]
    mid = [x for x in items if x not in ("]", "-")]
    if not mid:
        mid = ["a"]
    if mid and mid[0] == "^":
        mid[0], mid[-1] = mid[-1], mid[0]
    if mid[0] == "^":
        mid.append("a")
    body = "".join(first + mid + last)
    return "[" + ("^" if rng.random() < 0.3 else "") + body + "]"


def gen_atom(rng):
    r = rng.random()
    if r < 0.40:
        tok = escape_lit(rng.choice(LIT_CHARS))
    elif r < 0.50:
        tok = rng.choice(CONTROL_ESCAPES)
    elif r < 0.62:
        tok = "."
    elif r < 0.85:
        tok = gen_class(rng)
    else:
        tok = rng.choice(["\\d", "\\w", "\\s", "\\D", "\\W", "\\S"])
    if rng.random() < 0.35:
        tok += rng.choice("*+?")
    return tok


def gen_tokens(rng):
    tokens = []
    if rng.random() < 0.25:
        tokens.append("^")
    tokens.extend(gen_atom(rng) for _ in range(rng.randint(0, 7)))
    if rng.random() < 0.25:
        tokens.append("$")
    return tokens


def gen_input(rng):
    r = rng.random()
    if r < 0.08:
        return ""
    if r < 0.16:
        return "a" * rng.randint(1, 30)
    s = "".join(rng.choice(INPUT_ALPHABET) for _ in range(rng.randint(0, 10)))
    if rng.random() < 0.1:
        s += "\n"
    return s


def engine_result(pattern, text):
    try:
        return E.search(pattern, text)
    except E.RegexSyntaxError:
        return "ERR"


def ref_result(pattern, text):
    try:
        m = re.search(pattern, text)
        return m.span() if m else None
    except re.error:
        return "ERR"


def is_mismatch(tokens, text, ref=ref_result):
    return engine_result("".join(tokens), text) != ref("".join(tokens), text)


def shrink(tokens, text, ref=ref_result):
    """收缩反例：先按 token 删模式，再按子串删输入，再逐字符简化输入。"""
    def pass_pattern(tokens, text):
        changed = True
        while changed:  # 删除模式中的连续 token 段（长的优先）
            changed = False
            for length in range(len(tokens), 0, -1):
                for i in range(0, len(tokens) - length + 1):
                    cand = tokens[:i] + tokens[i + length:]
                    if is_mismatch(cand, text, ref):
                        tokens, changed = cand, True
                        break
                if changed:
                    break
        return tokens

    def pass_input(tokens, text):
        changed = True
        while changed:  # 删除输入中的连续子串（长的优先）
            changed = False
            for length in range(len(text), 0, -1):
                for i in range(0, len(text) - length + 1):
                    cand = text[:i] + text[i + length:]
                    if is_mismatch(tokens, cand, ref):
                        text, changed = cand, True
                        break
                if changed:
                    break
        for i in range(len(text)):  # 逐字符替换成更简单的 'a'
            if text[i] != "a" and is_mismatch(tokens, text[:i] + "a" + text[i + 1:], ref):
                text = text[:i] + "a" + text[i + 1:]
        return text

    while True:  # 迭代到不动点：输入变短后可能解锁新的模式删减
        before = (list(tokens), text)
        tokens = pass_pattern(tokens, text)
        text = pass_input(tokens, text)
        if (list(tokens), text) == before:
            break
    return tokens, text


def report(tokens, text, ref=ref_result):
    pattern = "".join(tokens)
    print("=" * 50)
    print("最小反例 (minimal counterexample):")
    print("  pattern: %r" % pattern)
    print("  input:   %r" % text)
    print("  engine:  %r" % (engine_result(pattern, text),))
    print("  re:      %r" % (ref(pattern, text),))
    print("=" * 50)


def fuzz_invalid(rng, n):
    """错误处理对拍：非法模式两边都必须报错（不收缩）。"""
    mutants = 0
    for _ in range(n):
        tokens = gen_tokens(rng)
        kind = rng.randrange(5)
        if kind == 0:
            tokens.insert(rng.randrange(len(tokens) + 1), "[")  # 可能未闭合
        elif kind == 1:
            tokens.append("\\")  # 悬空转义
        elif kind == 2:
            tokens.insert(0, rng.choice("*+?"))  # 量词缺少操作数
        elif kind == 3:
            tokens.insert(rng.randrange(len(tokens) + 1), "[z-a]")  # 区间反序
        else:
            tokens.append("\\e")  # 未知转义
        pattern = "".join(tokens)
        mine, ref = engine_result(pattern, "ab"), ref_result(pattern, "ab")
        if (mine == "ERR") != (ref == "ERR"):
            mutants += 1
            print("错误处理不一致: pattern=%r engine=%r re=%r" % (pattern, mine, ref))
    return mutants


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000, help="随机用例数")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--demo-shrink", action="store_true",
                    help="用故意写错的参照演示最小反例输出")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    if args.demo_shrink:
        # 错误参照：DOTALL 让 '.' 也匹配换行
        def bad_ref(pattern, text):
            m = re.search(pattern, text, re.DOTALL)
            return m.span() if m else None
        for _ in range(200):
            tokens, text = gen_tokens(rng), gen_input(rng)
            if is_mismatch(tokens, text, bad_ref):
                tokens, text = shrink(tokens, text, bad_ref)
                report(tokens, text, bad_ref)
                return 1
        print("未触发（重试 --seed）")
        return 0

    bad = fuzz_invalid(rng, max(1, args.n // 5))
    for case in range(args.n):
        tokens, text = gen_tokens(rng), gen_input(rng)
        if is_mismatch(tokens, text):
            tokens, text = shrink(tokens, text)
            report(tokens, text)
            return 1
    print("对拍通过: %d 组随机用例 + %d 组非法模式用例, 结论全部一致 (seed=%d)"
          % (args.n, max(1, args.n // 5), args.seed))
    if bad:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
