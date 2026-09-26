"""Session state snapshot & recovery library (Python 3, stdlib only).

On-disk format (single file, byte layout)::

    +------------------+-------------------+-----------------+----------+
    | MAGIC (8 bytes)  | header_len (4B BE)| header (JSON)   | payload  |
    +------------------+-------------------+-----------------+----------+

- MAGIC: b"SNAP0001", identifies the envelope (independent of state version).
- header: JSON object {"version", "created_at", "payload_len", "sha256"}.
  ``sha256`` is the hex digest of the payload (integrity check).
- payload: canonical JSON (sort_keys, utf-8) of the state.

Guarantees:
- Atomic write: serialize -> temp file in same dir -> fsync -> os.replace
  -> fsync directory. A reader only ever sees the old or the new file.
- Failure isolation: load/restore never mutates the snapshot file.
- Error taxonomy: CorruptedSnapshotError (integrity/format) vs
  UnknownVersionError (version we cannot read/upgrade).
- Versioned upgrades: UPGRADERS[v] upgrades v -> v+1; every upgrader is
  idempotent, so the whole chain is idempotent and re-runnable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Callable, Dict, Tuple

MAGIC = b"SNAP0001"
CURRENT_VERSION = 3
MIN_VERSION = 1

_TMP_PREFIX = ".snap-"
_TMP_SUFFIX = ".tmp"
_tmp_re = re.compile(r"^\.snap-.*\.tmp$")


class SnapshotError(Exception):
    """Base class for all snapshot errors."""


class CorruptedSnapshotError(SnapshotError):
    """The snapshot content is damaged (bad magic, truncation, checksum...)."""


class UnknownVersionError(SnapshotError):
    """The snapshot version is not known to this build of the library."""


# ---------------------------------------------------------------------------
# Version upgrades (idempotent: upgrade(upgrade(x)) == upgrade(x))
# ---------------------------------------------------------------------------

def _upgrade_1_to_2(state: Dict[str, Any]) -> Dict[str, Any]:
    """v1 -> v2: introduce the ``meta`` object alongside ``history``."""
    upgraded = dict(state)
    upgraded.setdefault("meta", {})
    return upgraded


def _upgrade_2_to_3(state: Dict[str, Any]) -> Dict[str, Any]:
    """v2 -> v3: ``meta`` gains ``tags`` (list) and ``schema`` marker."""
    upgraded = dict(state)
    meta = dict(upgraded.get("meta") or {})
    meta.setdefault("tags", [])
    meta.setdefault("schema", 3)
    upgraded["meta"] = meta
    return upgraded


UPGRADERS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    1: _upgrade_1_to_2,
    2: _upgrade_2_to_3,
}


def upgrade(version: int, state: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade ``state`` from ``version`` to CURRENT_VERSION.

    Idempotent end to end: each step only fills in missing fields, so
    applying the chain to an already-current state is a no-op.
    """
    if version < MIN_VERSION or version > CURRENT_VERSION:
        raise UnknownVersionError(
            f"cannot upgrade from version {version!r} "
            f"(supported: {MIN_VERSION}..{CURRENT_VERSION})"
        )
    while version < CURRENT_VERSION:
        upgrader = UPGRADERS.get(version)
        if upgrader is None:
            raise UnknownVersionError(f"no upgrade path from version {version}")
        state = upgrader(state)
        version += 1
    return state


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _serialize(state: Any) -> bytes:
    return json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _build_blob(state: Any, version: int) -> bytes:
    payload = _serialize(state)
    header = {
        "version": version,
        "created_at": time.time(),
        "payload_len": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    header_bytes = json.dumps(header, sort_keys=True).encode("ascii")
    return MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes + payload


def _parse_blob(data: bytes) -> Tuple[Dict[str, Any], bytes]:
    """Validate envelope + integrity. Raises CorruptedSnapshotError."""
    if len(data) < len(MAGIC) + 4:
        raise CorruptedSnapshotError(
            f"snapshot too small ({len(data)} bytes); truncated header?"
        )
    if data[: len(MAGIC)] != MAGIC:
        raise CorruptedSnapshotError("bad magic; not a snapshot file")
    header_len = int.from_bytes(data[len(MAGIC): len(MAGIC) + 4], "big")
    header_end = len(MAGIC) + 4 + header_len
    if len(data) < header_end:
        raise CorruptedSnapshotError(
            f"truncated header: need {header_end} bytes, have {len(data)}"
        )
    try:
        header = json.loads(data[len(MAGIC) + 4: header_end].decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptedSnapshotError(f"header is not valid JSON: {exc}") from exc
    payload = data[header_end:]
    expected_len = header.get("payload_len")
    if expected_len != len(payload):
        raise CorruptedSnapshotError(
            f"payload length mismatch: header says {expected_len}, "
            f"file has {len(payload)}"
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != header.get("sha256"):
        raise CorruptedSnapshotError(
            f"checksum mismatch: expected {header.get('sha256')}, got {digest}"
        )
    return header, payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fsync_dir(dir_path: str) -> None:
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path: str, state: Any, version: int = CURRENT_VERSION) -> None:
    """Atomically write ``state`` to ``path``.

    Writes a uniquely-named temp file in the same directory (safe for
    concurrent writers), fsyncs it, then os.replace()s it onto ``path``.
    On any failure the temp file is removed and the previous snapshot
    (if any) is left untouched.
    """
    blob = _build_blob(state, version)
    dir_path = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(
        prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX, dir=dir_path
    )
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(blob)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(dir_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_raw(path: str) -> Tuple[int, Any]:
    """Read and verify the snapshot; return (version, state) un-upgraded.

    Raises CorruptedSnapshotError for integrity/format problems and
    UnknownVersionError for versions outside the supported range.
    Never modifies the file.
    """
    with open(path, "rb") as snap_file:
        data = snap_file.read()
    header, payload = _parse_blob(data)
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CorruptedSnapshotError(f"invalid version field: {version!r}")
    if version < MIN_VERSION or version > CURRENT_VERSION:
        raise UnknownVersionError(
            f"snapshot version {version} is not supported by this build "
            f"(supported: {MIN_VERSION}..{CURRENT_VERSION})"
        )
    try:
        state = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptedSnapshotError(f"payload is not valid JSON: {exc}") from exc
    return version, state


def load(path: str) -> Any:
    """Load the snapshot at ``path``, upgrading to CURRENT_VERSION.

    The on-disk file is never modified, whether loading succeeds or fails.
    """
    version, state = load_raw(path)
    return upgrade(version, state)


def restore(path: str, default: Any = None) -> Tuple[Any, str]:
    """Best-effort recovery. Returns (state, status_message).

    On any failure the original file is preserved byte-for-byte and the
    reason is reported in the status message; ``default`` is returned.
    """
    try:
        return load(path), "ok"
    except FileNotFoundError:
        return default, f"no snapshot at {path}; starting fresh"
    except UnknownVersionError as exc:
        return default, f"unknown snapshot version, kept original file: {exc}"
    except CorruptedSnapshotError as exc:
        return default, f"snapshot corrupted, kept original file: {exc}"
    except OSError as exc:
        return default, f"I/O error reading snapshot, kept original file: {exc}"


def cleanup_stale_temps(path: str) -> int:
    """Remove leftover temp files from interrupted saves near ``path``.

    Returns the number of files removed. Never touches ``path`` itself.
    """
    dir_path = os.path.dirname(os.path.abspath(path))
    removed = 0
    for name in os.listdir(dir_path):
        if _tmp_re.match(name):
            try:
                os.unlink(os.path.join(dir_path, name))
                removed += 1
            except OSError:
                pass
    return removed
