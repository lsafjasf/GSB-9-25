"""Declarative, deterministic text redaction library (Python 3, stdlib only).

Core ideas
----------
- Rules are declarative: regex pattern, replacement mode (fixed mask or
  consistent pseudonym), priority, and whether the rule may match inside
  already-replaced regions.
- Conflict resolution is deterministic:
    1. Rules are applied in rounds, grouped by descending ``priority``.
    2. Within one round, all candidate matches are collected, then sorted by
       (longest match, earliest start, declaration order) and greedily
       accepted; overlapping losers are recorded as shadowed.
    3. Regions replaced by higher-priority rounds are locked. Later rules may
       not match inside them unless the rule sets ``allow_in_replaced``.
- Pseudonyms are derived as HMAC-SHA256(key, rule_name + NUL + value), so the
  same value maps to the same token within and across batches for a given
  key, and different keys yield different tokens.
- Fullwidth/halfwidth normalization (engine option) and case-insensitive
  matching (rule option) are explicit opt-ins and are OFF by default, so
  non-sensitive text is never rewritten implicitly.

CLI
---
    python3 redactor.py --rules rules.sample.json --key secret \
        [--normalize-fullwidth] [--report report.json] [-o out.txt] [input]
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import hmac
import json
import re
import sys
from dataclasses import dataclass, field

# 1:1 char mapping, so offsets are preserved between the normalized view and
# the original text. U+FF01..U+FF5E -> U+0021..U+007E, U+3000 -> U+0020.
_FULLWIDTH_MAP = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20


def normalize_fullwidth(text: str) -> str:
    """Return a fullwidth->halfwidth normalized view (same length as input)."""
    return text.translate(_FULLWIDTH_MAP)


@dataclass(frozen=True)
class Replacement:
    """How a match is replaced.

    type: "mask" (fixed string) or "pseudonym" (keyed consistent token).
    """

    kind: str = "mask"
    value: str = "***"          # mask: literal replacement
    prefix: str = ""            # pseudonym: token prefix, e.g. "USER_"
    token_length: int = 12      # pseudonym: hex chars of the HMAC digest

    @staticmethod
    def from_dict(d: dict) -> "Replacement":
        kind = d.get("type", "mask")
        if kind not in ("mask", "pseudonym"):
            raise ValueError(f"unknown replacement type: {kind!r}")
        return Replacement(
            kind=kind,
            value=d.get("value", "***"),
            prefix=d.get("prefix", ""),
            token_length=int(d.get("token_length", 12)),
        )


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    priority: int = 0
    allow_in_replaced: bool = False
    case_insensitive: bool = False
    replacement: Replacement = field(default_factory=Replacement)
    order: int = 0  # declaration order, set by load_rules

    @staticmethod
    def from_dict(d: dict, order: int) -> "Rule":
        return Rule(
            name=d["name"],
            pattern=d["pattern"],
            priority=int(d.get("priority", 0)),
            allow_in_replaced=bool(d.get("allow_in_replaced", False)),
            case_insensitive=bool(d.get("case_insensitive", False)),
            replacement=Replacement.from_dict(d.get("replacement", {})),
            order=order,
        )

    def compile(self) -> "re.Pattern[str]":
        flags = re.IGNORECASE if self.case_insensitive else 0
        return re.compile(self.pattern, flags)


def load_rules(path_or_file) -> list[Rule]:
    """Load a rule set from a JSON file path or file object."""
    if hasattr(path_or_file, "read"):
        data = json.load(path_or_file)
    else:
        with open(path_or_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    raw_rules = data["rules"] if isinstance(data, dict) else data
    rules = [Rule.from_dict(d, i) for i, d in enumerate(raw_rules)]
    names = [r.name for r in rules]
    if len(set(names)) != len(names):
        raise ValueError("rule names must be unique")
    return rules


@dataclass
class _Piece:
    text: str
    locked_by: str | None = None  # name of the rule that produced this piece
    origin: str | None = None     # original text this piece replaced


@dataclass
class _Candidate:
    start: int
    end: int
    rule: Rule
    matched: str  # text as seen by the matcher (scan-view slice)


class Redactor:
    """Apply a rule set deterministically."""

    def __init__(self, rules: list[Rule], key: bytes | str = b"",
                 normalize_fullwidth_opt: bool = False):
        self.rules = list(rules)
        self.key = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        self.normalize = normalize_fullwidth_opt
        self._compiled = {r.name: r.compile() for r in self.rules}
        needs_key = any(r.replacement.kind == "pseudonym" for r in self.rules)
        if needs_key and not self.key:
            raise ValueError(
                "pseudonym rules require an explicit key for reproducibility")

    # -- pseudonym ---------------------------------------------------------
    def _pseudonym(self, rule: Rule, value: str) -> str:
        msg = rule.name.encode("utf-8") + b"\x00" + value.encode("utf-8")
        digest = hmac.new(self.key, msg, hashlib.sha256).hexdigest()
        rep = rule.replacement
        return rep.prefix + digest[: rep.token_length]

    def _replacement_text(self, rule: Rule, matched: str) -> str:
        rep = rule.replacement
        if rep.kind == "mask":
            return rep.value
        return self._pseudonym(rule, matched)

    # -- main entry --------------------------------------------------------
    def redact(self, text: str) -> tuple[str, dict]:
        pieces = [_Piece(text)]
        report_rules: dict[str, dict] = {
            r.name: {"name": r.name, "priority": r.priority,
                     "replacements": 0, "spans": []}
            for r in self.rules
        }
        shadowed: list[dict] = []

        # Group rules by priority, highest first; declaration order kept
        # inside each group.
        groups: dict[int, list[Rule]] = {}
        for rule in self.rules:
            groups.setdefault(rule.priority, []).append(rule)

        for priority in sorted(groups, reverse=True):
            group = groups[priority]
            full = "".join(p.text for p in pieces)
            offsets = _piece_offsets(pieces)

            candidates: list[_Candidate] = []
            for rule in group:
                regex = self._compiled[rule.name]
                if rule.allow_in_replaced:
                    # Scan the current text, including replaced regions.
                    view = full
                    bounds = None
                else:
                    # Scan the "original view": locked regions are shown as
                    # their original text, so candidates covered by higher
                    # priorities are still found and reported as shadowed.
                    view, bounds = _original_view(pieces, offsets)
                    vstarts = [b[0] for b in bounds]
                if self.normalize:
                    view = normalize_fullwidth(view)
                for m in regex.finditer(view):
                    if m.end() == m.start():
                        continue
                    if bounds is None:
                        candidates.append(_Candidate(
                            m.start(), m.end(), rule, view[m.start():m.end()]))
                        continue
                    lockers, span = _translate(bounds, vstarts, m.start(), m.end())
                    if lockers:
                        shadowed.append({
                            "rule": rule.name,
                            "start": m.start(),
                            "end": m.end(),
                            "matched": view[m.start():m.end()],
                            "reason": "locked-by:" + ",".join(sorted(lockers)),
                        })
                        continue
                    candidates.append(_Candidate(
                        span[0], span[1], rule, view[m.start():m.end()]))

            accepted = _resolve(candidates, shadowed)
            if accepted:
                pieces = self._apply(pieces, offsets, accepted, report_rules)

        output = "".join(p.text for p in pieces)
        report = {
            "rules": [report_rules[r.name] for r in self.rules],
            "shadowed": shadowed,
            "summary": {
                "input_length": len(text),
                "output_length": len(output),
                "total_replacements": sum(
                    v["replacements"] for v in report_rules.values()),
                "total_shadowed": len(shadowed),
            },
        }
        return output, report

    # -- internals ---------------------------------------------------------
    def _apply(self, pieces, offsets, accepted, report_rules):
        """Splice accepted replacements into the piece list."""
        accepted = sorted(accepted, key=lambda c: c.start)
        new_pieces: list[_Piece] = []
        pos = 0
        for cand in accepted:
            if cand.start > pos:
                new_pieces.extend(_clip(pieces, offsets, pos, cand.start))
            repl = self._replacement_text(cand.rule, cand.matched)
            new_pieces.append(_Piece(repl, locked_by=cand.rule.name,
                                     origin=cand.matched))
            entry = report_rules[cand.rule.name]
            entry["replacements"] += 1
            entry["spans"].append({
                "start": cand.start,
                "end": cand.end,
                "matched": cand.matched,
                "replacement": repl,
            })
            pos = cand.end
        total = offsets[-1] + len(pieces[-1].text)
        new_pieces.extend(_clip(pieces, offsets, pos, total))
        return new_pieces


def _piece_offsets(pieces: list[_Piece]) -> list[int]:
    offsets = []
    acc = 0
    for p in pieces:
        offsets.append(acc)
        acc += len(p.text)
    return offsets


def _piece_index(offsets: list[int], pos: int) -> int:
    """Index of the piece containing ``pos`` (offsets are piece starts)."""
    return bisect.bisect_right(offsets, pos) - 1


def _original_view(pieces: list[_Piece], offsets: list[int]):
    """Build the scan view where locked pieces show their original text.

    Returns (view, bounds); bounds[i] = (view_start, view_end, current_start,
    locked_by) for piece i, used to translate view spans to current-text
    spans and to detect matches touching locked regions.
    """
    parts = []
    bounds = []
    vpos = 0
    for piece, cur_start in zip(pieces, offsets):
        shown = piece.origin if piece.locked_by else piece.text
        parts.append(shown)
        bounds.append((vpos, vpos + len(shown), cur_start, piece.locked_by))
        vpos += len(shown)
    return "".join(parts), bounds


def _translate(bounds, vstarts, start, end):
    """Map a view span to a current-text span.

    Returns (lockers, span). ``lockers`` is the set of rule names whose
    locked regions the span touches (span is then None); otherwise ``span``
    is the (start, end) pair in current-text coordinates.
    """
    lockers = set()
    cur_start = cur_end = None
    i = max(0, bisect.bisect_right(vstarts, start) - 1)
    while i < len(bounds) and bounds[i][0] < end:
        vs, ve, cs, locked = bounds[i]
        if locked:
            lockers.add(locked)
        else:
            lo = max(start, vs) + (cs - vs)
            hi = min(end, ve) + (cs - vs)
            if cur_start is None:
                cur_start = lo
            cur_end = hi
        i += 1
    if lockers:
        return lockers, None
    return lockers, (cur_start, cur_end)


def _clip(pieces, offsets, a, b) -> list[_Piece]:
    """Old pieces clipped to the [a, b) range of the full string."""
    out = []
    i = _piece_index(offsets, a) if a < b else len(pieces)
    while i < len(pieces) and offsets[i] < b:
        lo = max(a, offsets[i]) - offsets[i]
        hi = min(b, offsets[i] + len(pieces[i].text)) - offsets[i]
        if hi > lo:
            out.append(_Piece(pieces[i].text[lo:hi], pieces[i].locked_by,
                              pieces[i].origin))
        i += 1
    return out


def _resolve(candidates: list[_Candidate],
             shadowed: list[dict]) -> list[_Candidate]:
    """Deterministic conflict resolution within one priority round.

    Sort by (longest match, earliest start, declaration order), then greedily
    accept non-overlapping candidates. Rejected ones are reported as shadowed.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-(c.end - c.start), c.start, c.rule.order),
    )
    accepted: list[_Candidate] = []
    starts: list[int] = []  # starts of accepted, kept sorted
    for cand in ordered:
        i = bisect.bisect_right(starts, cand.start)
        overlap = False
        if i > 0 and accepted[i - 1].end > cand.start:
            overlap = True
        if not overlap and i < len(accepted) and accepted[i].start < cand.end:
            overlap = True
        if overlap:
            shadowed.append({
                "rule": cand.rule.name,
                "start": cand.start,
                "end": cand.end,
                "matched": cand.matched,
                "reason": "overlap:higher-or-equal-priority-match",
            })
            continue
        accepted.insert(i, cand)
        starts.insert(i, cand.start)
    return accepted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic declarative redactor")
    ap.add_argument("input", nargs="?", help="input file (default: stdin)")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--rules", required=True, help="rule set JSON file")
    ap.add_argument("--key", default="", help="pseudonym key (or env REDACTOR_KEY)")
    ap.add_argument("--normalize-fullwidth", action="store_true",
                    help="match against a fullwidth-normalized view (opt-in)")
    ap.add_argument("--report", help="write JSON report to this file")
    args = ap.parse_args(argv)

    import os
    key = args.key or os.environ.get("REDACTOR_KEY", "")

    rules = load_rules(args.rules)
    if args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    redactor = Redactor(rules, key=key, normalize_fullwidth_opt=args.normalize_fullwidth)
    output, report = redactor.redact(text)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
    else:
        sys.stdout.write(output)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    else:
        json.dump(report["summary"], sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
