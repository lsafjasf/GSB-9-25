#!/usr/bin/env python3
"""Migrate legacy v1 frames (``LC1``) to fixed v2 frames (``MC2``).

Input is a concatenation of frames, each either v1 (``LC1`` + uint16 length +
LZSS tokens) or already v2 (``MC2`` ...).  Output is a concatenation of v2
frames written to stdout; a human report goes to stderr.

Frame-level migration policy:

* v2 frame                  -> copied byte-for-byte (idempotent);
* v1 frame, status "ok"     -> recompressed as v2, migrated;
* v1 frame, "truncated"     -> SKIPPED.  Bytes are already lost (defect
                               #2/#4); there is no way to reconstruct them;
* v1 frame, "unrecoverable" -> SKIPPED.  Corrupt token stream.

v1 frames carry no token-byte length, so the v1 frame boundary is found by
scanning for the next LC1/MC2 magic and validating the candidate boundary.

Exit code is 1 if any frame had to be skipped, 0 otherwise.

Examples:
    python3 migrate.py old_frames.bin > new_frames.bin
    cat old_frames.bin | python3 migrate.py > new_frames.bin
"""

import sys

from mini_compress import compress2
from mini_compress import legacy


def _find_v1_end(blob, start):
    """Locate the end of a v1 frame beginning at *start*.

    A complete defect-free v1 frame ends exactly when decoded length reaches
    the declared length.  Frames produced by the defect #2 encoder end early;
    their boundary is the earliest later position where another well-formed
    frame begins.  Returns None when the frame is simply cut off.
    """
    declared = int.from_bytes(blob[start + 3:start + 5], "big")
    n = len(blob)
    i = start + 5
    out_len = 0
    while True:
        if i >= n:
            # Stream ends inside a tag byte: genuinely truncated.
            return None
        tag = blob[i]
        if tag & 0x80:
            if i + 1 >= n:
                return None
            off = ((tag & 1) << 8) | blob[i + 1]
            if off < 1:
                break
            ln = ((tag >> 1) & 0x0F) + legacy.MIN_MATCH
            i += 2
            out_len += ln
        else:
            if i + 1 >= n:
                return None
            i += 2
            out_len += 1
        if out_len >= declared:
            # Complete frame ends exactly on this token boundary.
            return i
        if blob[i:i + 3] in (legacy.MAGIC, compress2.MAGIC):
            # Encoder stopped early (defect #2): this magic starts the next
            # frame only if no token byte is dangling, which holds because i
            # is on a token boundary above.
            return i
    return None


def _find_v2_end(blob, start):
    if start + compress2.HEADER_LEN > len(blob):
        return None
    end = start + compress2.HEADER_LEN + int.from_bytes(
        blob[start + 9:start + 13], "big")
    return end if end <= len(blob) else None


def iter_frames(blob):
    """Yield ``(kind, offset, frame_bytes)`` for every frame."""
    pos, n = 0, len(blob)
    while pos < n:
        magic = blob[pos:pos + 3]
        if magic == legacy.MAGIC:
            end = _find_v1_end(blob, pos)
            kind = "v1"
        elif magic == compress2.MAGIC:
            end = _find_v2_end(blob, pos)
            kind = "v2"
        else:
            raise ValueError("unknown frame magic at offset %d" % pos)
        if end is None:
            raise ValueError("truncated frame at offset %d" % pos)
        yield kind, pos, blob[pos:end]
        pos = end


def migrate_frame(kind, frame):
    """Return ``(output_frame_or_None, status, detail)`` for one frame."""
    if kind == "v2":
        compress2.decompress(frame)  # verify before trusting
        return frame, "kept", "already v2"
    status, data = legacy.inspect(frame)
    if status == "ok":
        return compress2.compress(data), "migrated", "lossless v1 frame"
    return None, "skipped:" + status, "bytes are unrecoverable"


def main(argv):
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in ("-h", "--help")):
        print(__doc__, file=sys.stderr)
        return 2
    source = open(argv[1], "rb") if len(argv) == 2 else sys.stdin.buffer
    blob = source.read()

    counts = {"migrated": 0, "kept": 0, "skipped:truncated": 0,
              "skipped:unrecoverable": 0}
    output = bytearray()
    try:
        for kind, offset, frame in iter_frames(blob):
            new_frame, status, detail = migrate_frame(kind, frame)
            counts[status] = counts.get(status, 0) + 1
            if new_frame is not None:
                output += new_frame
            print("frame@%-6d %-22s %s" % (offset, status, detail),
                  file=sys.stderr)
    except ValueError as exc:
        print("migration aborted: %s" % exc, file=sys.stderr)
        return 1

    sys.stdout.buffer.write(bytes(output))
    print("summary: %s" % ", ".join(
        "%s=%d" % (k, v) for k, v in counts.items() if v), file=sys.stderr)
    skipped = counts["skipped:truncated"] + counts["skipped:unrecoverable"]
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
