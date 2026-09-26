#!/usr/bin/env python3
"""Differential fuzzer: semverlib vs. the independent reference (refimpl).

Randomly generates version pairs and range expressions, then checks:

* comparison:   sign(semverlib.compare(a, b)) == refimpl.ref_cmp(a, b)
* equality:     (a == b) == (ref_cmp(a, b) == 0)
* matching:     Range.match(v, include_prerelease=f) == ref_match(...)  (both flags)
* parse agreement: both implementations accept/reject the same strings

On any mismatch the counterexample is greedily minimized (delta-debugging
style) and printed as a runnable reproduction snippet; exit code is 1.

Usage:
    python3 differential.py [--iterations N] [--seed S] [--demo-bug]

``--demo-bug`` injects a deliberate discrepancy into the reference so the
minimizer's output can be inspected.
"""

import argparse
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refimpl
from semverlib import Range, Version, compare
from semverlib.errors import RangeParseError, VersionParseError

IDENT_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-"


# ---------------------------------------------------------------- generators

def gen_ident(rng):
    if rng.random() < 0.4:
        return str(rng.randint(0, 10 ** rng.randint(1, 6)))
    text = "".join(rng.choice(IDENT_ALPHABET) for _ in range(rng.randint(1, 5)))
    if text.isdigit() and len(text) > 1 and text.startswith("0"):
        text = "a" + text  # keep numeric prerelease identifiers zero-pad free
    return text


def gen_version(rng):
    major = rng.randint(0, 9) if rng.random() < 0.85 else rng.randint(0, 10 ** 8)
    minor = rng.randint(0, 9)
    patch = rng.randint(0, 9)
    text = "%d.%d.%d" % (major, minor, patch)
    if rng.random() < 0.5:
        text += "-" + ".".join(gen_ident(rng) for _ in range(rng.randint(1, 3)))
    if rng.random() < 0.3:
        text += "+" + ".".join(gen_ident(rng) for _ in range(rng.randint(1, 2)))
    return text


def mutate(rng, text):
    """Random single-step mutation, used to generate invalid inputs."""
    if not text:
        return text + rng.choice(IDENT_ALPHABET)
    pos = rng.randrange(len(text))
    kind = rng.random()
    if kind < 0.35:
        return text[:pos] + rng.choice(IDENT_ALPHABET + ".-+ ") + text[pos:]
    if kind < 0.7:
        return text[:pos] + text[pos + 1 :]
    return text[:pos] + rng.choice(IDENT_ALPHABET + ".-+_v") + text[pos + 1 :]


def gen_maybe_invalid_version(rng):
    text = gen_version(rng)
    for _ in range(rng.randint(1, 3)):
        text = mutate(rng, text)
    return text


def gen_partial(rng):
    precision = rng.choice([1, 2, 3, 3, 3])
    parts = [str(rng.randint(0, 5)) for _ in range(precision)]
    if precision < 3 and rng.random() < 0.35:
        parts.append(rng.choice(["x", "X", "*"]))
    text = ".".join(parts)
    if precision == 3 and rng.random() < 0.3:
        text += "-" + ".".join(gen_ident(rng) for _ in range(rng.randint(1, 2)))
    if precision == 3 and rng.random() < 0.15:
        text += "+" + gen_ident(rng)
    return text


def gen_comparator(rng):
    op = rng.choice(["", "", "", ">=", "<=", ">", "<", "=", "==", "!=", "!",
                     "~", "^"])
    if rng.random() < 0.05:
        return op + rng.choice(["*", "x"])
    return op + gen_partial(rng)


def gen_range(rng):
    if rng.random() < 0.03:
        return ""
    sets = []
    for _ in range(rng.randint(1, 2)):
        sets.append(" ".join(gen_comparator(rng)
                             for _ in range(rng.randint(1, 3))))
    return " || ".join(sets)


def gen_maybe_invalid_range(rng):
    text = gen_range(rng)
    for _ in range(rng.randint(1, 2)):
        text = mutate(rng, text)
    return text


# ------------------------------------------------------------------- checks

def sign(x):
    return (x > 0) - (x < 0)


def observed_ref_cmp(a, b, demo_bug=False):
    result = refimpl.ref_cmp(a, b)
    if demo_bug and "-" in a and "-" in b:
        result = -result
    return result


def check_compare(a, b, demo_bug=False):
    la, lb = Version.parse(a), Version.parse(b)
    lib_sign = sign(compare(la, lb))
    ref_sign = observed_ref_cmp(a, b, demo_bug)
    return lib_sign == ref_sign and (la == lb) == (ref_sign == 0)


def observed_ref_match(range_text, version_text, flag, demo_bug=False):
    result = refimpl.ref_match(range_text, version_text, include_prerelease=flag)
    if demo_bug and flag and "-" in version_text:
        result = not result
    return result


