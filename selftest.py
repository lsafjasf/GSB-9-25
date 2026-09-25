"""Self-test suite for b64hex.py.

Run:  python3 selftest.py            # vectors + strict cases + roundtrip
      python3 selftest.py --perf     # additionally run the benchmark

Exit code is 0 only if every check passes.
"""

import os
import sys
import time

from b64hex import CodecError, hex_encode, hex_decode, b64_encode, b64_decode

failures = []


def check(name, got, expected):
    ok = got == expected
    if not ok:
        failures.append(name)
    return ok


# --------------------------------------------------------------------------
# 1. Standard vectors (RFC 4648 sec. 10 + hand-computed hex / alphabet-diff
#    vectors), printed as a comparison table.
# --------------------------------------------------------------------------

def vector_tests():
    print("=" * 78)
    print("1. Standard vectors (expected vs. got)")
    print("=" * 78)

    b64_vectors = [  # (raw, standard, urlsafe)  -- RFC 4648 test vectors
        (b"", "", ""),
        (b"f", "Zg==", "Zg=="),
        (b"fo", "Zm8=", "Zm8="),
        (b"foo", "Zm9v", "Zm9v"),
        (b"foob", "Zm9vYg==", "Zm9vYg=="),
        (b"fooba", "Zm9vYmE=", "Zm9vYmE="),
        (b"foobar", "Zm9vYmFy", "Zm9vYmFy"),
        # vectors where the two alphabets actually differ:
        (b"\xfb", "+w==", "-w=="),
        (b"\xff\xfe", "//4=", "__4="),
        (b"\xfb\xff\xfe", "+//+", "-__-"),
        (b"\x14\xfb\x9c\x03\xd9\x7e", "FPucA9l+", "FPucA9l-"),
        (b"\x14\xfb\x9c\x03\xd9\x7f", "FPucA9l/", "FPucA9l_"),
    ]
    hex_vectors = [  # (raw, lowercase hex)
        (b"", ""),
        (b"\x00", "00"),
        (b"\xff", "ff"),
        (b"\xde\xad\xbe\xef", "deadbeef"),
        (b"\x01\x23\x45\x67\x89\xab\xcd\xef", "0123456789abcdef"),
        (b"Hello", "48656c6c6f"),
    ]

    hdr = "%-14s | %-8s | %-20s | %-20s | %s" % (
        "input", "kind", "expected", "got", "result")
    print(hdr)
    print("-" * 78)

    for raw, std, url in b64_vectors:
        for alpha, expected in (("standard", std), ("urlsafe", url)):
            label = "%r/%s" % (raw, alpha[:3])
            got = b64_encode(raw, alphabet=alpha)
            print("%-14s | %-8s | %-20s | %-20s | %s" % (
                label, "b64-enc", expected, got, "PASS" if check(label + " enc", got, expected) else "FAIL"))
            got2 = b64_decode(expected, alphabet=alpha)
            print("%-14s | %-8s | %-20s | %-20s | %s" % (
                label, "b64-dec", raw, got2, "PASS" if check(label + " dec", got2, raw) else "FAIL"))
        # unpadded variant must also round-trip
        unp = std.rstrip("=")
        got3 = b64_decode(b64_encode(raw, alphabet="standard", padding=False),
                          alphabet="standard", padding="forbid")
        print("%-14s | %-8s | %-20s | %-20s | %s" % (
            "%r/nopad" % raw, "b64-rt", raw, got3,
            "PASS" if check("nopad rt %r" % (raw,), got3, raw) else "FAIL"))
        if unp != std:
            check("nopad form %r" % (raw,),
                  b64_encode(raw, alphabet="standard", padding=False), unp)

    for raw, hx in hex_vectors:
        label = "%r" % (raw,)
        got = hex_encode(raw)
        print("%-14s | %-8s | %-20s | %-20s | %s" % (
            label, "hex-enc", hx, got, "PASS" if check(label + " hex enc", got, hx) else "FAIL"))
        got2 = hex_decode(hx)
        print("%-14s | %-8s | %-20s | %-20s | %s" % (
            label, "hex-dec", raw, got2, "PASS" if check(label + " hex dec", got2, raw) else "FAIL"))
        upper = hx.upper()
        got3 = hex_decode(upper)
        print("%-14s | %-8s | %-20s | %-20s | %s" % (
            label, "hex-dec-UC", raw, got3,
            "PASS" if check(label + " hex dec UC", got3, raw) else "FAIL"))
    print()


