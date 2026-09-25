"""versioning - 语义化版本解析、严格全序比较与范围约束匹配（仅标准库）。

比较规则（严格全序，文档化的确定性规则）：
  1. 先按 (major, minor, patch) 数值比较；缺省的 minor/patch 视为 0。
  2. 预发布版本 < 同核心号的正式版本（1.0.0-alpha < 1.0.0）。
  3. 预发布标识逐段比较：数字标识 < 字母数字标识；数字按数值、字母数字按 ASCII；
     前缀相同则标识数少者更小。
  4. 构建元数据（+build）不参与上述优先级比较；仅当且仅当前述全部相等时，
     作为最终平局裁决（按标识元组字典序），以保证「不同字符串 => 可比出大小」
     的严格全序，杜绝排序与逐对比较不一致。

范围表达式（空白分隔的 AND 合取，空串或 `*` 匹配任意）：
  >=1.2.3 <2.0.0   上下界（> >= < <=）
  1.2.3 / =1.2.3   精确匹配
  1.2.* / 1.x / 1  通配符（部分版本号按通配展开，如 1.2 => >=1.2.0 <1.3.0）
  ^1.2.3           主次兼容：>=1.2.3 <2.0.0（0.x 时锁到下一个非零段）
  ~1.2.3           修订兼容：>=1.2.3 <1.3.0
  !1.2.3           取反（不等于，仅允许精确版本）

边界行为：
  - 等于下界/上界：闭区间端点（>=、<=）包含边界，开区间（>、<）不包含。
  - 区间为空（如 >2.0.0 <1.0.0）：合法表达式，匹配结果恒为 False，不报错。
  - include_prerelease=False（默认）：带预发布标识的版本一律不匹配；
    True 时按正常比较参与匹配。
"""

from __future__ import annotations

import re
from functools import total_ordering

__all__ = [
    "Version",
    "Range",
    "VersionError",
    "RangeError",
    "parse_version",
    "parse_range",
]

_IDENT_RE = re.compile(r"^[0-9A-Za-z-]+$")
_NUMERIC_RE = re.compile(r"^(0|[1-9][0-9]*)$")


class VersionError(ValueError):
    """非法版本串。position 为出错字符的下标（0 基）。"""

    def __init__(self, message: str, source: str, position: int):
        self.source = source
        self.position = position
        caret = " " * position + "^"
        super().__init__(f"{message}\n  {source}\n  {caret}")


class RangeError(ValueError):
    """非法范围表达式。position 为出错 token 的起始下标（0 基）。"""

    def __init__(self, message: str, source: str, position: int):
        self.source = source
        self.position = position
        caret = " " * position + "^"
        super().__init__(f"{message}\n  {source}\n  {caret}")


def _parse_identifiers(text: str, source: str, offset: int, kind: str) -> tuple:
    idents = text.split(".")
    for i, ident in enumerate(idents):
        pos = offset + sum(len(p) + 1 for p in idents[:i])
        if not ident:
            raise VersionError(f"{kind}标识为空", source, pos)
        if not _IDENT_RE.match(ident):
            bad = next(j for j, ch in enumerate(ident)
                       if not (ch.isalnum() or ch == "-"))
            raise VersionError(
                f"{kind}标识含非法字符: {ident[bad]!r}", source, pos + bad)
        if kind == "预发布" and ident.isdigit() and not _NUMERIC_RE.match(ident):
            raise VersionError(f"预发布数字标识不允许前导零: {ident!r}", source, pos)
    return tuple(idents)


@total_ordering
class Version:
    """已解析的版本号。比较为严格全序，见模块 docstring。"""

    __slots__ = ("major", "minor", "patch", "prerelease", "build", "_key")

    def __init__(self, major, minor, patch, prerelease=(), build=()):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.build = tuple(build)
        self._key = self._make_key()

    def _make_key(self):
        if self.prerelease:
            pre = tuple(
                (0, int(i), "") if i.isdigit() else (1, 0, i)
                for i in self.prerelease
            )
            pre_key = (0, pre)
        else:
            pre_key = (1,)  # 正式版 > 任何预发布版
        # 构建元数据仅作最终平局裁决，保证严格全序。
        build_key = tuple(
            (0, int(i)) if i.isdigit() else (1, i) for i in self.build
        )
        return (self.major, self.minor, self.patch, pre_key, build_key)

    # -- 比较：全部经由同一 _key，排序与逐对比较天然一致 --
    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._key < other._key

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):
        return f"Version({str(self)!r})"

    def __str__(self):
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + ".".join(self.build)
        return s

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)


