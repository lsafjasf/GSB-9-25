"""对拍脚本：本引擎 vs Python re（参照实现）。

用法: python3 difftest.py [迭代次数，默认 20000]

随机生成模式与输入，比较两边“匹配/不匹配/语法错误”的结论是否一致。
发现不一致时自动收缩（单字符删除意义下的最小反例）并输出。
注意：re 仅作为测试参照，引擎本体（regex_engine.py）不依赖 re。
"""
import random
import re
import sys
import warnings

warnings.simplefilter('ignore', FutureWarning)  # re 对 [[] 等写法发警告，语义当前一致

import regex_engine as engine


def ref_verdict(pattern, text):
    try:
        prog = re.compile(pattern)
    except re.error:
        return 'error'
    return prog.search(text) is not None


def my_verdict(pattern, text):
    try:
        prog = engine.compile(pattern)
    except engine.RegexSyntaxError as exc:
        if '占有量词' in exc.reason:
            return 'unsupported'  # 有意拒绝：占有量词无法在线性 NFA 模拟中实现
        return 'error'
    return prog.search(text)


def is_counterexample(pattern, text):
    mine = my_verdict(pattern, text)
    if mine == 'unsupported':
        return False  # 有意为之的差异：明确报错，而非静默给出错误结论
    return ref_verdict(pattern, text) != mine


def shrink(pattern, text):
    """反复尝试删除单个字符，得到单字符删除意义下的最小反例。"""
    improved = True
    while improved:
        improved = False
        for i in range(len(text)):
            candidate = text[:i] + text[i + 1:]
            if is_counterexample(pattern, candidate):
                text = candidate
                improved = True
                break
        if improved:
            continue
        for i in range(len(pattern)):
            candidate = pattern[:i] + pattern[i + 1:]
            if is_counterexample(candidate, text):
                pattern = candidate
                improved = True
                break
    return pattern, text


# ------------------------------------------------------------ 随机生成

LITERALS = 'abcx01 .'
CLASS_CHARS = 'abcxyz019'
INPUT_ALPHABET = 'aabbc1 .\n-*[]{}^$\\'


def gen_class(rng):
    parts = ['[']
    if rng.random() < 0.3:
        parts.append('^')
    if rng.random() < 0.08:
        parts.append(']')  # 首位的 ] 是字面量
    for _ in range(rng.randint(1, 4)):
        r = rng.random()
        if r < 0.35:
            lo = rng.choice(CLASS_CHARS)
            hi = rng.choice(CLASS_CHARS)
            if hi < lo:
                lo, hi = hi, lo
            parts.append(f'{lo}-{hi}')
        elif r < 0.5:
            parts.append('\\' + rng.choice('dDwWsS]^-\\'))
        elif r < 0.6:
            parts.append('-')
        else:
            parts.append(rng.choice(CLASS_CHARS + '^['))
    parts.append(']')
    return ''.join(parts)


def gen_atom(rng):
    """返回 (文本, 是否可被量化)。"""
    r = rng.random()
    if r < 0.40:
        return rng.choice(LITERALS), True
    if r < 0.50:
        return '.', True
    if r < 0.60:
        return rng.choice('^$'), False
    if r < 0.80:
        return gen_class(rng), True
    return '\\' + rng.choice('dDwWsSnt.*[\\^$'), True


def gen_quantifier(rng):
    r = rng.random()
    if r < 0.3:
        q = rng.choice('*+?')
    elif r < 0.5:
        lo = rng.randint(0, 3)
        hi = lo + rng.randint(0, 3)
        q = rng.choice([f'{{{lo}}}', f'{{{lo},}}', f'{{{lo},{hi}}}', f'{{,{hi}}}'])
    else:
        return ''
    if rng.random() < 0.2:
        q += '?'  # 惰性后缀
    return q


def gen_pattern(rng):
    parts = []
    for _ in range(rng.randint(0, 6)):
        atom, quantifiable = gen_atom(rng)
        parts.append(atom)
        if quantifiable:
            parts.append(gen_quantifier(rng))
    pattern = ''.join(parts)
    if rng.random() < 0.05:  # 小概率注入元字符，覆盖错误路径
        pos = rng.randint(0, len(pattern))
        pattern = pattern[:pos] + rng.choice('*+?[\\{') + pattern[pos:]
    return pattern


