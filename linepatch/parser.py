"""Parser for unified-diff style line patches.

Supports: multiple files per patch, multiple hunks per file, "\\ No newline
at end of file" markers, git-style preamble lines (diff --git / index /
new file mode / ...) which are skipped, and optional ",count" parts in hunk
headers. Patch text may use LF or CRLF terminators; a single trailing "\\r"
is stripped from every patch line.
"""
from __future__ import annotations

import re

from .errors import PatchParseError
from .model import ADD, CONTEXT, DELETE, FilePatch, Hunk, Patch, PatchLine

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_NO_NEWLINE = "\\ No newline at end of file"


def _parse_path(rest: str) -> str:
    rest = rest.strip()
    if rest.startswith('"'):  # quoted (git core.quotePath style for specials)
        end = rest.find('"', 1)
        if end > 0:
            return rest[1:end]
    # traditional diffs append a tab-separated timestamp
    if "\t" in rest:
        rest = rest.split("\t", 1)[0]
    return rest.strip()


def parse_patch(text: str) -> Patch:
    """Parse patch text into a :class:`~linepatch.model.Patch`."""
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()  # artifact of a trailing newline, not a real line
    # tolerate CRLF patch files
    lines = [l[:-1] if l.endswith("\r") else l for l in raw_lines]

    patch = Patch()
    current_file = None
    current_hunk = None
    old_left = new_left = 0  # remaining lines of the hunk being read

    for idx, line in enumerate(lines):
        lineno = idx + 1

        if current_hunk is not None and line.startswith("\\"):
            if line.rstrip() != _NO_NEWLINE:
                raise PatchParseError("unknown '\\' line: %r" % line, lineno)
            if not current_hunk.lines:
                raise PatchParseError("'No newline' marker before any line", lineno)
            current_hunk.lines[-1].has_newline = False
            continue

        if current_hunk is not None and (old_left > 0 or new_left > 0):
            if line == "":
                # unified diff quirk: an empty line means an empty context line
                kind, body = CONTEXT, ""
            else:
                kind = {" ": CONTEXT, "-": DELETE, "+": ADD}.get(line[0])
                if kind is None:
                    raise PatchParseError(
                        "unexpected line inside hunk: %r" % line, lineno
                    )
                body = line[1:]
            if kind != ADD:
                old_left -= 1
            if kind != DELETE:
                new_left -= 1
            current_hunk.lines.append(PatchLine(kind, body))
            continue

        if line.startswith("--- "):
            current_file = FilePatch(old_path=_parse_path(line[4:]), new_path="")
            current_hunk = None
            continue
        if line.startswith("+++ "):
            if current_file is None or current_file.new_path:
                raise PatchParseError("'+++' without preceding '---'", lineno)
            current_file.new_path = _parse_path(line[4:])
            patch.files.append(current_file)
            continue

        m = _HUNK_RE.match(line)
        if m:
            if current_file is None or not current_file.new_path:
                raise PatchParseError("hunk header outside of a file section", lineno)
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current_hunk = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=m.group(5).strip(),
            )
            current_file.hunks.append(current_hunk)
            old_left, new_left = old_count, new_count
            continue

        # Anything else (diff --git, index, mode lines, comments, blank
        # separators between file sections) is ignored outside hunks.

    if current_hunk is not None and (old_left > 0 or new_left > 0):
        raise PatchParseError(
            "patch truncated: hunk declares %d more old / %d more new line(s)"
            % (old_left, new_left)
        )
    if current_file is not None and not current_file.new_path:
        raise PatchParseError("file section missing '+++' header")
    return patch