def check_match(range_text, version_text, flag, demo_bug=False):
    lib = Range.parse(range_text).match(version_text, include_prerelease=flag)
    ref = observed_ref_match(range_text, version_text, flag, demo_bug)
    return lib == ref


def check_version_parse_agreement(text):
    try:
        Version.parse(text)
        lib_ok = True
    except VersionParseError:
        lib_ok = False
    try:
        refimpl.parse_key(text)
        ref_ok = True
    except ValueError:
        ref_ok = False
    return lib_ok == ref_ok


def check_range_parse_agreement(text):
    try:
        Range.parse(text)
        lib_ok = True
    except RangeParseError:
        lib_ok = False
    try:
        refimpl._parse_range_sets(text)
        ref_ok = True
    except ValueError:
        ref_ok = False
    return lib_ok == ref_ok


# -------------------------------------------------------------- minimizers

def simplify_version(text):
    """Candidate simpler versions derived from ``text``."""
    candidates = set()
    try:
        v = Version.parse(text)
    except VersionParseError:
        for i in range(1, len(text)):
            candidates.add(text[:i])
        return candidates
    if v.build:
        candidates.add(str(Version(v.major, v.minor, v.patch, v.prerelease, ())))
    if v.prerelease:
        candidates.add(str(Version(v.major, v.minor, v.patch, (), v.build)))
        for k in range(1, len(v.prerelease)):
            candidates.add(str(Version(v.major, v.minor, v.patch,
                                       v.prerelease[:k], v.build)))
        for k, ident in enumerate(v.prerelease):
            for replacement in ("0", "a", "1"):
                if ident != replacement:
                    new = v.prerelease[:k] + (replacement,) + v.prerelease[k + 1:]
                    candidates.add(str(Version(v.major, v.minor, v.patch,
                                               new, v.build)))
    for field in ("major", "minor", "patch"):
        for small in (0, 1):
            if getattr(v, field) != small:
                candidates.add(str(Version(
                    small if field == "major" else v.major,
                    small if field == "minor" else v.minor,
                    small if field == "patch" else v.patch,
                    v.prerelease, v.build)))
    candidates.discard(text)
    return candidates


def _complexity(state):
    """Simplicity measure: shorter, lexicographically smaller is simpler."""
    return tuple(len(s) for s in state) + tuple(state)


def minimize(state, fails, candidate_fn):
    """Greedy fixpoint minimization: keep simplifying while it still fails.

    Only strictly simpler states (by ``_complexity``) are accepted, and
    visited states are remembered, so the loop always terminates.
    """
    visited = {state}
    improved = True
    while improved:
        improved = False
        for candidate in candidate_fn(state):
            if (candidate not in visited
                    and _complexity(candidate) < _complexity(state)
                    and fails(candidate)):
                state = candidate
                visited.add(candidate)
                improved = True
                break
    return state


def minimize_compare(a, b, demo_bug=False):
    def fails(state):
        return not check_compare(state[0], state[1], demo_bug)

    def candidates(state):
        a, b = state
        for s in simplify_version(a):
            yield (s, b)
        for s in simplify_version(b):
            yield (a, s)

    return minimize((a, b), fails, candidates)


def range_simplifications(range_text):
    """Candidate simpler range strings."""
    candidates = set()
    sets = range_text.split("||")
    if len(sets) > 1:
        for i in range(len(sets)):
            candidates.add("||".join(sets[:i] + sets[i + 1:]).strip())
    for set_index, comparator_set in enumerate(sets):
        tokens = comparator_set.split()
        if len(tokens) > 1:
            for i in range(len(tokens)):
                new_tokens = tokens[:i] + tokens[i + 1:]
                new_set = " ".join(new_tokens)
                candidates.add("||".join(
                    sets[:set_index] + [new_set] + sets[set_index + 1:]).strip())
    # shrink numbers and drop prerelease/build parts inside tokens
    for m in re.finditer(r"\d+", range_text):
        for small in ("0", "1"):
            if m.group(0) != small:
                candidates.add(range_text[:m.start()] + small + range_text[m.end():])
    for m in re.finditer(r"[-+][0-9A-Za-z.-]+", range_text):
        candidates.add(range_text[:m.start()] + range_text[m.end():])
    candidates.discard(range_text)
    return {c for c in candidates if c.strip(" |") != "" or range_text == ""}


def minimize_match(range_text, version_text, flag, demo_bug=False):
    def fails(state):
        r, v = state
        try:
            return not check_match(r, v, flag, demo_bug)
        except (RangeParseError, VersionParseError):
            return False

    def candidates(state):
        r, v = state
        for s in range_simplifications(r):
            yield (s, v)
        for s in simplify_version(v):
            yield (r, s)

    return minimize((range_text, version_text), fails, candidates)


