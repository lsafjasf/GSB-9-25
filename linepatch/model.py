"""Data model for line-level (unified-diff style) patches."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

#: Patch line kinds
CONTEXT = "context"
DELETE = "del"
ADD = "add"

_KIND_FROM_CHAR = {" ": CONTEXT, "-": DELETE, "+": ADD}
_KIND_TO_CHAR = {CONTEXT: " ", DELETE: "-", ADD: "+"}


@dataclass
class PatchLine:
    """A single line inside a hunk.

    ``text`` never contains a line terminator. ``has_newline`` is False only
    for a line that the diff marks with "\\ No newline at end of file".
    """

    kind: str  # CONTEXT | DELETE | ADD
    text: str
    has_newline: bool = True

    def render(self) -> str:
        s = _KIND_TO_CHAR[self.kind] + self.text + "\n"
        if not self.has_newline:
            s += "\\ No newline at end of file\n"
        return s


@dataclass
class Hunk:
    """One @@ ... @@ section."""

    old_start: int  # 1-based line number in the old file
    old_count: int
    new_start: int  # 1-based line number in the new file
    new_count: int
    section: str = ""  # optional heading text after the second @@
    lines: List[PatchLine] = field(default_factory=list)

    @property
    def old_lines(self) -> List[PatchLine]:
        """Lines expected in the source (context + deletions)."""
        return [l for l in self.lines if l.kind != ADD]

    @property
    def new_lines(self) -> List[PatchLine]:
        """Lines produced by the hunk (context + additions)."""
        return [l for l in self.lines if l.kind != DELETE]

    def render(self) -> str:
        head = "@@ -{} +{} @@".format(
            _range(self.old_start, self.old_count),
            _range(self.new_start, self.new_count),
        )
        if self.section:
            head += " " + self.section
        return head + "\n" + "".join(l.render() for l in self.lines)


def _range(start: int, count: int) -> str:
    # unified diff omits ",1"
    return str(start) if count == 1 else "%d,%d" % (start, count)


@dataclass
class FilePatch:
    """All hunks belonging to one file (--- / +++ pair)."""

    old_path: str
    new_path: str
    hunks: List[Hunk] = field(default_factory=list)

    @property
    def is_creation(self) -> bool:
        return self.old_path == "/dev/null"

    @property
    def is_deletion(self) -> bool:
        return self.new_path == "/dev/null"

    @property
    def target_path(self) -> str:
        """Path of the file the hunks are applied to / produced from."""
        return self.old_path if self.is_deletion else self.new_path

    def render(self) -> str:
        out = "--- %s\n+++ %s\n" % (self.old_path, self.new_path)
        return out + "".join(h.render() for h in self.hunks)


@dataclass
class Patch:
    """A whole patch file: an ordered list of per-file sections."""

    files: List[FilePatch] = field(default_factory=list)

    def render(self) -> str:
        return "".join(f.render() for f in self.files)