def parse_version(text: str) -> Version:
    """解析版本串；非法输入抛 VersionError 并指出位置。"""
    if not isinstance(text, str):
        raise TypeError("版本必须是字符串")
    source = text
    s = text.strip()
    if not s:
        raise VersionError("版本串为空", source, 0)
    base_off = (len(text) - len(text.lstrip())) if text else 0

    # 核心号：major[.minor[.patch]]，缺省补 0
    m = re.match(r"(0|[1-9][0-9]*)", s)
    if not m:
        raise VersionError("主版本号缺失或含前导零", source, base_off)
    if m.end() < len(s) and s[m.end()].isdigit():
        raise VersionError("主版本号不允许前导零", source, base_off)
    parts = [int(m.group(0))]
    i = m.end()
    while i < len(s) and s[i] == "." and len(parts) < 3:
        m = re.match(r"\.(0|[1-9][0-9]*)", s[i:])
        if not m:
            raise VersionError("次/修订号缺失或含前导零", source, base_off + i + 1)
        parts.append(int(m.group(1)))
        i += m.end()
    while len(parts) < 3:
        parts.append(0)

    prerelease: tuple = ()
    build: tuple = ()
    if i < len(s) and s[i] == "-":
        j = i + 1
        k = j
        while k < len(s) and s[k] != "+":
            k += 1
        prerelease = _parse_identifiers(s[j:k], source, base_off + j, "预发布")
        i = k
    if i < len(s) and s[i] == "+":
        j = i + 1
        build = _parse_identifiers(s[j:], source, base_off + j, "构建元数据")
        i = len(s)
    if i != len(s):
        raise VersionError(f"此处出现意外字符: {s[i]!r}", source, base_off + i)
    return Version(parts[0], parts[1], parts[2], prerelease, build)


# ---------------------------------------------------------------- 范围匹配

_WILDCARDS = {"*", "x", "X"}
_OP_RE = re.compile(r"(>=|<=|>|<|=|!|\^|~)?(.*)")


def _split_core(token: str):
    """把 token 的版本部分拆成 核心段 / 预发布 / 构建 三段原始字符串。"""
    main, _, build = token.partition("+")
    core, _, pre = main.partition("-")
    return core, pre, build


class _Comparator:
    """单个比较子句：对 Version 的谓词。"""

    __slots__ = ("desc", "pred")

    def __init__(self, desc, pred):
        self.desc = desc
        self.pred = pred

    def __call__(self, v: Version) -> bool:
        return self.pred(v)


def _expand_partial(core: str, source: str, pos: int):
    """解析核心段（允许通配/缺省），返回 (nums, wildcard_level)。

    nums 为已确定的数值段列表；wildcard_level 为首个通配/缺省段的层级
    （1=major 通配, 2=minor 通配, 3=patch 通配, 4=完全精确）。
    """
    segs = core.split(".")
    if len(segs) > 3:
        raise RangeError("版本段数超过 3 段", source, pos)
    nums = []
    level = 4
    for idx, seg in enumerate(segs):
        if seg in _WILDCARDS:
            if idx != len(segs) - 1:
                raise RangeError("通配符只能出现在最后一段", source, pos)
            level = idx + 1
            break
        if not _NUMERIC_RE.match(seg):
            raise RangeError(f"非法数值段: {seg!r}", source, pos)
        nums.append(int(seg))
    else:
        # 完全写满 3 段为精确(level=4)，否则首个缺省段即通配层级
        level = 4 if len(segs) == 3 else len(segs) + 1
    if level == 1:
        nums = []
    return nums, level


def _bump(nums, level):
    """对 (nums 补零到 3 段) 在 level 的前一段进位，得到开区间上界。"""
    full = list(nums) + [0] * (3 - len(nums))
    idx = level - 2
    full[idx] += 1
    for k in range(idx + 1, 3):
        full[k] = 0
    return tuple(full)


