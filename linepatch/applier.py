"""Context-based patch application with offset search and diagnostics.

Matching strategy per hunk (never blind line numbers):
  1. try the expected position (hunk header adjusted by the accumulated
     delta of previously applied hunks);
  2. otherwise search outwards (up, then down, alternating) for an exact
     match of the hunk's old-side lines, up to ``options.max_offset`` lines;
  3. if no exact match exists, build a diagnostic: the most similar
     candidate window near the expected position plus a line-by-line
     preview of the first differences, and raise ApplyError.

End-of-line handling: comparison is done on line content with the EOL
marker stripped, so a LF patch applies cleanly to a CRLF file; output is
written back using the *target file's* EOL style.  The presence/absence of
a final newline is significant and participates in matching.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .model import Patch, FilePatch, Hunk, reversed_patch


@dataclass
class ApplyOptions:
    max_offset: int = 50          # how far (lines) a hunk may drift from its expected position
    diag_radius: int = 200        # search radius used to find the closest candidate on failure
    max_preview_diffs: int = 5    # differing lines shown in a failure preview


@dataclass
class HunkReport:
    hunk_index: int      # 0-based index inside the file patch
    applied_at: int      # 1-based line number where the hunk was applied
    offset: int          # drift from the expected position (0 = exact)


@dataclass
class HunkFailure:
    path: str
    hunk_index: int
    expected_line: int          # 1-based position where the hunk was expected
    old_line_count: int
    best_position: int | None   # 1-based position of the most similar window
    best_ratio: float           # similarity of that window, 0..1
    preview: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"hunk #{self.hunk_index + 1} of {self.path} does not match "
            f"(expected near line {self.expected_line})",
        ]
        if self.best_position is not None:
            lines.append(
                f"  closest candidate starts at line {self.best_position} "
                f"(similarity {self.best_ratio:.0%})"
            )
        lines.extend(f"  {p}" for p in self.preview)
        return "\n".join(lines)


class ApplyError(Exception):
    def __init__(self, failures: list[HunkFailure] | str):
        self.failures: list[HunkFailure] = (
            failures if isinstance(failures, list) else []
        )
        msg = (
            "\n".join(str(f) for f in self.failures)
            if isinstance(failures, list)
            else failures
        )
        super().__init__(msg)


# ---------------------------------------------------------------- content io

def split_content(text: str) -> tuple[list[str], str, bool]:
    """Split file content into (lines, eol_style, ends_with_eol)."""
    if text == "":
        return [], "\n", True
    eol = "\r\n" if "\r\n" in text else "\n"
    ends_with_eol = text.endswith("\n")
    body = text[:-1] if ends_with_eol else text
    lines = [l[:-1] if l.endswith("\r") else l for l in body.split("\n")]
    return lines, eol, ends_with_eol


def join_content(lines: list[str], eol: str, ends_with_eol: bool) -> str:
    if not lines:
        return ""
    text = eol.join(lines)
    if ends_with_eol:
        text += eol
    return text


# ---------------------------------------------------------------- matching

def _hunk_base(hunk: Hunk) -> int:
    # 0-based index of the first old-side line; for pure insertions
    # (old_count == 0) old_start is the line *after which* to insert.
    return hunk.old_start - 1 if hunk.old_count else hunk.old_start


def _locate(lines: list[str], ends_with_eol: bool, hunk: Hunk,
            expected: int, max_offset: int) -> tuple[int, int] | None:
    old = hunk.old_lines
    n = len(old)

    def ok(pos: int) -> bool:
        if pos < 0 or pos + n > len(lines):
            return False
        if lines[pos:pos + n] != old:
            return False
        # if the hunk consumes the file tail, the final-newline state must match
        if pos + n == len(lines) and ends_with_eol == hunk.old_no_eol:
            return False
        return True

    if ok(expected):
        return expected, 0
    for d in range(1, max_offset + 1):
        if ok(expected - d):
            return expected - d, -d
        if ok(expected + d):
            return expected + d, d
    return None


def _best_candidate(lines: list[str], old: list[str], expected: int,
                    radius: int) -> tuple[int | None, float]:
    if not old:
        return None, 0.0
    n = len(old)
    lo = max(0, expected - radius)
    hi = min(len(lines) - n, expected + radius)
    best_pos: int | None = None
    best_ratio = -1.0
    for pos in range(lo, hi + 1):
        window = lines[pos:pos + n]
        sm = SequenceMatcher(None, old, window)
        if sm.real_quick_ratio() < best_ratio:
            continue
        if sm.quick_ratio() < best_ratio:
            continue
        ratio = sm.ratio()
        if ratio > best_ratio:
            best_ratio, best_pos = ratio, pos
    return best_pos, max(best_ratio, 0.0)


def _preview(old: list[str], actual: list[str], actual_start: int,
             max_diffs: int) -> list[str]:
    out = []
    shown = 0
    for i in range(max(len(old), len(actual))):
        e = old[i] if i < len(old) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            shown += 1
            if shown > max_diffs:
                out.append("...")
                break
            out.append(
                f"line {actual_start + i}: expected {e!r} but found {a!r}"
            )
    if not out:
        out.append("(content matches; final-newline state differs)")
    return out


# ---------------------------------------------------------------- application

def apply_file_patch(lines: list[str], ends_with_eol: bool, fp: FilePatch,
                     options: ApplyOptions
                     ) -> tuple[list[str], bool, list[HunkReport]]:
    """Apply one FilePatch to in-memory lines. Raises ApplyError on conflict."""
    lines = list(lines)
    delta = 0  # accumulated line drift from previously applied hunks
    reports: list[HunkReport] = []
    for idx, hunk in enumerate(fp.hunks):
        expected = _hunk_base(hunk) + delta
        found = _locate(lines, ends_with_eol, hunk, expected, options.max_offset)
        if found is None:
            old = hunk.old_lines
            best_pos, best_ratio = _best_candidate(
                lines, old, expected, options.diag_radius)
            actual = (lines[best_pos:best_pos + len(old)]
                      if best_pos is not None else [])
            failure = HunkFailure(
                path=fp.new_path,
                hunk_index=idx,
                expected_line=expected + 1,
                old_line_count=len(old),
                best_position=(best_pos + 1) if best_pos is not None else None,
                best_ratio=best_ratio,
                preview=_preview(old, actual,
                                 (best_pos + 1) if best_pos is not None else 0,
                                 options.max_preview_diffs),
            )
            raise ApplyError([failure])
        pos, off = found
        old_len = len(hunk.old_lines)
        touches_eof = pos + old_len == len(lines)
        lines[pos:pos + old_len] = hunk.new_lines
        if touches_eof:
            ends_with_eol = not hunk.new_no_eol
        delta += hunk.new_count - hunk.old_count
        reports.append(HunkReport(hunk_index=idx, applied_at=pos + 1, offset=off))
    return lines, ends_with_eol, reports


def apply_to_text(content: str, fp: FilePatch,
                  options: ApplyOptions | None = None
                  ) -> tuple[str, list[HunkReport]]:
    options = options or ApplyOptions()
    lines, eol, ends_with_eol = split_content(content)
    lines, ends_with_eol, reports = apply_file_patch(lines, ends_with_eol, fp, options)
    return join_content(lines, eol, ends_with_eol), reports


def apply_patch_to_tree(patch: Patch, root: str = ".",
                        options: ApplyOptions | None = None,
                        reverse: bool = False) -> dict[str, list[HunkReport]]:
    """Apply a parsed patch to the filesystem under `root`.

    Behaviour for edge cases:
      * missing target file: created only when the file patch marks it as new
        (``--- /dev/null``); otherwise ApplyError is raised;
      * deleted files (``+++ /dev/null``) are removed;
      * new files are written with LF line endings; existing files keep
        their own EOL style.
    """
    options = options or ApplyOptions()
    if reverse:
        patch = reversed_patch(patch)
    results: dict[str, list[HunkReport]] = {}
    for fp in patch.files:
        path = os.path.join(root, fp.new_path)
        if fp.is_delete:
            old_path = os.path.join(root, fp.old_path)
            if not os.path.exists(old_path):
                raise ApplyError(f"cannot delete {fp.old_path}: file is missing")
            with open(old_path, "r", newline="") as f:
                content = f.read()
            apply_to_text(content, FilePatch(fp.old_path, fp.old_path, fp.hunks),
                          options)  # validate the old content matches
            os.remove(old_path)
            results[fp.old_path] = []
            continue
        if os.path.exists(path):
            with open(path, "r", newline="") as f:
                content = f.read()
        elif fp.is_new:
            content = ""
        else:
            raise ApplyError(
                f"target file {fp.new_path} is missing and the patch does "
                f"not create it")
        new_content, reports = apply_to_text(content, fp, options)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="") as f:
            f.write(new_content)
        results[fp.new_path] = reports
    return results
