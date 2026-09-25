"""Fixed variable-length small-block compression codec (MCP1 format)."""

from .mini_compress import (
    compress,
    decompress,
    MODE_RAW,
    MODE_LZ,
    HEADER_SIZE,
    Error as MCPError,
    HeaderError,
    TruncatedError,
    ChecksumError,
)

__all__ = [
    "compress",
    "decompress",
    "MODE_RAW",
    "MODE_LZ",
    "HEADER_SIZE",
    "MCPError",
    "HeaderError",
    "TruncatedError",
    "ChecksumError",
]