def gen_text(rng):
    return ''.join(rng.choice(INPUT_ALPHABET) for _ in range(rng.randint(0, 10)))


# ------------------------------------------------------------ 固定疑难用例

FIXED_PATTERNS = [
    '', 'a', '.', '^', '$', '^$', '^^', '$$', 'a^b', 'a$b', '^a$', 'a^', '$a',
    '.*', '.+', '.?', 'a*', 'a+', 'a?', 'a*?', 'a+?', 'a??', 'a**', 'a*+', 'a???',
    '*a', '+a', '?a', '^*', '$+', '$^', '^$*',
    'a{2}', 'a{2,}', 'a{1,3}', 'a{0}', 'a{0,}', 'a{,3}', 'a{,}', 'a{2,3}?',
    'a{3,2}', 'a{', 'a{}', 'a{,', 'a{1,2,3}', '{2}', 'a{2}*', 'a*{2}', 'a*{',
    '.*.*b', 'a*a*a*b', 'a?a?a?aaa',
    '\\d', '\\D', '\\w', '\\W', '\\s', '\\S', '\\d+', '\\n', '\\t', '\\r',
    '\\.', '\\*', '\\+', '\\?', '\\[', '\\]', '\\^', '\\$', '\\\\', '\\-', '\\{',
    '\\', 'a\\', '\\q', '\\A', '\\Z', '\\b', '\\B', '\\a', '\\0', '\\1',
    'a*+?', 'a??+', 'a+?+', '\\b*', '\\A+', '[a--b]', '[[]', '[]]',
    '[\\1]', '[\\12]', '[\\123]', '[\\8]', '[\\400]', '[\\377]', '\\07', '\\00', '[\\08]',
    '\\x41', '[\\x41]', '\\x', '\\x4', '\\xzz', '\\u0041', '\\U00000041', '[\\x30-\\x39]',
    '[abc]', '[^abc]', '[a-z]', '[^a-z]', '[z-a]', '[a-]', '[-a]', '[--0]',
    '[]a]', '[^]a]', '[]', '[', '[a', '[^', '[a-a]', '[a-cb-d]', '[a-ca-c]',
    '[\\d]', '[\\D]', '[a-\\d]', '[\\d-z]', '[\\]]', '[\\-]', '[\\^]', '[a^]',
    '[*+?]', '[.]', '[$^]', 'a[', 'a[]',
]

FIXED_TEXTS = [
    '', 'a', 'b', 'aa', 'ab', 'ba', 'aaa', 'abc', 'a\n', '\n', 'a\n\n', ' ',
    'a{2}', '{', '}', ']', '[', '-', '.', '0', '5', 'a-b', '^', '$', '*', '\\',
    'aaaa', 'xyz',
]


def check_fixed():
    bad = 0
    for pattern in FIXED_PATTERNS:
        for text in FIXED_TEXTS:
            if is_counterexample(pattern, text):
                print(f'[固定用例不一致] 模式={pattern!r} 输入={text!r} '
                      f're={ref_verdict(pattern, text)} mine={my_verdict(pattern, text)}')
                bad += 1
    return bad


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    rng = random.Random(20260925)

    bad = check_fixed()
    print(f'固定用例: {len(FIXED_PATTERNS)} 模式 x {len(FIXED_TEXTS)} 输入, '
          f'不一致 {bad} 处')

    for it in range(iterations):
        pattern = gen_pattern(rng)
        text = gen_text(rng)
        if is_counterexample(pattern, text):
            min_pat, min_text = shrink(pattern, text)
            print(f'[随机对拍不一致] 第 {it} 轮')
            print(f'  原始: 模式={pattern!r} 输入={text!r}')
            print(f'  最小反例: 模式={min_pat!r} 输入={min_text!r}')
            print(f'  re={ref_verdict(min_pat, min_text)} '
                  f'mine={my_verdict(min_pat, min_text)}')
            return 1
        if (it + 1) % 5000 == 0:
            print(f'  已完成 {it + 1} 轮随机对拍, 无不一致')
    print(f'随机对拍 {iterations} 轮全部一致')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
