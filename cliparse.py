"""cliparse -- a small command-line argument parsing library (stdlib only).

Features
--------
- Long options:   --verbose, --output FILE, --output=FILE, --output=
- Short options:  -v, -o FILE, -oFILE, -o=FILE
- Short clusters: -abc  (flags),  -aboFILE  (last one takes a value)
- Boolean switches, repeated options (collect into a list / count flags)
- Positional arguments, intermixed freely with options
- Subcommands with their own options, plus defaults
- "--" terminator: everything after it is positional
- Configurable exit behaviour: raise ParseError (carrying an exit code)
  instead of killing the process

Parsing rules (see USAGE.md for the full spec)
----------------------------------------------
1. Options may appear before, after, or between positional arguments.
2. A token is an option iff it starts with '-' and is not:
   "-", a negative number (-1, -3.14), or anything after "--".
3. Space-separated option values: the next token is consumed as the value
   unless it looks like an option (rule 2).  --opt=-value always works.
4. "--name=" gives an empty-string value; "--name ''" likewise.
5. Subcommand options are parsed by the subcommand's parser; a name that
   exists both globally and in the subcommand is resolved by position:
   before the subcommand token -> global, after it -> subcommand.
"""

from __future__ import annotations

import difflib
import re
import sys

__all__ = ["Option", "Parser", "ParseError", "HelpRequested", "Result"]

_NEGATIVE_NUMBER_RE = re.compile(r"^-\d")


class ParseError(Exception):
    """Raised on any parsing failure when exit_on_error is False.

    Attributes:
        message:   human-readable error description.
        usage:     one-line usage string for the parser that failed.
        exit_code: process exit code a caller should use (always 2).
    """

    def __init__(self, message, usage=None, exit_code=2):
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.exit_code = exit_code

    def format(self):
        if self.usage:
            return "%s\nerror: %s" % (self.usage, self.message)
        return "error: %s" % self.message


class HelpRequested(Exception):
    """Raised for -h/--help when exit_on_error is False.

    ``text`` holds the full help message; ``exit_code`` is 0.
    """

    def __init__(self, text):
        super().__init__(text)
        self.text = text
        self.exit_code = 0


class Option:
    """A single named option.  Create via Parser.add_option()."""

    def __init__(self, names, takes_value=False, repeat=False, default=None,
                 type=None, metavar=None, help="", required=False):
        self.long_names = [n[2:] for n in names if n.startswith("--")]
        self.short_names = [n[1:] for n in names
                            if n.startswith("-") and not n.startswith("--")]
        if not self.long_names and not self.short_names:
            raise ValueError("option needs at least one name like '--foo' or '-f'")
        for n in self.short_names:
            if len(n) != 1:
                raise ValueError("short option names must be one character: %r" % n)
        self.takes_value = takes_value
        self.repeat = repeat
        self.default = default
        self.type = type
        self.metavar = metavar
        self.help = help
        self.required = required
        base = self.long_names[0] if self.long_names else self.short_names[0]
        self.dest = base.replace("-", "_")

    def display(self):
        names = ["--" + n for n in self.long_names]
        names += ["-" + n for n in self.short_names]
        text = ", ".join(names)
        if self.takes_value:
            text += " " + (self.metavar or "VALUE")
        return text

    def type_name(self):
        return getattr(self.type, "__name__", str(self.type))


class Positional:
    def __init__(self, name, required=True, help=""):
        self.name = name
        self.required = required
        self.help = help


class Result:
    """Outcome of a successful parse.

    - Option values are attributes:  result.verbose, result.output ...
      (dest = first long name with '-' -> '_').
    - result.positionals: list of positional argument strings.
    - result.subcommand:  name of the subcommand used, or None.
    - result.sub:         the subcommand's own Result, or None.
    """

    def __init__(self):
        self.options = {}
        self.positionals = []
        self.subcommand = None
        self.sub = None

    def __getattr__(self, name):
        # Only called when normal lookup fails.
        options = self.__dict__.get("options", {})
        if name in options:
            return options[name]
        raise AttributeError(name)

    def get(self, name, default=None):
        return self.options.get(name, default)


