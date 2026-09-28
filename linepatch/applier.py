"""Apply (and reverse-apply) parsed patches with context-based matching.

Hunks are located by their context/deletion lines, not by absolute line
numbers: the position declared in the hunk header is only the *expected*
position.  The applier searches outwards from it (nearest first, ties broken
towards the earlier position) up to ``ApplyConfig.max_offset`` lines.  On
failure a :class:`HunkConflictError` reports the first differing line at the
expected position and the closest candidate positions found in the file.

Line endings: matching is done on line *content*, so a LF patch applies to a
CRLF file and vice versa.  Output line endings follow ``ApplyConfig.eol``;
the default ``preserve`` keeps the target file's dominant style.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from .errors import (
    Candidate,
    HunkConflictError,
    MissingTargetError,
    PatchApplyError,
)
from .model import ADD, CONTEXT, DELETE, FilePatch, Hunk, Patch, PatchLine


@dataclass
class ApplyConfig:
    max_offset: Optional[int] = 200  # search radius in lines; None = unlimited
    eol: str = "preserve"  # 'preserve' | 'lf' | 'crlf'
    allow_create: bool = True  # allow --- /dev/null file creations
    allow_delete: bool = True  # allow +++ /dev/null file deletions
    candidate_limit: int = 5  # near-miss positions reported on conflict


class _Line:
    __slots__ = ("text", "has_newline")

    def __init__(self, text: str, has_newline: bool):
        self.text = text
        self.has_newline = has_newline


def _split(content: str) -> List[_Line]:
    """Split content into lines; only the last line may lack a newline."""
    if content == "":
        return []
    parts = content.split("\n")
    ends_with_nl = content.endswith("\n")
    if ends_with_nl:
        parts.pop()
    out = []
    for p in parts:
        if p.endswith("\r"):
            p = p[:-1]
        out.append(_Line(p, True))
    if not ends_with_nl:
        out[-1].has_newline = False
    return out


def _detect_eol(content: str, config: ApplyConfig) -> str:
    if config.eol == "lf":
        return "\n"
    if config.eol == "crlf":
        return "\r\n"
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _join(lines: List[_Line], eol: str) -> str:
    return "".join(l.text + (eol if l.has_newline else "") for l in lines)


def _matches(buf: List[_Line], old: List[PatchLine], pos: int) -> bool:
    if pos < 0 or pos + len(old) > len(buf):
        return False
    for j, pl in enumerate(old):
        bl = buf[pos + j]
        if bl.text != pl.text or bl.has_newline != pl.has_newline:
            return False
    return True


def _find(buf: List[_Line], old: List[PatchLine], expected: int,
          max_offset: Optional[int]) -> Optional[int]:
    if not old:  # pure-addition hunk: nothing to match, insert at expected pos
        return min(max(expected, 0), len(buf))
    limit = max_offset if max_offset is not None else max(len(buf), 1)
    for d in range(0, limit + 1):
        if _matches(buf, old, expected - d):
            return expected - d
        if d and _matches(buf, old, expected + d):
            return expected + d
    return None


def _best_candidates(buf: List[_Line], old: List[PatchLine], expected: int,
                     limit: int) -> List[Candidate]:
    """Rank positions by how many of the hunk's old-side lines match."""
    if not old:
        return []
    first = old[0].text
    seeds = [i for i, l in enumerate(buf) if l.text == first]
    scored = []
    for pos in seeds:
        score = 0
        for j, pl in enumerate(old):
            if pos + j < len(buf) and buf[pos + j].text == pl.text:
                score += 1
        scored.append((pos, score, len(old)))
    scored.sort(key=lambda c: (-c[1], abs(c[0] - expected)))
    return scored[:limit]


def _hunk_header(hunk: Hunk) -> str:
    head = "@@ -%d,%d +%d,%d @@" % (
        hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count)
    if hunk.section:
        head += " " + hunk.section
    return head


