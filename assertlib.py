"""assertlib -- 结构化断言辅助库（仅标准库）。

特点：
- 失败消息精确到键路径 / 下标（如 root.items[2].name）。
- 列表比较基于 difflib 对齐，能识别插入 / 删除（"inserted 1 item(s) at index 3"）。
- 长字符串 / 长集合截断显示时保留完整定位信息（路径 + 绝对偏移）。
- 近似比较可配置绝对 / 相对容差，失败给出实际差值、容差与相对误差。
- 消息完全确定性：同一失败两次输出逐字节一致。
"""

from __future__ import annotations

import difflib
import threading
import time

__all__ = [
    "AssertionFailure",
    "assert_equal",
    "assert_contains",
    "assert_not_contains",
    "assert_almost_equal",
    "assert_raises",
    "assert_completes_within",
]

MAX_DIFFS = 20      # 单次失败最多报告的差异条数
REPR_LIMIT = 120    # 单个值 repr 的最大显示长度
CONTEXT = 20        # 字符串差异处两侧保留的上下文字符数


class AssertionFailure(AssertionError):
    """assertlib 断言失败。消息是确定性的，可安全做快照比较。"""


# ---------------------------------------------------------------- 格式化工具

def _fmt(value, limit=REPR_LIMIT):
    """截断 repr，但保留总长度信息，截断本身可定位。"""
    try:
        text = repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"
    if len(text) > limit:
        return f"{text[:limit]}... <truncated, {len(text)} chars total>"
    return text


def _rec(path, *lines):
    body = "\n".join(f"    {line}" for line in lines)
    return f"x {path}\n{body}"


def _fail(header, records, msg):
    lines = [header + (f": {msg}" if msg else "")]
    lines.extend(records)
    raise AssertionFailure("\n".join(lines))


def _keypath(key):
    if isinstance(key, str) and key.isidentifier():
        return f".{key}"
    return f"[{key!r}]"


# ---------------------------------------------------------------- 深度比较

def _is_plain_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _diff(actual, expected, path, out, budget):
    if len(out) >= budget:
        return

    if type(actual) is not type(expected):
        # int/float 之间按数值比较，其余类型不同即类型不匹配
        if _is_plain_number(actual) and _is_plain_number(expected) and actual == expected:
            return
        out.append(_rec(
            path,
            f"type mismatch: expected {type(expected).__name__}, got {type(actual).__name__}",
            f"expected: {_fmt(expected)}",
            f"actual:   {_fmt(actual)}",
        ))
        return

    if isinstance(expected, dict):
        _diff_dict(actual, expected, path, out, budget)
    elif isinstance(expected, (list, tuple)):
        _diff_sequence(actual, expected, path, out, budget)
    elif isinstance(expected, str):
        _diff_string(actual, expected, path, out)
    elif isinstance(expected, (set, frozenset)):
        _diff_set(actual, expected, path, out)
    elif actual != expected:
        lines = [f"expected: {_fmt(expected)}", f"actual:   {_fmt(actual)}"]
        if isinstance(expected, float):
            lines.append("hint: use assert_almost_equal for float comparison")
        out.append(_rec(path, *lines))


def _diff_dict(actual, expected, path, out, budget):
    expected_keys = set(expected)
    actual_keys = set(actual)
    for key in sorted(expected_keys - actual_keys, key=repr):
        if len(out) >= budget:
            return
        out.append(_rec(path + _keypath(key),
                        f"missing key {key!r}",
                        f"expected: {_fmt(expected[key])}"))
    for key in sorted(actual_keys - expected_keys, key=repr):
        if len(out) >= budget:
            return
        out.append(_rec(path + _keypath(key),
                        f"unexpected key {key!r}",
                        f"actual: {_fmt(actual[key])}"))
    for key in sorted(expected_keys & actual_keys, key=repr):
        _diff(actual[key], expected[key], path + _keypath(key), out, budget)


def _freeze(value):
    """为不可哈希元素提供对齐用代理键。"""
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


def _diff_sequence(actual, expected, path, out, budget):
    kind = type(expected).__name__
    try:
        matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
        opcodes = matcher.get_opcodes()
    except TypeError:  # 含不可哈希元素（如 dict），用代理键对齐
        matcher = difflib.SequenceMatcher(
            None, [_freeze(x) for x in expected], [_freeze(x) for x in actual],
            autojunk=False)
        opcodes = matcher.get_opcodes()

    for tag, i1, i2, j1, j2 in opcodes:
        if len(out) >= budget:
            return
        if tag == "equal":
            continue
        if tag == "insert":
            items = list(actual[j1:j2])
            out.append(_rec(
                path,
                f"{kind}: inserted {j2 - j1} item(s) at index {j1} "
                f"(subsequent items shifted right)",
                f"inserted: {_fmt(items)}",
            ))
        elif tag == "delete":
            items = list(expected[i1:i2])
            out.append(_rec(
                path,
                f"{kind}: deleted {i2 - i1} item(s) at index {i1} "
                f"(subsequent items shifted left)",
                f"deleted: {_fmt(items)}",
            ))
        else:  # replace：按位置配对递归，剩余部分按插入/删除报告
            paired = min(i2 - i1, j2 - j1)
            for k in range(paired):
                _diff(actual[j1 + k], expected[i1 + k], f"{path}[{j1 + k}]", out, budget)
            if i2 - i1 > paired:
                items = list(expected[i1 + paired:i2])
                out.append(_rec(
                    path,
                    f"{kind}: deleted {i2 - i1 - paired} item(s) at index {i1 + paired}",
                    f"deleted: {_fmt(items)}",
                ))
            elif j2 - j1 > paired:
                items = list(actual[j1 + paired:j2])
                out.append(_rec(
                    path,
                    f"{kind}: inserted {j2 - j1 - paired} item(s) at index {j1 + paired}",
                    f"inserted: {_fmt(items)}",
                ))


