"""cursor_pagination: signed, offset-free, keyset pagination (stdlib only)."""

from .cursor import BACKWARD, FORWARD, Cursor, decode_cursor, encode_cursor
from .errors import (
    CursorDecodeError,
    CursorError,
    CursorSpecMismatchError,
    CursorTamperedError,
)
from .paginator import Page, Paginator
from .sorting import ASC, DESC, SortSpec
from .store import Store

__all__ = [
    "ASC",
    "DESC",
    "BACKWARD",
    "FORWARD",
    "Cursor",
    "CursorDecodeError",
    "CursorError",
    "CursorSpecMismatchError",
    "CursorTamperedError",
    "Page",
    "Paginator",
    "SortSpec",
    "Store",
    "decode_cursor",
    "encode_cursor",
]

__version__ = "1.0.0"