def minimize_string(text, fails):
    """Delta-debugging style substring deletion (length strictly shrinks)."""
    n = 2
    while len(text) >= 2:
        chunk = max(1, len(text) // n)
        changed = False
        for i in range(0, len(text), chunk):
            candidate = text[:i] + text[i + chunk:]
            if candidate and fails(candidate):
                text = candidate
                n = max(2, n - 1)
                changed = True
                break
        if not changed:
            if chunk == 1:
                break
            n = min(len(text), n * 2)
    return text


# ----------------------------------------------------------------- reports

def report_compare(a, b, demo_bug):
    a, b = minimize_compare(a, b, demo_bug)
    lib_sign = sign(compare(Version.parse(a), Version.parse(b)))
    ref_sign = observed_ref_cmp(a, b, demo_bug)
    print("COUNTEREXAMPLE (comparison), minimized:", file=sys.stderr)
    print("  a = %r\n  b = %r" % (a, b), file=sys.stderr)
    print("  semverlib: %d   refimpl: %d" % (lib_sign, ref_sign), file=sys.stderr)
    print("  reproduce:", file=sys.stderr)
    print("    from semverlib import Version, compare", file=sys.stderr)
    print("    import refimpl", file=sys.stderr)
    print("    compare(Version.parse(%r), Version.parse(%r)), refimpl.ref_cmp(%r, %r)"
          % (a, b, a, b), file=sys.stderr)


def report_match(range_text, version_text, flag, demo_bug):
    (range_text, version_text), flag = minimize_match(
        range_text, version_text, flag, demo_bug), flag
    lib = Range.parse(range_text).match(version_text, include_prerelease=flag)
    ref = observed_ref_match(range_text, version_text, flag, demo_bug)
    print("COUNTEREXAMPLE (range match), minimized:", file=sys.stderr)
    print("  range = %r\n  version = %r\n  include_prerelease = %r"
          % (range_text, version_text, flag), file=sys.stderr)
    print("  semverlib: %r   refimpl: %r" % (lib, ref), file=sys.stderr)
    print("  reproduce:", file=sys.stderr)
    print("    from semverlib import match", file=sys.stderr)
    print("    import refimpl", file=sys.stderr)
    print("    match(%r, %r, include_prerelease=%r), "
          "refimpl.ref_match(%r, %r, %r)"
          % (range_text, version_text, flag, range_text, version_text, flag),
          file=sys.stderr)


def report_parse(kind, text, check):
    text = minimize_string(text, lambda s: not check(s))
    print("COUNTEREXAMPLE (%s parse agreement), minimized:" % kind,
          file=sys.stderr)
    print("  input = %r" % text, file=sys.stderr)


# -------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--demo-bug", action="store_true",
                        help="inject a deliberate discrepancy to demo the "
                             "counterexample minimizer")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    stats = {"compare": 0, "match": 0, "version_parse": 0, "range_parse": 0}

    for iteration in range(args.iterations):
        # 1. version pair comparison
        a, b = gen_version(rng), gen_version(rng)
        stats["compare"] += 1
        if not check_compare(a, b, args.demo_bug):
            print("iteration %d: comparison mismatch" % iteration, file=sys.stderr)
            report_compare(a, b, args.demo_bug)
            return 1

        # 2. range matching (both prerelease flags)
        range_text, version_text = gen_range(rng), gen_version(rng)
        for flag in (False, True):
            stats["match"] += 1
            try:
                ok = check_match(range_text, version_text, flag, args.demo_bug)
            except (RangeParseError, VersionParseError) as err:
                print("iteration %d: unexpected error %r" % (iteration, err),
                      file=sys.stderr)
                return 1
            if not ok:
                print("iteration %d: range match mismatch" % iteration,
                      file=sys.stderr)
                report_match(range_text, version_text, flag, args.demo_bug)
                return 1

        # 3. parse agreement on (mostly invalid) mutated inputs
        bad_version = gen_maybe_invalid_version(rng)
        stats["version_parse"] += 1
        if not check_version_parse_agreement(bad_version):
            print("iteration %d: version parse disagreement" % iteration,
                  file=sys.stderr)
            report_parse("version", bad_version, check_version_parse_agreement)
            return 1

        bad_range = gen_maybe_invalid_range(rng)
        stats["range_parse"] += 1
        if not check_range_parse_agreement(bad_range):
            print("iteration %d: range parse disagreement" % iteration,
                  file=sys.stderr)
            report_parse("range", bad_range, check_range_parse_agreement)
            return 1

    print("OK: no divergence in %d iterations" % args.iterations)
    print("checks performed: %s" % ", ".join(
        "%s=%d" % kv for kv in sorted(stats.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
