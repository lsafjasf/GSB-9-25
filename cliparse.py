"""cliparse -- 仅依赖标准库的命令行参数解析库。

特性:
  * 长选项 (--name / --name=value) 与短选项 (-n / -nvalue / 合并写法 -abc)
  * 布尔开关 (store_true / store_false / count) 与重复收集 (append)
  * 位置参数 (必选 / 可选 / nargs="*" 收集剩余)
  * 子命令: 全局选项与子命令选项分别解析
  * 明确的错误提示 + 用法说明; 退出行为可配置 (exit_on_error)

解析规则 (明确规定, 均有测试覆盖):
  1. 选项可以出现在位置参数之后 (交错解析), 顺序不影响结果。
  2. 带值选项的下一个 token 即使以减号开头也会被当作值,
     除非它恰好是某个已注册选项名或 "--"。
     若值恰好等于某个选项名, 请使用 "--opt=value" 形式。
  3. "--opt=" 与 "--opt ''" 都表示空字符串值。
  4. "--" 之后的所有内容一律当作位置参数。
  5. 子命令名出现之前的选项按全局解析; 之后先查子命令选项,
     查不到再回退到全局选项。同名选项: 子命令优先 (就近原则)。
  6. 单独一个 "-" 视为位置参数 (常用于表示 stdin)。
"""

from __future__ import annotations

import copy
import os
import sys

__all__ = ["Parser", "ParseError", "HelpRequested", "Namespace"]

_MISSING = object()

_ACTION_DEFAULTS = {
    "store": None,
    "append": list,
    "count": 0,
    "store_true": False,
    "store_false": True,
}


class ParseError(Exception):
    """解析失败。exit_code 供调用方在 exit_on_error=False 时自行退出。"""

    def __init__(self, message, prog=None, usage=None, exit_code=2):
        super().__init__(message)
        self.message = message
        self.prog = prog
        self.usage = usage
        self.exit_code = exit_code

    def format(self):
        lines = []
        if self.usage:
            lines.append(self.usage)
        lines.append("%s: error: %s" % (self.prog or "program", self.message))
        return "\n".join(lines)


class HelpRequested(Exception):
    """用户请求帮助 (-h/--help)。exit_on_error=False 时由调用方捕获。"""

    def __init__(self, text, exit_code=0):
        super().__init__(text)
        self.text = text
        self.exit_code = exit_code


class Namespace:
    """解析结果。属性访问 + as_dict()。"""

    def as_dict(self):
        return dict(self.__dict__)

    def __eq__(self, other):
        if isinstance(other, Namespace):
            return self.__dict__ == other.__dict__
        if isinstance(other, dict):
            return self.__dict__ == other
        return NotImplemented

    def __repr__(self):
        inner = ", ".join(
            "%s=%r" % (k, v) for k, v in sorted(self.__dict__.items())
        )
        return "Namespace(%s)" % inner


class Option:
    def __init__(self, *names, action="store", type=str, default=_MISSING,
                 required=False, choices=None, metavar=None, help=""):
        if not names:
            raise ValueError("option needs at least one name")
        if action not in _ACTION_DEFAULTS and action != "help":
            raise ValueError("unknown action: %r" % (action,))
        self.names = names
        self.long_names = [n for n in names if n.startswith("--")]
        self.short_names = [
            n for n in names if n.startswith("-") and not n.startswith("--")
        ]
        for n in self.short_names:
            if len(n) != 2:
                raise ValueError("short option must be one letter: %r" % n)
        if self.long_names:
            self.dest = self.long_names[0][2:].replace("-", "_")
        else:
            self.dest = self.short_names[0][1:]
        self.action = action
        self.type = type
        self.required = required
        self.choices = choices
        self.metavar = metavar
        self.help = help
        self.takes_value = action in ("store", "append")
        if default is _MISSING:
            default = _ACTION_DEFAULTS[action] if action != "help" else None
            if callable(default):
                default = default()
        self.default = default

    def display(self):
        return self.long_names[0] if self.long_names else self.short_names[0]

    def value_name(self):
        return self.metavar or self.dest.upper()

    def usage_fragment(self):
        name = self.display()
        if self.takes_value:
            frag = "%s %s" % (name, self.value_name())
        else:
            frag = name
        return frag if self.required else "[%s]" % frag


class Positional:
    def __init__(self, name, type=str, required=True, nargs=None,
                 choices=None, metavar=None, help=""):
        if nargs not in (None, "*"):
            raise ValueError("nargs must be None or '*'")
        if nargs == "*" and required:
            required = False
        self.name = name
        self.type = type
        self.required = required
        self.nargs = nargs
        self.choices = choices
        self.metavar = metavar or name.upper()
        self.help = help
        self.default = [] if nargs == "*" else None