# --------------------------------------------------------------------------
# 2. Strict decoding: every case must raise CodecError at the expected
#    position (or succeed, for the explicit opt-in cases).
# --------------------------------------------------------------------------

def strict_tests():
    print("=" * 78)
    print("2. Strict decoding cases (position = expected 0-based error index)")
    print("=" * 78)
    hdr = "%-30s | %-8s | %-40s | %s"
    print(hdr % ("input / call", "pos", "error message", "result"))
    print("-" * 78)

    # (label, thunk, expected_position) -- expected_position None means "must succeed"
    cases = [
        # hex
        ("hex 'abc' (odd length)", lambda: hex_decode("abc"), 2),
        ("hex 'zz'", lambda: hex_decode("zz"), 0),
        ("hex '1g'", lambda: hex_decode("1g"), 1),
        ("hex '0x1f' (prefix rejected)", lambda: hex_decode("0x1f"), 0),
        ("hex '0XAB' allow_prefix", lambda: hex_decode("0XAB", allow_prefix=True), None),
        ("hex '0x' allow_prefix", lambda: hex_decode("0x", allow_prefix=True), None),
        ("hex '0xfg' allow_prefix", lambda: hex_decode("0xfg", allow_prefix=True), 3),
        ("hex non-ascii 'ä0'", lambda: hex_decode("ä0"), 0),
        # base64 length / padding
        ("b64 'A' (len%4==1)", lambda: b64_decode("A"), 0),
        ("b64 'ABCDE' (len%4==1)", lambda: b64_decode("ABCDE", padding="optional"), 4),
        ("b64 'Zg' padding=require", lambda: b64_decode("Zg"), 2),
        ("b64 'Zg==' padding=forbid", lambda: b64_decode("Zg==", padding="forbid"), 2),
        ("b64 'Zg==' padding=optional", lambda: b64_decode("Zg==", padding="optional"), None),
        ("b64 'Zg' padding=optional", lambda: b64_decode("Zg", padding="optional"), None),
        ("b64 'A===' (too much pad)", lambda: b64_decode("A==="), 3),
        ("b64 '====' (too much pad)", lambda: b64_decode("===="), 2),
        ("b64 'Z=g=' (pad in middle)", lambda: b64_decode("Z=g="), 1),
        ("b64 'Zg=' (padded len%4!=0)", lambda: b64_decode("Zg=", padding="optional"), 2),
        ("b64 '=' (lone pad)", lambda: b64_decode("=", padding="optional"), 0),
        # base64 characters / alphabets
        ("b64 'Zm9v!' (bad char)", lambda: b64_decode("Zm9v!"), 4),
        ("b64 '-w==' as standard", lambda: b64_decode("-w=="), 0),
        ("b64 '+w==' as urlsafe", lambda: b64_decode("+w==", alphabet="urlsafe"), 0),
        ("b64 '-w==' as urlsafe", lambda: b64_decode("-w==", alphabet="urlsafe"), None),
        ("b64 'Zm9 v' (space)", lambda: b64_decode("Zm9 v", padding="optional"), 3),
        # newlines
        ("b64 'Zm9v\\n' (newline rejected)", lambda: b64_decode("Zm9v\n"), 4),
        ("b64 'Zm9v\\n' allow_newlines", lambda: b64_decode("Zm9v\n", allow_newlines=True), None),
        ("b64 'Zm9\\nv' allow_newlines", lambda: b64_decode("Zm9\nv", allow_newlines=True), None),
        ("b64 'Zm9v\\t' (tab still bad)", lambda: b64_decode("Zm9v\t", allow_newlines=True), 4),
        # canonical trailing bits
        ("b64 'Zh==' (non-canonical)", lambda: b64_decode("Zh=="), 1),
        ("b64 'Zm9=' (non-canonical)", lambda: b64_decode("Zm9="), 2),
        ("b64 'Zh' optional (non-canon.)", lambda: b64_decode("Zh", padding="optional"), 1),
        ("b64 'Zg==' (canonical)", lambda: b64_decode("Zg=="), None),
    ]

    for label, thunk, expected_pos in cases:
        try:
            got = thunk()
        except CodecError as exc:
            ok = expected_pos is not None and exc.position == expected_pos
            if not ok:
                failures.append(label)
            print(hdr % (label, str(exc.position),
                         str(exc)[:40], "PASS" if ok else
                         "FAIL (wanted pos %s)" % expected_pos))
        else:
            ok = expected_pos is None
            if not ok:
                failures.append(label)
            print(hdr % (label, "-", "decoded to %r" % (got,),
                         "PASS" if ok else "FAIL (expected CodecError at %s)" % expected_pos))
    print()


