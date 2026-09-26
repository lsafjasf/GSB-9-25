"""Parse errors that carry the exact position of the problem."""


class _PositionedError(ValueError):
    """Base class for parse errors with a 0-based position into the source."""

    label = "parse error"

    def __init__(self, message, position, source):
        self.message = message
        self.position = position
        self.source = source
        super().__init__(
            "%s: %s (position %d): %r" % (self.label, message, position, source)
        )

    @property
    def pointer(self):
        """Render the source with a caret under the offending position."""
        return "%s\n%s^" % (self.source, " " * self.position)


class VersionParseError(_PositionedError):
    """Raised when a version string does not follow SemVer 2.0.0."""

    label = "invalid version"


class RangeParseError(_PositionedError):
    """Raised when a range expression is malformed."""

    label = "invalid range"