class Parser:
    def __init__(self, prog=None, description="", exit_on_error=True,
                 add_help=True):
        self.prog = prog or (
            os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0]
            else "program"
        )
        self.description = description
        self.exit_on_error = exit_on_error
        self.options = []
        self.positionals = []
        self.subcommands = {}
        self._subcommand_help = {}
        if add_help:
            self.add_option("--help", "-h", action="help",
                            help="显示本帮助并退出")

    # ------------------------------------------------------------------ API

    def add_option(self, *names, **kwargs):
        opt = Option(*names, **kwargs)
        self.options.append(opt)
        return opt

    def add_positional(self, name, **kwargs):
        pos = Positional(name, **kwargs)
        if pos.nargs == "*" and any(p.nargs == "*" for p in self.positionals):
            raise ValueError("only one nargs='*' positional is allowed")
        self.positionals.append(pos)
        return pos

    def add_subcommand(self, name, help="", **kwargs):
        kwargs.setdefault("exit_on_error", self.exit_on_error)
        sub = Parser(prog="%s %s" % (self.prog, name), **kwargs)
        self.subcommands[name] = sub
        self._subcommand_help[name] = help
        return sub

    def parse(self, argv=None):
        """解析参数, 返回 Namespace。

        exit_on_error=True : 出错时打印用法+错误到 stderr 并 SystemExit(2)。
        exit_on_error=False: 出错时抛出 ParseError (携带 .exit_code),
                             -h/--help 抛出 HelpRequested。
        """
        if argv is None:
            argv = sys.argv[1:]
        try:
            return self._parse_scope(list(argv), [])
        except HelpRequested as h:
            if self.exit_on_error:
                sys.stdout.write(h.text + "\n")
                raise SystemExit(h.exit_code)
            raise
        except ParseError as e:
            if self.exit_on_error:
                sys.stderr.write(e.format() + "\n")
                raise SystemExit(e.exit_code)
            raise

    # ------------------------------------------------------------- internal

    def _error(self, message):
        return ParseError(message, prog=self.prog, usage=self.format_usage())

    def _parse_scope(self, argv, parents):
        ns = Namespace()
        ns.command = None
        ns.command_args = None
        for opt in self.options:
            setattr(ns, opt.dest, copy.deepcopy(opt.default))

        positionals = []
        matched_subcommand = False
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--":
                positionals.extend(argv[i + 1:])
                break
            if tok in self.subcommands:
                sub = self.subcommands[tok]
                ns.command = tok
                ns.command_args = sub._parse_scope(
                    argv[i + 1:], parents + [(self, ns)])
                matched_subcommand = True
                break
            if tok.startswith("--"):
                i = self._handle_long(ns, argv, i, parents)
            elif tok.startswith("-") and tok != "-":
                i = self._handle_short(ns, argv, i, parents)
            else:
                positionals.append(tok)
                i += 1

        # 进入子命令后, 全局必选位置参数不再强制 (常见 CLI 行为):
        # 已收集的仍按声明填充, 缺失的取默认值。
        self._assign_positionals(ns, positionals,
                                 enforce_required=not matched_subcommand)
        self._check_required(ns)
        return ns

    def _scopes(self, ns, parents):
        # 查找顺序: 自己 -> 最近的父作用域 -> ... -> 全局 (就近原则)
        return [(self, ns)] + list(reversed(parents))

    def _lookup_long(self, name, ns, parents):
        for scope, scope_ns in self._scopes(ns, parents):
            for opt in scope.options:
                if "--" + name in opt.long_names:
                    return opt, scope_ns
        return None, None

    def _lookup_short(self, ch, ns, parents):
        for scope, scope_ns in self._scopes(ns, parents):
            for opt in scope.options:
                if "-" + ch in opt.short_names:
                    return opt, scope_ns
        return None, None

    def _is_option_token(self, tok, ns, parents):
        """tok 是否是当前可见的已注册选项名 (或终止符)。"""
        if tok == "--":
            return True
        if tok.startswith("--"):
            return self._lookup_long(tok[2:].split("=", 1)[0], ns, parents)[0] \
                is not None
        if tok.startswith("-") and len(tok) == 2:
            return self._lookup_short(tok[1], ns, parents)[0] is not None
        return False

    def _take_value(self, argv, i, opt, ns, parents):
        if i + 1 >= len(argv):
            raise self._error(
                "option '%s' requires a value" % opt.display())
        nxt = argv[i + 1]
        if self._is_option_token(nxt, ns, parents):
            raise self._error(
                "option '%s' requires a value (got option '%s'; "
                "use '%s=<value>' if this is intended)"
                % (opt.display(), nxt, opt.display()))
        return nxt

    def _handle_long(self, ns, argv, i, parents):
        body = argv[i][2:]
        if "=" in body:
            name, value = body.split("=", 1)
            has_value = True
        else:
            name, value, has_value = body, None, False
        if not name:
            raise self._error("unrecognized option '%s'" % argv[i])
        opt, owner_ns = self._lookup_long(name, ns, parents)
        if opt is None:
            raise self._error("unrecognized option '--%s'" % name)
        if opt.takes_value:
            if not has_value:
                value = self._take_value(argv, i, opt, ns, parents)
                i += 1
            self._apply(owner_ns, opt, value)
        else:
            if has_value:
                raise self._error(
                    "option '--%s' does not take a value" % name)
            self._apply(owner_ns, opt, None)
        return i + 1

    def _handle_short(self, ns, argv, i, parents):
        cluster = argv[i][1:]
        pos = 0
        while pos < len(cluster):
            ch = cluster[pos]
            opt, owner_ns = self._lookup_short(ch, ns, parents)
            if opt is None:
                raise self._error("unrecognized option '-%s'" % ch)
            if opt.takes_value:
                rest = cluster[pos + 1:]
                if rest:
                    value = rest
                else:
                    value = self._take_value(argv, i, opt, ns, parents)
                    i += 1
                self._apply(owner_ns, opt, value)
                return i + 1
            self._apply(owner_ns, opt, None)
            pos += 1
        return i + 1

    def _apply(self, ns, opt, raw):
        if opt.action == "help":
            raise HelpRequested(self.format_help())
        if opt.takes_value:
            value = self._convert(opt.display(), opt, raw)
            if opt.action == "append":
                getattr(ns, opt.dest).append(value)
            else:
                setattr(ns, opt.dest, value)
        elif opt.action == "store_true":
            setattr(ns, opt.dest, True)
        elif opt.action == "store_false":
            setattr(ns, opt.dest, False)
        elif opt.action == "count":
            setattr(ns, opt.dest, getattr(ns, opt.dest) + 1)

    def _convert(self, what, spec, raw):
        if spec.type is str or spec.type is None:
            value = raw
        else:
            try:
                value = spec.type(raw)
            except (ValueError, TypeError):
                tname = getattr(spec.type, "__name__", str(spec.type))
                raise self._error(
                    "%s: invalid %s value: %r" % (what, tname, raw))
        if spec.choices is not None and value not in spec.choices:
            raise self._error(
                "%s: invalid choice: %r (choose from %s)"
                % (what, value, ", ".join(repr(c) for c in spec.choices)))
        return value

    def _assign_positionals(self, ns, values, enforce_required=True):
        idx = 0
        for spec in self.positionals:
            if spec.nargs == "*":
                setattr(ns, spec.name, [
                    self._convert("argument '%s'" % spec.name, spec, v)
                    for v in values[idx:]
                ])
                idx = len(values)
            elif idx < len(values):
                setattr(ns, spec.name, self._convert(
                    "argument '%s'" % spec.name, spec, values[idx]))
                idx += 1
            elif spec.required and enforce_required:
                raise self._error(
                    "missing required argument '%s'" % spec.name)
            else:
                setattr(ns, spec.name, spec.default)
        if idx < len(values):
            extra = values[idx:]
            shown = " ".join(extra[:3])
            if len(extra) > 3:
                shown += " ... (+%d more)" % (len(extra) - 3)
            raise self._error("unexpected extra argument(s): %s" % shown)

    def _check_required(self, ns):
        for opt in self.options:
            if opt.required and getattr(ns, opt.dest) is None:
                raise self._error(
                    "missing required option '%s'" % opt.display())

    # -------------------------------------------------------------- help

    def format_usage(self):
        parts = ["usage:", self.prog]
        for opt in self.options:
            parts.append(opt.usage_fragment())
        for p in self.positionals:
            frag = p.metavar + ("..." if p.nargs == "*" else "")
            parts.append(frag if p.required else "[%s]" % frag)
        if self.subcommands:
            parts.append("{%s}" % ",".join(self.subcommands))
            parts.append("...")
        return " ".join(parts)

    def format_help(self):
        lines = [self.format_usage()]
        if self.description:
            lines += ["", self.description]
        if self.positionals:
            lines += ["", "positional arguments:"]
            width = max(len(p.metavar) for p in self.positionals)
            for p in self.positionals:
                lines.append("  %-*s  %s" % (width, p.metavar, p.help))
        if self.options:
            lines += ["", "options:"]
            frags = []
            for opt in self.options:
                names = ", ".join(opt.short_names + opt.long_names)
                if opt.takes_value:
                    names += " " + opt.value_name()
                frags.append((names, opt.help))
            width = max(len(f) for f, _ in frags)
            for frag, helptext in frags:
                lines.append("  %-*s  %s" % (width, frag, helptext))
        if self.subcommands:
            lines += ["", "subcommands:"]
            width = max(len(n) for n in self.subcommands)
            for name, sub in self.subcommands.items():
                lines.append("  %-*s  %s"
                             % (width, name, self._subcommand_help[name]))
        return "\n".join(lines)
