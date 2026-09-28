"""linepatch: parse and apply unified-diff style line patches (stdlib only)."""
from .applier import (
    ApplyConfig,
    apply_file_patch,
    apply_patch,
    reverse_file_patch,
    reverse_patch,
)
from .errors import (
    HunkConflictError,
    MissingTargetError,
    PatchApplyError,
    PatchParseError,
)
from .model import FilePatch, Hunk, Patch, PatchLine
from .parser import parse_patch

__all__ = [
    "ApplyConfig",
    "apply_file_patch",
    "apply_patch",
    "reverse_file_patch",
    "reverse_patch",
    "HunkConflictError",
    "MissingTargetError",
    "PatchApplyError",
    "PatchParseError",
    "FilePatch",
    "Hunk",
    "Patch",
    "PatchLine",
    "parse_patch",
]
