"""Exception hierarchy for cursor_pagination."""


class CursorError(Exception):
    """Base class for all Cursor related errors."""


class CursorDecodeError(CursorError):
    """The cursor token is malformed (bad encoding / bad payload)."""


class CursorTamperedError(CursorError):
    """The cursor signature does not match: it was tampered with,
    or it was signed with a different secret."""


class CursorSpecMismatchError(CursorError):
    """The cursor is valid but was issued for a different sort spec."""
