"""会话状态快照与恢复。

文件格式（UTF-8 文本，body 为一行 JSON）::

    # SESSNAP/1
    version: 3
    length: 1234
    sha256: <payload 的 sha256 hex>
    created: 2026-09-26T10:00:00Z
    <空行>
    <json payload>

设计要点
--------
* 写入原子：数据先落盘到同目录临时文件并 fsync，再加锁用
  ``os.replace`` 原子替换目标文件；目标目录也 fsync，保证崩溃后可恢复。
* 完整性：校验 sha256（覆盖 payload）+ length 双校验。
* 恢复只读，失败时绝不覆盖/删除原文件，异常区分：
  - :class:`CorruptedSnapshotError`：内容损坏（头截断、校验不符等）
  - :class:`UnknownVersionError`：内容完整但版本号不认识
* 升级：按版本链顺序应用迁移函数；迁移函数要求幂等，整个升级过程
  可重复执行且结果一致（重复升级同一对象结果不变）。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import glob
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

try:  # 进程间互斥锁（POSIX）。无 fcntl 的平台降级为不加锁。
    import fcntl

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - 非 POSIX
    fcntl = None  # type: ignore
    _HAVE_FLOCK = False

FORMAT_VERSION = 1
CURRENT_VERSION = 3
MAGIC = "# SESSNAP"
HEADER_END = "\n\n"

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class SnapshotError(Exception):
    """快照相关错误基类。"""


class CorruptedSnapshotError(SnapshotError):
    """快照内容损坏（格式错误、头截断、长度/校验不符）。"""


class UnknownVersionError(SnapshotError):
    """快照完整、能解析，但版本号未知、无法迁移。"""

    def __init__(self, version: int, known: Tuple[int, ...], path: str):
        self.version = version
        self.known_versions = known
        self.path = path
        super().__init__(
            f"unknown snapshot version {version!r} (known: {list(known)}) in {path}"
        )


@dataclass
class Snapshot:
    """加载/升级后的快照。"""

    state: Dict[str, Any]
    version: int
    created: str
    upgraded_from: int | None = None


# ---------------------------------------------------------------------------
# 版本迁移（幂等）
# ---------------------------------------------------------------------------

_MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def _migration(from_version: int) -> Callable[[Callable[..., Dict[str, Any]]], Callable[..., Dict[str, Any]]]:
    def deco(fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        _MIGRATIONS[from_version] = fn
        return fn

    return deco


@_migration(1)
def _v1_to_v2(state: Dict[str, Any]) -> Dict[str, Any]:
    """v1 -> v2：把裸会话字段包进 session，新增 notes 列表。

    对已经是 v2 结构的输入不做任何改动（幂等）。
    """
    if "session" in state and isinstance(state.get("session"), dict) and "notes" in state:
        return state
    session = {
        "user_id": state.get("user_id"),
        "messages": state.get("messages", []),
    }
    return {"session": session, "notes": []}


@_migration(2)
def _v2_to_v3(state: Dict[str, Any]) -> Dict[str, Any]:
    """v2 -> v3：notes 从字符串列表升级为 {id, text} 对象列表。

    已经是对象形态的元素保持不变（幂等）。
    """
    notes = state.get("notes", [])
    upgraded = [
        {"id": idx, "text": note} if isinstance(note, str) else note
        for idx, note in enumerate(notes)
    ]
    state["notes"] = upgraded
    return state


def upgrade_state(
    state: Dict[str, Any],
    version: int,
    migrations: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] | None = None,
    target_version: int = CURRENT_VERSION,
    path: str = "<memory>",
) -> Tuple[Dict[str, Any], int]:
    """把 ``state`` 从 ``version`` 沿迁移链升级到 ``target_version``。

    可重复执行：对已处于目标版本的状态原样返回；每步迁移幂等，
    因此连续调用多次结果一致。返回 (新状态, 新版本)。
    """
    migs = _MIGRATIONS if migrations is None else migrations
    known = tuple(sorted({1, *migs.keys(), target_version}))
    if version < 1 or version > target_version:
        raise UnknownVersionError(version, known, path=path)
    current = version
    # 上限保护，防止迁移链成环导致死循环。
    for _ in range(target_version + 1):
        if current == target_version:
            return state, current
        step = migs.get(current)
        if step is None:
            raise UnknownVersionError(current, known, path=path)
        state = step(state)
        current += 1
    raise SnapshotError("migration chain exceeded maximum length")  # pragma: no cover


# ---------------------------------------------------------------------------
# 编码 / 解码
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_snapshot(
    state: Dict[str, Any],
    version: int = CURRENT_VERSION,
    created: str | None = None,
) -> bytes:
    """把状态编码为快照字节。"""
    payload = _canonical_json({"version": version, "state": state})
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if created is None:
        created = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"{MAGIC}/{FORMAT_VERSION}\n"
        f"version: {version}\n"
        f"length: {len(payload_bytes)}\n"
        f"sha256: {digest}\n"
        f"created: {created}\n"
        f"\n"
    )
    return header.encode("utf-8") + payload_bytes


def _parse_header(raw: bytes, path: str) -> Tuple[Dict[str, str], bytes]:
    if not raw.startswith(MAGIC.encode("utf-8")):
        raise CorruptedSnapshotError(f"bad magic header (not a snapshot file): {path}")

    end = raw.find(HEADER_END.encode("utf-8"))
    if end == -1:
        # 没有“头结束”标记：快照头被截断（或 body 起始符缺失）。
        raise CorruptedSnapshotError(f"snapshot header truncated (no header terminator): {path}")

    try:
        header_text = raw[:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptedSnapshotError(f"snapshot header is not valid UTF-8: {path}: {exc}") from exc
    body = raw[end + len(HEADER_END):]
    lines = header_text.split("\n")

    first = lines[0].split("/", 1)
    if len(first) != 2 or first[0] != MAGIC:
        raise CorruptedSnapshotError(f"bad magic header: {path}")
    try:
        fmt_ver = int(first[1])
    except ValueError:
        raise CorruptedSnapshotError(f"bad format version in header: {path}") from None
    if fmt_ver != FORMAT_VERSION:
        raise CorruptedSnapshotError(
            f"unsupported snapshot format version {fmt_ver}: {path}"
        )

    fields: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            raise CorruptedSnapshotError(f"malformed header line {line!r}: {path}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    for required in ("version", "length", "sha256"):
        if required not in fields:
            raise CorruptedSnapshotError(f"missing header field {required!r}: {path}")
    fields["_format_version"] = str(fmt_ver)
    return fields, body


def decode_snapshot(raw: bytes, path: str = "<bytes>") -> Tuple[Dict[str, Any], int, str]:
    """解码并校验快照字节，返回 (state, version, created)。

    先做完整性校验，再判定版本是否可识别——因此“版本不认识”只在
    内容完整可信时才会抛出。
    """
    fields, body = _parse_header(raw, path)

    try:
        declared_length = int(fields["length"])
    except ValueError:
        raise CorruptedSnapshotError(f"bad length field: {path}") from None
    if declared_length < 0:
        raise CorruptedSnapshotError(f"negative length field: {path}")

    if len(body) != declared_length:
        raise CorruptedSnapshotError(
            f"length mismatch: header says {declared_length}, body is {len(body)}: {path}"
        )

    actual = hashlib.sha256(body).hexdigest()
    declared_hash = fields["sha256"].lower()
    try:
        int(declared_hash, 16)
    except ValueError:
        raise CorruptedSnapshotError(f"bad sha256 field: {path}") from None
    if len(declared_hash) != 64:
        raise CorruptedSnapshotError(f"bad sha256 field length: {path}")
    if actual != declared_hash:
        raise CorruptedSnapshotError(
            f"sha256 mismatch: expected {declared_hash}, got {actual}: {path}"
        )

    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptedSnapshotError(f"payload is not valid JSON: {path}: {exc}") from exc
    if not isinstance(envelope, dict) or "version" not in envelope or "state" not in envelope:
        raise CorruptedSnapshotError(f"payload missing version/state: {path}")

    payload_version = envelope["version"]
    header_version = fields["version"]
    if str(payload_version) != header_version:
        raise CorruptedSnapshotError(
            f"version mismatch: header {header_version} vs payload {payload_version}: {path}"
        )
    if not isinstance(payload_version, int) or payload_version < 1:
        raise CorruptedSnapshotError(f"bad payload version: {path}")
    if not isinstance(envelope["state"], dict):
        raise CorruptedSnapshotError(f"payload state is not an object: {path}")

    return envelope["state"], payload_version, fields.get("created", "")


def load_raw(path: str) -> Tuple[Dict[str, Any], int, str]:
    """读取并校验快照，不做版本升级。只读，不写不改任何文件。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    return decode_snapshot(raw, path=path)