def _snippet(text, lo, hi, total):
    left = "..." if lo > 0 else ""
    right = "..." if hi < total else ""
    return f"{left}{text[lo:hi]!r}{right}"


def _diff_string(actual, expected, path, out):
    if actual == expected:
        return
    if len(actual) <= REPR_LIMIT and len(expected) <= REPR_LIMIT:
        out.append(_rec(path,
                        f"expected: {_fmt(expected)}",
                        f"actual:   {_fmt(actual)}"))
        return
    common = 0
    limit = min(len(actual), len(expected))
    while common < limit and actual[common] == expected[common]:
        common += 1
    lo = max(0, common - CONTEXT)
    hi_a = min(len(actual), common + CONTEXT)
    hi_e = min(len(expected), common + CONTEXT)
    out.append(_rec(
        path,
        f"string differs at offset {common} "
        f"(expected len {len(expected)}, actual len {len(actual)})",
        f"expected[{lo}:{hi_e}]: {_snippet(expected, lo, hi_e, len(expected))}",
        f"actual  [{lo}:{hi_a}]: {_snippet(actual, lo, hi_a, len(actual))}",
    ))


def _diff_set(actual, expected, path, out):
    missing = sorted(expected - actual, key=repr)
    extra = sorted(actual - expected, key=repr)
    lines = []
    if missing:
        lines.append(f"missing:   {_fmt(missing)}")
    if extra:
        lines.append(f"unexpected: {_fmt(extra)}")
    if lines:
        out.append(_rec(path, *lines))


# ---------------------------------------------------------------- 公开断言

def assert_equal(actual, expected, msg=None, max_diffs=MAX_DIFFS):
    """深度相等断言；失败时输出按路径组织的结构化差异。"""
    out = []
    _diff(actual, expected, "root", out, max_diffs)
    if len(out) >= max_diffs:
        out.append(f"... (further differences suppressed after {max_diffs})")
    if out:
        _fail("assert_equal failed", out, msg)


def assert_contains(container, item, msg=None):
    """包含断言：dict 查键，str 查子串，其余用 __contains__。"""
    if item in container:
        return
    kind = type(container).__name__
    size = (f"len {len(container)}" if hasattr(container, "__len__")
            else "no len()")
    _fail("assert_contains failed", [
        _rec("root",
             f"item not found: {_fmt(item)}",
             f"container ({kind}, {size}): {_fmt(container)}"),
    ], msg)


def assert_not_contains(container, item, msg=None):
    if item not in container:
        return
    _fail("assert_not_contains failed", [
        _rec("root",
             f"item unexpectedly present: {_fmt(item)}",
             f"container ({type(container).__name__}): {_fmt(container)}"),
    ], msg)


def assert_almost_equal(actual, expected, *, tol=1e-9, rel=None, msg=None):
    """近似相等断言。tol 为绝对容差，rel 为相对容差（相对 expected）。"""
    if not _is_plain_number(actual) or not _is_plain_number(expected):
        _fail("assert_almost_equal failed", [
            _rec("root",
                 f"type mismatch: both sides must be numbers "
                 f"(expected: {type(expected).__name__}, "
                 f"actual: {type(actual).__name__})",
                 f"expected: {_fmt(expected)}",
                 f"actual:   {_fmt(actual)}"),
        ], msg)
    diff = abs(actual - expected)
    rel_err = diff / abs(expected) if expected else (0.0 if diff == 0 else float("inf"))
    ok = diff <= tol or (rel is not None and diff <= rel * abs(expected))
    if ok:
        return
    lines = [
        f"expected:  {expected!r}",
        f"actual:    {actual!r}",
        f"abs diff:  {diff!r}",
        f"tolerance: {tol!r} (abs)" + (f", {rel!r} (rel)" if rel is not None else ""),
        f"rel error: {rel_err!r}",
    ]
    _fail("assert_almost_equal failed", [_rec("root", *lines)], msg)


class _RaisesContext:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            _fail("assert_raises failed", [
                _rec("root", f"expected {self.exc_type.__name__}, but nothing was raised"),
            ], None)
        if isinstance(exc, self.exc_type):
            self.exception = exc
            return True
        _fail("assert_raises failed", [
            _rec("root",
                 f"expected {self.exc_type.__name__}, but {type(exc).__name__} was raised",
                 f"actual: {type(exc).__name__}: {exc}"),
        ], None)


def assert_raises(exc_type, fn=None, *args, **kwargs):
    """抛错断言。支持 assert_raises(E, fn, *args) 与 with 上下文两种用法。"""
    if fn is None:
        return _RaisesContext(exc_type)
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:
        _fail("assert_raises failed", [
            _rec("root",
                 f"expected {exc_type.__name__}, but {type(exc).__name__} was raised",
                 f"actual: {type(exc).__name__}: {exc}"),
        ], None)
    _fail("assert_raises failed", [
        _rec("root", f"expected {exc_type.__name__}, but nothing was raised"),
    ], None)


def assert_completes_within(seconds, fn, *args, msg=None, **kwargs):
    """超时断言：fn 必须在 seconds 秒内完成，否则失败（不等待其结束）。"""
    box = {}

    def run():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 原样向上传递
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        _fail("assert_completes_within failed", [
            _rec("root",
                 f"did not complete within {seconds:.3f}s (limit exceeded, "
                 f"function still running)"),
        ], msg)
    if "error" in box:
        raise box["error"]
    return box.get("result")
