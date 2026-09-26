"""filededup: file fingerprinting and chunk-level duplicate detection."""

from .cdc import Chunker
from .scanner import (
    DedupIndex,
    FileInfo,
    SimilarEdge,
    SimilarGroup,
    iter_files,
    scan_file,
)

__all__ = [
    "Chunker",
    "DedupIndex",
    "FileInfo",
    "SimilarEdge",
    "SimilarGroup",
    "iter_files",
    "scan_file",
]

__version__ = "0.1.0"