def _compile_token(token: str, source: str, pos: int) -> _Comparator:
    if token in _WILDCARDS:
        return _Comparator("*", lambda v: True)

    m = _OP_RE.match(token)
    op, body = m.group(1) or "", m.group(2)
    op_pos = pos + (len(token) - len(body))
    if not body:
        raise RangeError("操作符后缺少版本", source, pos)

    core, pre, build = _split_core(body)
    if build:
        raise RangeError("范围表达式中不允许构建元数据", source, op_pos)
    if pre:
        if op in ("^", "~", "!"):
            raise RangeError(f"操作符 {op!r} 不允许带预发布标识", source, pos)
        # 带预发布的比较必须完全精确
        for seg in core.split("."):
            if seg in _WILDCARDS:
                raise RangeError("预发布比较不允许通配符", source, op_pos)
        try:
            exact = parse_version(body)
        except VersionError as e:
            raise RangeError(f"非法版本: {e.args[0].splitlines()[0]}", source, op_pos)
        return _make_simple(op, exact, source, pos)

    nums, level = _expand_partial(core, source, op_pos)

    if op == "!":
        if level != 4:
            raise RangeError("取反 ! 仅允许精确版本", source, pos)
        target = Version(*nums)
        return _Comparator(f"!{target}", lambda v, t=target: v != t)

    if op == "^":
        if level == 1:
            return _Comparator("^*", lambda v: True)
        full = list(nums) + [0] * (3 - len(nums))
        low = Version(*full)
        # 兼容上界：第一个非零段进位；全零则锁到最右已确定段
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
        high = Version(*hi)
        return _Comparator(
            f">={low} <{high}",
            lambda v, lo=low, hi=high: lo <= v < hi,
        )

    if op == "~":
        if level == 1:
            return _Comparator("~*", lambda v: True)
        full = list(nums) + [0] * (3 - len(nums))
        low = Version(*full)
        hi = (full[0] + 1, 0, 0) if len(nums) <= 1 else (full[0], full[1] + 1, 0)
        high = Version(*hi)
        return _Comparator(
            f">={low} <{high}",
            lambda v, lo=low, hi=high: lo <= v < hi,
        )

    # 普通比较符 / 精确 / 通配
    if level < 4 and op in ("", "="):
        low = Version(*(list(nums) + [0] * (3 - len(nums))))
        high = Version(*_bump(nums, level))
        return _Comparator(
            f">={low} <{high}",
            lambda v, lo=low, hi=high: lo <= v < hi,
        )
    if level < 4:
        raise RangeError(f"操作符 {op!r} 不允许通配/部分版本", source, pos)

    exact = Version(*nums)
    return _make_simple(op, exact, source, pos)


def _make_simple(op: str, target: Version, source: str, pos: int) -> _Comparator:
    table = {
        "": ("={t}", lambda v, t: v == t),
        "=": ("={t}", lambda v, t: v == t),
        ">": (">{t}", lambda v, t: v > t),
        ">=": (">={t}", lambda v, t: v >= t),
        "<": ("<{t}", lambda v, t: v < t),
        "<=": ("<={t}", lambda v, t: v <= t),
        "!": ("!{t}", lambda v, t: v != t),
    }
    if op not in table:
        raise RangeError(f"未知操作符: {op!r}", source, pos)
    desc, fn = table[op]
    return _Comparator(desc.format(t=target), lambda v, t=target, f=fn: f(v, t))


class Range:
    """范围表达式（多个比较子句的 AND 合取）。"""

    __slots__ = ("source", "comparators")

    def __init__(self, source: str, comparators):
        self.source = source
        self.comparators = tuple(comparators)

    def matches(self, version, include_prerelease: bool = False) -> bool:
        """判断版本是否满足范围。

        include_prerelease=False（默认）时，带预发布标识的版本一律不匹配。
        空区间合法，结果恒为 False。
        """
        if isinstance(version, str):
            version = parse_version(version)
        if version.is_prerelease and not include_prerelease:
            return False
        return all(c(version) for c in self.comparators)

    def __contains__(self, version) -> bool:
        return self.matches(version)

    def __repr__(self):
        return f"Range({self.source!r})"


_TOKEN_RE = re.compile(r"\S+")


def parse_range(text: str) -> Range:
    """解析范围表达式；非法输入抛 RangeError 并指出位置。"""
    if not isinstance(text, str):
        raise TypeError("范围必须是字符串")
    comparators = []
    for m in _TOKEN_RE.finditer(text):
        comparators.append(_compile_token(m.group(0), text, m.start()))
    return Range(text, comparators)