def apply_file_patch(fp: FilePatch, content: str,
                     config: Optional[ApplyConfig] = None) -> str:
    """Apply one file's hunks to ``content``; return the patched content."""
    config = config or ApplyConfig()
    eol = _detect_eol(content, config)
    buf = _split(content)
    shift = 0  # accumulated delta between old-file and buffer coordinates
    for idx, hunk in enumerate(fp.hunks):
        old = hunk.old_lines
        # unified diff: count==0 means "insert after line old_start"
        base = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
        expected = base + shift
        pos = _find(buf, old, expected, config.max_offset)
        if pos is None:
            first_diff = None
            for j, pl in enumerate(old):
                bi = expected + j
                actual = buf[bi].text if 0 <= bi < len(buf) else None
                if actual != pl.text:
                    first_diff = (bi, pl.text, actual)
                    break
            raise HunkConflictError(
                path=fp.target_path,
                hunk_index=idx,
                hunk_header=_hunk_header(hunk),
                expected_pos=expected,
                first_diff=first_diff,
                candidates=_best_candidates(buf, old, expected,
                                            config.candidate_limit),
                searched_offset=config.max_offset,
            )
        new = [_Line(l.text, l.has_newline) for l in hunk.new_lines]
        # A pure-insertion hunk can land directly after the target's final
        # line, which may be missing its terminator.  That preceding line
        # is no longer terminal, so supply the newline; _join renders it
        # with ``eol`` (the target file's own dominant line-ending style).
        if new and pos > 0 and not buf[pos - 1].has_newline:
            buf[pos - 1].has_newline = True
        buf[pos:pos + len(old)] = new
        shift += (len(new) - len(old)) + (pos - expected)
    return _join(buf, eol)


def reverse_file_patch(fp: FilePatch) -> FilePatch:
    """Return a FilePatch that undoes ``fp``."""
    swap = {ADD: DELETE, DELETE: ADD, CONTEXT: CONTEXT}
    hunks = [
        Hunk(
            old_start=h.new_start,
            old_count=h.new_count,
            new_start=h.old_start,
            new_count=h.old_count,
            section=h.section,
            lines=[PatchLine(swap[l.kind], l.text, l.has_newline)
                   for l in h.lines],
        )
        for h in fp.hunks
    ]
    return FilePatch(old_path=fp.new_path, new_path=fp.old_path, hunks=hunks)


def reverse_patch(patch: Patch) -> Patch:
    """Return a patch that undoes ``patch`` (file order is also reversed)."""
    return Patch(files=[reverse_file_patch(fp) for fp in reversed(patch.files)])


def _strip_prefix(path: str) -> str:
    parts = path.split("/")
    if len(parts) > 1 and parts[0] in ("a", "b"):
        return "/".join(parts[1:])
    return path


def apply_patch(patch: Patch, root: str = ".",
                config: Optional[ApplyConfig] = None) -> List[str]:
    """Apply a whole patch to the filesystem under ``root``.

    Returns a list of human-readable actions performed.
    """
    config = config or ApplyConfig()
    actions = []
    for fp in patch.files:
        rel = _strip_prefix(fp.target_path)
        path = os.path.join(root, rel)
        if fp.is_creation:
            if not config.allow_create:
                raise PatchApplyError("creation of %r disabled by config" % rel)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                raise PatchApplyError(
                    "cannot create %r: file exists and is not empty" % rel)
            new_content = apply_file_patch(fp, "", config)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", newline="") as f:
                f.write(new_content)
            actions.append("created %s" % rel)
        elif fp.is_deletion:
            if not config.allow_delete:
                raise PatchApplyError("deletion of %r disabled by config" % rel)
            if not os.path.exists(path):
                raise MissingTargetError(rel)
            with open(path, "r", newline="") as f:
                content = f.read()
            new_content = apply_file_patch(fp, content, config)
            if new_content != "":
                raise PatchApplyError(
                    "deletion of %r failed: patch leaves residual content" % rel)
            os.remove(path)
            actions.append("deleted %s" % rel)
        else:
            if not os.path.exists(path):
                raise MissingTargetError(rel)
            with open(path, "r", newline="") as f:
                content = f.read()
            new_content = apply_file_patch(fp, content, config)
            with open(path, "w", newline="") as f:
                f.write(new_content)
            actions.append("patched %s" % rel)
    return actions
