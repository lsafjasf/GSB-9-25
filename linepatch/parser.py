"""Parser for unified-diff style line patches.

Supported input:
  * multiple file sections (``---``/``+++`` headers), preamble/garbage lines
    between sections (e.g. ``diff --git`` / ``index`` lines) are ignored;
  * multiple hunks per file (``@@ -a,b +c,d @@`` headers, counts optional);
  * body lines: ' ' context, '-' removed, '+' added, a bare empty line is
    treated as an empty context line (GNU patch behaviour);
  * ``\\ No newline at end of file`` markers.
"""
from __future__ import annotations

import re

from .model import ADD, CONTEXT, DEV_NULL, REMOVE, FilePatch, Hunk, HunkLine, Patch, normalize_path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s?(.*)$")

NO_EOL_MARKER = "\\ No newline at end of file"


class PatchParseError(ValueError):
    def __init__(self, lineno: int, message: str):
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


def _validate_hunk(hunk: Hunk, lineno: int) -> None:
    old = sum(1 for l in hunk.lines if l.kind != ADD)
    new = sum(1 for l in hunk.lines if l.kind != REMOVE)
    if old != hunk.old_count or new != hunk.new_count:
        raise PatchParseError(
            lineno,
            f"hunk line counts mismatch: header says -{hunk.old_count} "
            f"+{hunk.new_count}, body has -{old} +{new}",
        )


def parse_patch(text: str) -> Patch:
    lines = text.splitlines()
    patch = Patch()
    cur_file: FilePatch | None = None
    cur_hunk: Hunk | None = None
    pending_old_path: str | None = None

    def close_hunk(lineno: int) -> None:
        nonlocal cur_hunk
        if cur_hunk is not None:
            _validate_hunk(cur_hunk, lineno)
            cur_hunk = None

    def close_file(lineno: int) -> None:
        nonlocal cur_file, pending_old_path
        close_hunk(lineno)
        if cur_file is not None:
            patch.files.append(cur_file)
            cur_file = None
        pending_old_path = None

    for i, line in enumerate(lines, start=1):
        if line.startswith("--- "):
            close_file(i)
            pending_old_path = normalize_path(line[4:])
        elif line.startswith("+++ "):
            if pending_old_path is None:
                raise PatchParseError(i, "'+++' without preceding '---'")
            cur_file = FilePatch(old_path=pending_old_path, new_path=normalize_path(line[4:]))
            pending_old_path = None
        elif line.startswith("@@"):
            if cur_file is None:
                raise PatchParseError(i, "hunk header outside of a file section")
            close_hunk(i)
            m = HUNK_RE.match(line)
            if not m:
                raise PatchParseError(i, f"malformed hunk header: {line!r}")
            cur_hunk = Hunk(
                old_start=int(m.group(1)),
                old_count=int(m.group(2)) if m.group(2) is not None else 1,
                new_start=int(m.group(3)),
                new_count=int(m.group(4)) if m.group(4) is not None else 1,
                section=m.group(5),
            )
            cur_file.hunks.append(cur_hunk)
        elif line.startswith("\\"):
            if cur_hunk is None or not cur_hunk.lines:
                raise PatchParseError(i, "'\\' marker without a preceding body line")
            last = cur_hunk.lines[-1]
            if last.kind in (CONTEXT, REMOVE):
                cur_hunk.old_no_eol = True
            if last.kind in (CONTEXT, ADD):
                cur_hunk.new_no_eol = True
        elif cur_hunk is not None and (line[:1] in (CONTEXT, REMOVE, ADD) or line == ""):
            kind = line[:1] if line[:1] in (CONTEXT, REMOVE, ADD) else CONTEXT
            cur_hunk.lines.append(HunkLine(kind, line[1:]))
        # anything else (diff --git, index, blank separators between files,
        # comments) is ignored when not inside a hunk body.

    close_file(len(lines) + 1)
    if not patch.files:
        raise PatchParseError(0, "no file sections found in patch")
    return patch


def format_patch(patch: Patch) -> str:
    """Serialise a Patch back to unified-diff text (inverse of parse_patch)."""
    out: list[str] = []
    for fp in patch.files:
        old = fp.old_path if fp.old_path == DEV_NULL else f"a/{fp.old_path}"
        new = fp.new_path if fp.new_path == DEV_NULL else f"b/{fp.new_path}"
        out.append(f"--- {old}")
        out.append(f"+++ {new}")
        for h in fp.hunks:
            header = f"@@ -{h.old_start},{h.old_count} +{h.new_start},{h.new_count} @@"
            if h.section:
                header += f" {h.section}"
            out.append(header)
            last_old = max((i for i, l in enumerate(h.lines) if l.kind != ADD), default=-1)
            last_new = max((i for i, l in enumerate(h.lines) if l.kind != REMOVE), default=-1)
            for i, l in enumerate(h.lines):
                out.append(l.kind + l.text)
                if (i == last_old and h.old_no_eol) or (i == last_new and h.new_no_eol):
                    out.append(NO_EOL_MARKER)
    return "\n".join(out) + "\n"
