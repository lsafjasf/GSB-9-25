"""Data model for line-level (unified diff style) patches."""
from __future__ import annotations

from dataclasses import dataclass, field

CONTEXT, REMOVE, ADD = " ", "-", "+"

DEV_NULL = "/dev/null"


@dataclass
class HunkLine:
    kind: str  # CONTEXT, REMOVE or ADD
    text: str  # line content without trailing newline


@dataclass
class Hunk:
    old_start: int  # 1-based line number in the old file
    old_count: int
    new_start: int  # 1-based line number in the new file
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)
    section: str = ""  # optional text after the second @@
    old_no_eol: bool = False  # old side's last line lacks a trailing newline
    new_no_eol: bool = False  # new side's last line lacks a trailing newline

    @property
    def old_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.kind != ADD]

    @property
    def new_lines(self) -> list[str]:
        return [l.text for l in self.lines if l.kind != REMOVE]


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def is_new(self) -> bool:
        return self.old_path == DEV_NULL

    @property
    def is_delete(self) -> bool:
        return self.new_path == DEV_NULL


@dataclass
class Patch:
    files: list[FilePatch] = field(default_factory=list)

    @property
    def hunk_count(self) -> int:
        return sum(len(f.hunks) for f in self.files)


def _strip_prefix(path: str) -> str:
    # normalise git-style a/ b/ prefixes
    if path != DEV_NULL and len(path) > 2 and path[1] == "/" and path[0] in "ab":
        return path[2:]
    return path


def normalize_path(path: str) -> str:
    return _strip_prefix(path.split("\t")[0].strip())


def reversed_patch(patch: Patch) -> Patch:
    """Return a patch that undoes `patch` (swap old/new sides everywhere)."""
    out = Patch()
    for fp in patch.files:
        rfp = FilePatch(old_path=fp.new_path, new_path=fp.old_path)
        for h in fp.hunks:
            rh = Hunk(
                old_start=h.new_start,
                old_count=h.new_count,
                new_start=h.old_start,
                new_count=h.old_count,
                section=h.section,
                old_no_eol=h.new_no_eol,
                new_no_eol=h.old_no_eol,
            )
            for l in h.lines:
                if l.kind == REMOVE:
                    rh.lines.append(HunkLine(ADD, l.text))
                elif l.kind == ADD:
                    rh.lines.append(HunkLine(REMOVE, l.text))
                else:
                    rh.lines.append(HunkLine(CONTEXT, l.text))
            rfp.hunks.append(rh)
        out.files.append(rfp)
    return out
