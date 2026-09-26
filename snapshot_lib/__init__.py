"""会话状态快照与恢复库（仅标准库）。

详见 snapshot_lib.snapshot 的模块文档。
"""
from .snapshot import (
    Snapshot,
    SnapshotError,
    CorruptedSnapshotError,
    UnknownVersionError,
    save_state,
    load_state,
    stage_state,
    commit_staged,
    load_raw,
    upgrade_state,
    CURRENT_VERSION,
    encode_snapshot,
    decode_snapshot,
    FORMAT_VERSION,
    cleanup_stale_tempfiles,
)

__all__ = [
    "Snapshot",
    "SnapshotError",
    "CorruptedSnapshotError",
    "UnknownVersionError",
    "save_state",
    "load_state",
    "stage_state",
    "commit_staged",
    "load_raw",
    "upgrade_state",
    "CURRENT_VERSION",
    "encode_snapshot",
    "decode_snapshot",
    "FORMAT_VERSION",
    "cleanup_stale_tempfiles",
]