# --------------------------------------------------------------------------
# 3. Round-trip: random byte strings, all lengths 0..8, plus lengths that
#    are not multiples of 3, across every switch combination.
# --------------------------------------------------------------------------

def roundtrip_tests():
    print("=" * 78)
    print("3. Round-trip tests")
    print("=" * 78)
    lengths = list(range(0, 9)) + [10, 11, 13, 16, 17, 100, 101, 102, 103, 1000, 1001]
    combos = [(a, p) for a in ("standard", "urlsafe") for p in (True, False)]
    total = 0
    for n in lengths:
        for trial in range(4):
            raw = os.urandom(n)
            check("hex rt %d/%d" % (n, trial), hex_decode(hex_encode(raw)), raw)
            for alpha, pad in combos:
                enc = b64_encode(raw, alphabet=alpha, padding=pad)
                mode = "optional" if pad else "forbid"
                got = b64_decode(enc, alphabet=alpha, padding=mode)
                check("b64 rt %s/%s %d/%d" % (alpha, pad, n, trial), got, raw)
                total += 1
            total += 1
    # deterministic edge bytes: all 256 byte values, and all-zero / all-0xFF
    for raw in (bytes(range(256)), b"\x00" * 64, b"\xff" * 64):
        check("hex rt edge", hex_decode(hex_encode(raw)), raw)
        for alpha, pad in combos:
            enc = b64_encode(raw, alphabet=alpha, padding=pad)
            got = b64_decode(enc, alphabet=alpha, padding="optional" if pad else "forbid")
            check("b64 rt edge %s/%s" % (alpha, pad), got, raw)
            total += 1
    print("round-trip checks: %d groups, all %s" % (
        total, "PASSED" if not failures else "see FAILURES below"))
    print()


# --------------------------------------------------------------------------
# 4. Performance on oversized inputs.
# --------------------------------------------------------------------------

def perf_tests():
    print("=" * 78)
    print("4. Performance (large inputs)")
    print("=" * 78)
    print("%-10s | %-12s | %12s | %12s" % ("size", "operation", "time", "throughput"))
    print("-" * 78)
    for mib in (1, 16, 64):
        raw = os.urandom(mib * 1024 * 1024)
        for name, enc, dec in (
            ("hex", hex_encode, hex_decode),
            ("b64-std", lambda b: b64_encode(b, alphabet="standard"),
             lambda s: b64_decode(s, alphabet="standard")),
            ("b64-url", lambda b: b64_encode(b, alphabet="urlsafe"),
             lambda s: b64_decode(s, alphabet="urlsafe")),
        ):
            t0 = time.perf_counter()
            enc_out = enc(raw)
            t1 = time.perf_counter()
            back = dec(enc_out)
            t2 = time.perf_counter()
            assert back == raw, "round-trip failed at %d MiB" % mib
            for op, dt in (("encode", t1 - t0), ("decode", t2 - t1)):
                print("%-10s | %-12s | %10.3f s | %9.1f MB/s" % (
                    "%d MiB" % mib, "%s %s" % (name, op), dt, mib / dt))
    print()


def main():
    vector_tests()
    strict_tests()
    roundtrip_tests()
    if "--perf" in sys.argv:
        perf_tests()
    if failures:
        print("FAILURES (%d):" % len(failures))
        for f in failures:
            print("  -", f)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
