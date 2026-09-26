"""linepatch: parse and apply unified-diff style line patches (stdlib only)."""
from .model import Patch, FilePatch, Hunk, HunkLine, reversed_patch
from .parser import parse_patch, format_patch, PatchParseError
from .applier import (
    ApplyOptions,
    ApplyError,
    HunkFailure,
    HunkReport,
    apply_file_patch,
    apply_to_text,
    apply_patch_to_tree,
    split_content,
    join_content,
)

__all__ = [
    "Patch", "FilePatch", "Hunk", "HunkLine", "reversed_patch",
    "parse_patch", "format_patch", "PatchParseError",
    "ApplyOptions", "ApplyError", "HunkFailure", "HunkReport",
    "apply_file_patch", "apply_to_text", "apply_patch_to_tree",
    "split_content", "join_content",
]
