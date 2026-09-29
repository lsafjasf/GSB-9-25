"""Append-only log format with per-record length + CRC32 checksums.

Record layout (little-endian)::

    +----------+------------------+------------------+==================+
    | magic 4B | payload_len u32  | payload_crc u32  | payload (len B)  |
    +----------+------------------+------------------+==================+

* magic         : b"ALG1", used for framing and resynchronisation.
* payload_len   : length of the payload in bytes (header not included).
* payload_crc   : CRC32 (zlib) of the payload bytes.

Writer
------
``LogWriter.append_batch`` returns after the bytes have been handed to the
OS (write buffer) and reports a *buffered* position.  ``LogWriter.sync``
forces data to stable storage (fsync) and reports the *durable* position.
Callers can therefore distinguish "accepted into the write buffer" from
"forced to disk" and always query the last durable offset.

Reader
------
``LogReader`` validates every record.  Corruption is classified into:

* ``TRUNCATED``         - tail of the file ends mid-record (torn write).
* ``INVALID_LENGTH``    - the length field is implausible (out of bounds).
* ``CHECKSUM_MISMATCH`` - full record present but CRC32 does not match.
* ``BAD_MAGIC``         - framing bytes not found where a header was expected.

Two recovery modes are offered explicitly:

* ``STRICT`` - raise ``CorruptionError`` at the first damaged region.
* ``SKIP``   - log the damaged region, resynchronise byte-by-byte from the
  corruption start (a tampered length field is never trusted to skip over
  healthy records) and keep reading to EOF; every skipped range is
  recorded individually in ``ReadResult.corruptions``.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence, Tuple

MAGIC = b"ALG1"
HEADER_STRUCT = struct.Struct("<4sII")  # magic, payload_len, payload_crc32
HEADER_SIZE = HEADER_STRUCT.size  # 12 bytes
MAX_RECORD_SIZE = 64 * 1024 * 1024  # 64 MiB sanity bound for the length field

# Corruption kinds
TRUNCATED = "truncated"
INVALID_LENGTH = "invalid_length"
CHECKSUM_MISMATCH = "checksum_mismatch"
BAD_MAGIC = "bad_magic"

# Recovery modes
STRICT = "strict"
SKIP = "skip"


@dataclass(frozen=True)
class WriteAck:
    """Acknowledgement returned by write operations.

    ``buffered_upto`` is the file offset up to which data has been handed to
    the OS write buffer; ``durable_upto`` is the offset up to which data has
    been forced to stable storage with fsync.
    """

    buffered_upto: int
    durable_upto: int


@dataclass(frozen=True)
class Corruption:
    """A damaged region found while reading."""

    kind: str
    offset: int          # file offset where the damage starts
    end: int             # end of the damaged/skipped region (exclusive)
    reason: str

    @property
    def size(self) -> int:
        return self.end - self.offset


class CorruptionError(Exception):
    """Raised in STRICT mode when a damaged region is encountered."""

    def __init__(self, corruption: Corruption):
        self.corruption = corruption
        super().__init__(
            f"{corruption.kind} at offset {corruption.offset}: {corruption.reason}"
        )


@dataclass(frozen=True)
class Record:
    offset: int
    payload: bytes


@dataclass
class ReadResult:
    """Outcome of a full scan (used by ``LogReader.scan``)."""

    records: List[Record] = field(default_factory=list)
    corruptions: List[Corruption] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.corruptions


def encode_record(payload: bytes) -> bytes:
    """Encode a single record (header + payload)."""
    if len(payload) > MAX_RECORD_SIZE:
        raise ValueError(f"payload too large: {len(payload)} > {MAX_RECORD_SIZE}")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return HEADER_STRUCT.pack(MAGIC, len(payload), crc) + payload


class LogWriter:
    """Appends records to a log file.

    ``append_batch`` buffers; ``sync`` fsyncs.  The writer tracks its own
    logical end offset so positions are stable even with O_APPEND.
    """

    def __init__(self, path: str):
        self._path = path
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        # Logical end of file at open time; O_APPEND makes ftell unreliable.
        self._offset = os.fstat(self._fd).st_size
        self._durable_upto = self._offset

    @property
    def path(self) -> str:
        return self._path

    @property
    def buffered_upto(self) -> int:
        """Offset up to which data has been written to the OS buffer."""
        return self._offset

    @property
    def durable_upto(self) -> int:
        """Offset up to which data has been forced to stable storage."""
        return self._durable_upto

    def append_batch(self, payloads: Sequence[bytes]) -> WriteAck:
        """Append a batch of records; returns when buffered by the OS.

        The returned ack exposes both the buffered position (advanced by
        this call) and the durable position (unchanged until ``sync``).
        """
        blob = b"".join(encode_record(p) for p in payloads)
        view = memoryview(blob)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        self._offset += len(blob)
        return WriteAck(buffered_upto=self._offset, durable_upto=self._durable_upto)

    def sync(self) -> int:
        """Force all buffered data to stable storage; returns durable offset."""
        os.fsync(self._fd)
        self._durable_upto = self._offset
        return self._durable_upto

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "LogWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _parse_header(buf, pos: int, file_size: int) -> Tuple[Optional[Corruption], int, int]:
    """Validate the header at ``pos``.

    Returns ``(corruption, payload_len, payload_crc)``.  When ``corruption``
    is None the header is well-formed.
    """
    remaining = file_size - pos
    if remaining < HEADER_SIZE:
        return (
            Corruption(
                TRUNCATED,
                pos,
                file_size,
                f"only {remaining} byte(s) left, header needs {HEADER_SIZE} "
                "(torn write at tail)",
            ),
            0,
            0,
        )
    magic, length, crc = HEADER_STRUCT.unpack_from(buf, pos)
    if magic != MAGIC:
        return (
            Corruption(
                BAD_MAGIC,
                pos,
                pos + 1,  # caller extends the region while resyncing
                f"expected magic {MAGIC!r}, found {bytes(magic)!r}",
            ),
            0,
            0,
        )
    if length > MAX_RECORD_SIZE:
        return (
            Corruption(
                INVALID_LENGTH,
                pos,
                pos + HEADER_SIZE,
                f"length field {length} exceeds max record size {MAX_RECORD_SIZE}",
            ),
            0,
            0,
        )
    if pos + HEADER_SIZE + length > file_size:
        return (
            Corruption(
                TRUNCATED,
                pos,
                file_size,
                f"record claims {length} payload bytes but only "
                f"{file_size - pos - HEADER_SIZE} remain (torn write at tail)",
            ),
            0,
            0,
        )
    return None, length, crc


class LogReader:
    """Reads and validates records from a log file.

    ``mode`` is either ``STRICT`` (raise on first corruption) or ``SKIP``
    (record the damaged region, resynchronise, continue to EOF).
    """

    def __init__(self, path: str, mode: str = STRICT):
        if mode not in (STRICT, SKIP):
            raise ValueError(f"unknown mode: {mode!r} (expected 'strict' or 'skip')")
        self._path = path
        self._mode = mode

    def _open_view(self):
        """Return (view, buffer, size); uses mmap for large files."""
        fd = os.open(self._path, os.O_RDONLY)
        size = os.fstat(fd).st_size
        if size == 0:
            return os.fdopen(fd, "rb"), b"", 0
        import mmap

        mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        # The mmap keeps the mapping alive; closing the fd is safe.
        os.close(fd)
        return mm, mm, size

    def _handle(self, corruption: Corruption, corruptions: List[Corruption]) -> None:
        if self._mode == STRICT:
            raise CorruptionError(corruption)
        corruptions.append(corruption)

    @staticmethod
    def _find_next_magic(buf, start: int, size: int) -> int:
        idx = buf.find(MAGIC, start)
        return idx if idx != -1 else size

    def iter_records(self) -> Iterator[Record]:
        """Yield valid records.  STRICT raises; SKIP skips damaged regions."""
        for record, _ in self._walk(None):
            if record is not None:
                yield record

    def scan(self) -> ReadResult:
        """Read the whole file, returning valid records and all corruptions."""
        result = ReadResult()
        for record, corruption in self._walk(result.corruptions):
            if record is not None:
                result.records.append(record)
        return result

    def _walk(self, corruptions: Optional[List[Corruption]]):
        """Core scan loop yielding (record, corruption) pairs."""
        collected = corruptions if corruptions is not None else []
        view, buf, size = self._open_view()
        try:
            pos = 0
            while pos < size:
                corruption, length, crc = _parse_header(buf, pos, size)
                if corruption is not None:
                    if corruption.kind == TRUNCATED:
                        # Nothing recoverable after a torn tail.
                        self._handle(corruption, collected)
                        yield None, corruption
                        return
                    # INVALID_LENGTH / BAD_MAGIC: resynchronise on next magic.
                    nxt = self._find_next_magic(buf, pos + 1, size)
                    corruption = Corruption(
                        corruption.kind, corruption.offset, nxt, corruption.reason
                    )
                    self._handle(corruption, collected)
                    yield None, corruption
                    pos = nxt
                    continue
                payload = bytes(buf[pos + HEADER_SIZE : pos + HEADER_SIZE + length])
                if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                    end = pos + HEADER_SIZE + length
                    # The declared length may itself be the product of
                    # tampering, so ``end`` cannot be trusted: healthy
                    # records may be sandwiched inside the declared span.
                    # Resynchronise byte-by-byte from the corruption start
                    # and recover any record that fully validates; every
                    # skipped segment is reported individually.
                    seg_start = pos
                    scan = pos + 1
                    resume = None
                    while scan < end:
                        idx = buf.find(MAGIC, scan, end)
                        if idx == -1:
                            break
                        nested, nlen, ncrc = _parse_header(buf, idx, size)
                        if nested is not None:
                            scan = idx + 1  # false magic, keep scanning
                            continue
                        npayload = bytes(
                            buf[idx + HEADER_SIZE : idx + HEADER_SIZE + nlen]
                        )
                        if (zlib.crc32(npayload) & 0xFFFFFFFF) == ncrc:
                            # A valid record starts here: the skipped segment
                            # ends exactly at its offset.
                            resume = idx
                            break
                        # A fully framed record that fails its own checksum
                        # is a separate damage; report it on its own and
                        # keep resynchronising after it.
                        corruption = Corruption(
                            CHECKSUM_MISMATCH,
                            seg_start,
                            idx,
                            f"payload CRC32 mismatch at offset {seg_start} "
                            f"(skipped {idx - seg_start} byte(s) while "
                            "resynchronising)",
                        )
                        self._handle(corruption, collected)
                        yield None, corruption
                        seg_start = idx
                        scan = idx + 1
                    if resume is None:
                        resume = end
                    if seg_start != pos:
                        reason = (
                            f"payload CRC32 mismatch at offset {seg_start} "
                            f"(skipped {resume - seg_start} byte(s) while "
                            "resynchronising)"
                        )
                    else:
                        reason = (
                            f"payload CRC32 mismatch (header crc=0x{crc:08x}, "
                            f"computed over {length} byte(s) differs)"
                        )
                    corruption = Corruption(
                        CHECKSUM_MISMATCH,
                        seg_start,
                        resume,
                        reason,
                    )
                    self._handle(corruption, collected)
                    yield None, corruption
                    pos = resume
                    continue
                yield Record(offset=pos, payload=payload), None
                pos += HEADER_SIZE + length
        finally:
            view.close()