# ---------------------------------------------------------------------------
# 原子写入（两阶段：stage -> commit）
# ---------------------------------------------------------------------------

_TMP_SUFFIX = ".tmp.sessnap"


def _stale_glob(path: str) -> str:
    base = os.path.basename(path)
    return os.path.join(
        os.path.dirname(os.path.abspath(path)) or ".",
        f".sessnap-{base}-*{_TMP_SUFFIX}",
    )


def _temp_path(path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    prefix = f".sessnap-{os.path.basename(path)}-"
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=_TMP_SUFFIX, dir=d)
    os.close(fd)
    return tmp


@contextlib.contextmanager
def _file_lock(path: str):
    """对同目录下的锁文件加进程间排他锁；无 fcntl 时直接降级。"""
    if not _HAVE_FLOCK:
        yield
        return
    lock_path = path + ".lock"
    with open(lock_path, "a+b") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        # 某些文件系统不支持目录 fsync，尽力而为。
        pass
    finally:
        os.close(fd)


def stage_state(path: str, data: bytes) -> str:
    """把已编码字节写入同目录临时文件并 fsync，返回临时文件路径。

    模拟“写入到一半”时可只调用本函数而不调用 :func:`commit_staged`，
    从而在磁盘上留下临时文件残留。
    """
    tmp = _temp_path(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    return tmp


def _sweep_stale_tempfiles(path: str, keep: str | None = None) -> None:
    """删除本目录里其它（崩溃进程残留的）临时文件。仅在持锁时调用。"""
    for stale in glob.glob(_stale_glob(path)):
        if keep is not None and os.path.abspath(stale) == os.path.abspath(keep):
            continue
        with contextlib.suppress(OSError):
            os.remove(stale)


def commit_staged(path: str, tmp: str) -> None:
    """持锁把临时文件原子替换为目标文件，并 fsync 目录。

    持锁期间顺带清扫同目录中崩溃/写入中断残留的其它临时文件；
    锁保证这些文件不可能属于正在写的其它进程。
    """
    with _file_lock(path):
        _sweep_stale_tempfiles(path, keep=tmp)
        os.replace(tmp, path)
        _fsync_dir(path)


def save_state(
    path: str,
    state: Dict[str, Any],
    version: int = CURRENT_VERSION,
    created: str | None = None,
) -> str:
    """原子保存快照，返回目标路径。

    整个“清扫残留 -> 写临时文件 -> 替换”在进程间排他锁内完成。
    失败时目标路径保持原状（要么是旧的完整快照，要么不存在），
    本进程产生的临时文件会被清理；崩溃留下的残留由
    :func:`cleanup_stale_tempfiles` 或下一次保存清理。
    """
    data = encode_snapshot(state, version=version, created=created)
    with _file_lock(path):
        _sweep_stale_tempfiles(path)
        tmp = _temp_path(path)
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _fsync_dir(path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    return path


def cleanup_stale_tempfiles(path: str) -> list[str]:
    """清理目标快照路径对应的残留临时文件（崩溃/写入中断现场）。

    只删除本库命名、且 basename 与目标快照对应的 ``*.tmp.sessnap``，
    绝不触碰正式快照或其它快照的临时文件。
    """
    removed = []
    for tmp in glob.glob(_stale_glob(path)):
        try:
            os.remove(tmp)
            removed.append(tmp)
        except FileNotFoundError:
            pass
    return removed


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def load_state(
    path: str,
    target_version: int = CURRENT_VERSION,
    migrations: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] | None = None,
) -> Snapshot:
    """恢复快照：校验 -> （必要时）升级到当前结构。

    整个过程只读；任何失败都抛出 :class:`SnapshotError` 子类，
    原文件不被覆盖或删除。
    """
    state, version, created = load_raw(path)
    upgraded_from = None
    if version != target_version:
        upgraded_from = version
        state, version = upgrade_state(
            state,
            version,
            migrations=migrations,
            target_version=target_version,
            path=path,
        )
    elif migrations is not None:
        # 允许调用方注入自定义迁移表时仍做合法性检查。
        known = tuple(sorted({1, *(migrations or {}).keys(), target_version}))
        if version not in known:
            raise UnknownVersionError(version, known, path=path)
    return Snapshot(state=state, version=version, created=created, upgraded_from=upgraded_from)