class Parser:
    def __init__(self, prog=None, description="", exit_on_error=True,
                 add_help=True, subcommand_required=False):
        self.prog = prog or (sys.argv[0] if sys.argv else "prog")
        self.description = description
        self.exit_on_error = exit_on_error
        self.subcommand_required = subcommand_required
        self._options = []
        self._long = {}
        self._short = {}
        self._positionals = []
        self._subcommands = {}
        self._help_option = None
        if add_help:
            self._help_option = self.add_option(
                "--help", "-h", help="show this help message and exit")

    # -------------------------------------------------------------- setup

    def add_option(self, *names, **kwargs):
        opt = Option(names, **kwargs)
        for n in opt.long_names:
            if n in self._long:
                raise ValueError("duplicate option: --%s" % n)
            self._long[n] = opt
        for n in opt.short_names:
            if n in self._short:
                raise ValueError("duplicate option: -%s" % n)
            self._short[n] = opt
        self._options.append(opt)
        return opt

    def add_positional(self, name, required=True, help=""):
        self._positionals.append(Positional(name, required, help))

    def add_subcommand(self, name, help="", **kwargs):
        kwargs.setdefault("exit_on_error", self.exit_on_error)
        child = Parser(prog="%s %s" % (self.prog, name), **kwargs)
        child.help = help
        self._subcommands[name] = child
        return child

    # ------------------------------------------------------------- parsing

    def parse(self, argv=None):
        """Parse argv (defaults to sys.argv[1:]) and return a Result.

        On failure: if exit_on_error is True, print usage + error to stderr
        and raise SystemExit(2); otherwise raise ParseError (which carries
        .exit_code so the caller decides how to exit).
        """
        if argv is None:
            argv = sys.argv[1:]
        try:
            return self._parse(list(argv))
        except HelpRequested as req:
            if self.exit_on_error:
                print(req.text)
                raise SystemExit(req.exit_code)
            raise
        except ParseError as err:
            if self.exit_on_error:
                print(err.format(), file=sys.stderr)
                raise SystemExit(err.exit_code)
            raise

    def _parse(self, tokens):
        result = Result()
        for opt in self._options:
            if opt.repeat:
                result.options[opt.dest] = [] if opt.takes_value else 0
            elif opt.default is not None:
                result.options[opt.dest] = opt.default
            else:
                result.options[opt.dest] = None if opt.takes_value else False

        i = 0
        positional_only = False
        while i < len(tokens):
            tok = tokens[i]
            if positional_only:
                result.positionals.append(tok)
            elif tok == "--":
                positional_only = True
            elif tok.startswith("--") and len(tok) > 2:
                i = self._parse_long(tokens, i, result)
            elif self._is_short_cluster(tok):
                i = self._parse_shorts(tokens, i, result)
            elif tok in self._subcommands and result.subcommand is None:
                child = self._subcommands[tok]
                result.subcommand = tok
                result.sub = child._parse(tokens[i + 1:])
                self._finish(result)
                return result
            else:
                result.positionals.append(tok)
            i += 1
        self._finish(result)
        return result

    def _is_short_cluster(self, tok):
        return (tok.startswith("-") and not tok.startswith("--")
                and tok != "-" and not _NEGATIVE_NUMBER_RE.match(tok))

    def _looks_like_option(self, tok):
        if tok == "--":
            return True
        if tok in ("-" + s for s in self._short):
            return True
        if tok.startswith("--") and tok[2:].partition("=")[0] in self._long:
            return True
        if _NEGATIVE_NUMBER_RE.match(tok):
            return False
        return tok.startswith("-") and tok != "-"

    def _take_value(self, tokens, i, opt, inline):
        """Return (value, next_index) for an option expecting a value."""
        if inline is not None:
            return inline, i
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is None or self._looks_like_option(nxt):
            raise self._error("option %s requires a value" % opt.display())
        return nxt, i + 1

    def _parse_long(self, tokens, i, result):
        tok = tokens[i]
        name, eq, inline = tok[2:].partition("=")
        opt = self._long.get(name)
        if opt is None:
            raise self._unknown("--" + name)
        if opt.takes_value:
            value, i = self._take_value(tokens, i, opt, inline if eq else None)
            self._store(result, opt, self._convert(opt, value))
        else:
            if eq:
                raise self._error("option %s does not take a value"
                                  % opt.display())
            self._store(result, opt)
        return i

    def _parse_shorts(self, tokens, i, result):
        cluster = tokens[i][1:]
        j = 0
        while j < len(cluster):
            opt = self._short.get(cluster[j])
            if opt is None:
                raise self._unknown("-" + cluster[j])
            if opt.takes_value:
                rest = cluster[j + 1:]
                if rest.startswith("="):
                    rest = rest[1:]
                value, i = self._take_value(tokens, i, opt, rest or None)
                self._store(result, opt, self._convert(opt, value))
                return i
            self._store(result, opt)
            j += 1
        return i

    def _store(self, result, opt, value=True):
        if opt is self._help_option:
            raise HelpRequested(self.format_help())
        if opt.takes_value:
            if opt.repeat:
                result.options[opt.dest].append(value)
            else:
                result.options[opt.dest] = value
        else:
            if opt.repeat:
                result.options[opt.dest] += 1
            else:
                result.options[opt.dest] = True

    def _convert(self, opt, raw):
        if opt.type is None:
            return raw
        try:
            return opt.type(raw)
        except (TypeError, ValueError):
            raise self._error("option %s: invalid %s value: %r"
                              % (opt.display(), opt.type_name(), raw))

    def _finish(self, result):
        for opt in self._options:
            if opt.required and opt.takes_value and \
                    result.options[opt.dest] in (None, []):
                raise self._error("missing required option: %s" % opt.display())
        required = [p for p in self._positionals if p.required]
        if len(result.positionals) < len(required):
            missing = required[len(result.positionals)]
            raise self._error(
                "missing required positional argument: %s" % missing.name)
        if self._subcommands and self.subcommand_required \
                and result.subcommand is None:
            raise self._error("missing subcommand (expected one of: %s)"
                              % ", ".join(sorted(self._subcommands)))

    # ------------------------------------------------------------- errors

    def _error(self, message):
        return ParseError(message, usage=self.format_usage())

    def _unknown(self, token):
        known = ["--" + n for n in self._long]
        known += ["-" + n for n in self._short]
        known += sorted(self._subcommands)
        close = difflib.get_close_matches(token, known, n=1)
        msg = "unknown option: %s" % token
        if close:
            msg += " (did you mean %s?)" % close[0]
        return self._error(msg)

    # -------------------------------------------------------------- help

    def format_usage(self):
        parts = ["usage:", self.prog]
        if self._options:
            parts.append("[OPTIONS]")
        for p in self._positionals:
            parts.append("<%s>" % p.name if p.required else "[%s]" % p.name)
        if self._subcommands:
            parts.append("COMMAND" if self.subcommand_required else "[COMMAND]")
        return " ".join(parts)

    def format_help(self):
        lines = [self.format_usage()]
        if self.description:
            lines += ["", self.description]
        if self._positionals:
            lines += ["", "Positional arguments:"]
            for p in self._positionals:
                lines.append("  %-22s %s" % (p.name, p.help))
        if self._options:
            lines += ["", "Options:"]
            for opt in self._options:
                lines.append("  %-22s %s" % (opt.display(), opt.help))
        if self._subcommands:
            lines += ["", "Commands:"]
            for name, child in sorted(self._subcommands.items()):
                lines.append("  %-22s %s" % (name, child.help))
        return "\n".join(lines)
