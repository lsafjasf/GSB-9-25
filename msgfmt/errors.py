"""Exception types for msgfmt."""


class MessageFormatError(Exception):
    """Base class for all msgfmt errors."""


class TemplateSyntaxError(MessageFormatError):
    """A message template could not be parsed."""

    def __init__(self, message, pos):
        super().__init__(f"{message} (at position {pos})")
        self.pos = pos


class ConfigError(MessageFormatError):
    """The language configuration is invalid."""


class MessageNotFoundError(MessageFormatError):
    """The message key was not found in the locale or its fallback chain."""

    def __init__(self, key, locale):
        super().__init__(f"message {key!r} not found for locale {locale!r} "
                         f"or any of its fallbacks")
        self.key = key
        self.locale = locale


class RenderError(MessageFormatError):
    """A message could not be rendered with the given arguments."""
