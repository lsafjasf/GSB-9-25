"""Exceptions raised by linepatch."""
from __future__ import annotations

from typing import List, Optional, Tuple


class PatchParseError(ValueError):
    """The patch text is malformed."""

    def __init__(self, message: str, lineno: Optional[int] = None):
        self.lineno = lineno
        if lineno is not None:
            message = "line %d: %s" % (lineno, message)
        super().__init__(message)


class PatchApplyError(Exception):
    """Base class for failures while applying a patch."""


class MissingTargetError(PatchApplyError):
    """The target file of a file-patch does not exist."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(
            "target file %r does not exist (patch is not a file creation)" % path
        )


#: One near-miss position: (0-based position, matched line count, total lines)
Candidate = Tuple[int, int, int]


class HunkConflictError(PatchApplyError):
    """A hunk could not be matched against the target content.

    Carries enough detail to locate the problem: where the hunk was expected,
    the first differing line there, and the closest candidate positions found
    anywhere else in the file.
    """

    def __init__(
        self,
        path: str,
        hunk_index: int,
        hunk_header: str,
        expected_pos: int,  # 0-based position where the hunk was expected
        first_diff: Optional[Tuple[int, str, Optional[str]]],
        candidates: List[Candidate],
        searched_offset: Optional[int],
    ):
        self.path = path
        self.hunk_index = hunk_index
        self.hunk_header = hunk_header
        self.expected_pos = expected_pos
        self.first_diff = first_diff  # (0-based line, expected text, actual text|None)
        self.candidates = candidates
        self.searched_offset = searched_offset
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [
            "hunk #%d of %r failed to apply" % (self.hunk_index + 1, self.path),
            "  hunk header: %s" % self.hunk_header,
            "  expected at line %d (offset search radius: %s)"
            % (
                self.expected_pos + 1,
                "unlimited" if self.searched_offset is None else self.searched_offset,
            ),
        ]
        if self.first_diff is not None:
            lineno, expected, actual = self.first_diff
            lines.append("  first difference at line %d:" % (lineno + 1))
            lines.append("    expected: %r" % expected)
            lines.append(
                "    actual:   %r" % (actual if actual is not None else "<end of file>")
            )
        if self.candidates:
            lines.append("  closest candidate position(s):")
            for pos, score, total in self.candidates:
                offset = pos - self.expected_pos
                lines.append(
                    "    line %d (offset %+d): %d/%d lines match"
                    % (pos + 1, offset, score, total)
                )
        else:
            lines.append("  no candidate position found anywhere in the file")
        return "\n".join(lines)
