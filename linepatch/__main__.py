"""CLI: python -m linepatch <apply|check|parse|reverse> ..."""
from __future__ import annotations

import argparse
import sys

from .applier import ApplyError, ApplyOptions, apply_patch_to_tree
from .parser import PatchParseError, format_patch, parse_patch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="linepatch")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_apply(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("patch")
        p.add_argument("--root", default=".")
        p.add_argument("--max-offset", type=int, default=50)
        p.add_argument("--reverse", action="store_true")
        return p

    add_apply("apply", "apply a patch to a directory tree")
    add_apply("check", "dry-run: verify a patch applies without writing")

    p = sub.add_parser("parse", help="parse a patch and print statistics")
    p.add_argument("patch")

    p = sub.add_parser("reverse", help="print the reversed patch to stdout")
    p.add_argument("patch")

    args = ap.parse_args(argv)
    try:
        with open(args.patch, "r", newline="") as f:
            patch = parse_patch(f.read())
    except (OSError, PatchParseError) as e:
        print(f"error: cannot parse patch: {e}", file=sys.stderr)
        return 2

    if args.cmd == "parse":
        print(f"files: {len(patch.files)}")
        for fp in patch.files:
            tag = ""
            if fp.is_new:
                tag = " (new file)"
            elif fp.is_delete:
                tag = " (deleted)"
            print(f"  {fp.old_path} -> {fp.new_path}{tag}: {len(fp.hunks)} hunks")
        print(f"total hunks: {patch.hunk_count}")
        return 0

    if args.cmd == "reverse":
        from .model import reversed_patch
        sys.stdout.write(format_patch(reversed_patch(patch)))
        return 0

    options = ApplyOptions(max_offset=args.max_offset)
    if args.cmd == "check":
        import copy, io, os
        # dry run: apply to in-memory copies
        from .applier import apply_to_text
        from .model import reversed_patch, FilePatch
        p = reversed_patch(patch) if args.reverse else patch
        try:
            for fp in p.files:
                path = os.path.join(args.root, fp.new_path)
                if fp.is_delete:
                    path = os.path.join(args.root, fp.old_path)
                if os.path.exists(path):
                    with open(path, "r", newline="") as f:
                        content = f.read()
                elif fp.is_new:
                    content = ""
                else:
                    raise ApplyError(
                        f"target file {fp.new_path} is missing and the patch "
                        f"does not create it")
                target = fp if not fp.is_delete else FilePatch(
                    fp.old_path, fp.old_path, fp.hunks)
                apply_to_text(content, target, options)
        except ApplyError as e:
            print(f"FAILED:\n{e}", file=sys.stderr)
            return 1
        print("patch applies cleanly")
        return 0

    try:
        results = apply_patch_to_tree(patch, root=args.root, options=options,
                                      reverse=args.reverse)
    except ApplyError as e:
        print(f"FAILED:\n{e}", file=sys.stderr)
        return 1
    for path, reports in results.items():
        for r in reports:
            note = f" (offset {r.offset:+d})" if r.offset else ""
            print(f"{path}: hunk #{r.hunk_index + 1} applied at line "
                  f"{r.applied_at}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
