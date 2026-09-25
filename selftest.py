"""自测：标准向量对拍 + 严格解码用例 + 全边界长度往返。

运行：python3 selftest.py
全部通过退出码 0，任一失败退出码 1。
"""

import random
import sys

from strict_hex import HexDecodeError, hex_decode, hex_encode
from strict_b64 import B64DecodeError, b64_decode, b64_encode

failures = 0
checks = 0


def report(ok, name, detail=""):
    global failures, checks
    checks += 1
    if not ok:
        failures += 1
    print("  %-4s  %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


# ---------------------------------------------------------------- 标准向量
def test_vectors():
    print("== 1. 标准向量对拍（RFC 4648 测试向量 + 字母表差异向量）==")
    print("  %-46s %-14s %-14s %s" % ("用例", "期望", "实际", "结果"))
    # (字节, 标准带填充, URL安全带填充)
    vectors = [
        (b"", "", ""),
        (b"f", "Zg==", "Zg=="),
        (b"fo", "Zm8=", "Zm8="),
        (b"foo", "Zm9v", "Zm9v"),
        (b"foob", "Zm9vYg==", "Zm9vYg=="),
        (b"fooba", "Zm9vYmE=", "Zm9vYmE="),
        (b"foobar", "Zm9vYmFy", "Zm9vYmFy"),
        (b"\xfb", "+w==", "-w=="),
        (b"\xff", "/w==", "_w=="),
        (b"\xfb\xff", "+/8=", "-_8="),
        (b"\xfb\xff\xfe", "+//+", "-__-"),
        (b"\x00\x01\x02", "AAEC", "AAEC"),
    ]
    for raw, std, url in vectors:
        got_std = b64_encode(raw)
        got_url = b64_encode(raw, urlsafe=True)
        got_unp = b64_encode(raw, padding=False)
        ok = got_std == std and got_url == url and got_unp == std.rstrip("=")
        report(ok, "encode %-10r" % raw,
               "期望 %s/%s 实际 %s/%s" % (std, url, got_std, got_url))
        ok = b64_decode(std) == raw and b64_decode(url, urlsafe=True) == raw
        report(ok, "decode %-14s" % std, "-> %r" % raw)
        ok = b64_decode(std.rstrip("="), require_padding=False) == raw
        report(ok, "decode 无填充 %-10s" % std.rstrip("="), "-> %r" % raw)

    hex_vectors = [
        (b"", ""),
        (b"\x00", "00"),
        (b"\xff", "ff"),
        (b"\xde\xad\xbe\xef", "deadbeef"),
        (b"\x01\x23\x45\x67\x89\xab\xcd\xef", "0123456789abcdef"),
    ]
    for raw, text in hex_vectors:
        got = hex_encode(raw)
        report(got == text, "hex encode %r" % raw,
               "期望 %s 实际 %s" % (text, got))
        report(hex_decode(text) == raw, "hex decode %s" % text)
        report(hex_decode(text.upper()) == raw, "hex decode 大写 %s" % text.upper())
        report(hex_decode("0x" + text, allow_prefix=True) == raw,
               "hex decode 0x 前缀（显式允许）")
    print()


# ---------------------------------------------------------------- 严格解码
def expect_error(name, fn, expect_pos, expect_kw):
    global checks, failures
    checks += 1
    try:
        fn()
    except (HexDecodeError, B64DecodeError) as e:
        ok = e.position == expect_pos and expect_kw in str(e)
        if not ok:
            failures += 1
        print("  %-4s  %-46s -> %s" % ("PASS" if ok else "FAIL", name, e))
    except Exception as e:  # noqa: BLE001
        failures += 1
        print("  FAIL  %-46s -> 异常类型错误: %r" % (name, e))
    else:
        failures += 1
        print("  FAIL  %-46s -> 未报错（宽松通过）" % name)


def test_strict():
    print("== 2. 严格解码用例（必须报错并指出位置）==")
    # hex
    expect_error("hex: 0x 前缀默认拒绝", lambda: hex_decode("0xAB"), 0, "前缀")
    expect_error("hex: 奇数长度", lambda: hex_decode("ABC"), 3, "奇数")
    expect_error("hex: 非法字符位置 0", lambda: hex_decode("GG"), 0, "非法字符")
    expect_error("hex: 非法字符位置 1", lambda: hex_decode("A "), 1, "非法字符")
    expect_error("hex: 非 ASCII 字符", lambda: hex_decode("0ä"), 1, "非法字符")

    # base64 字符与字母表
    expect_error("b64: 非法字符位置 3", lambda: b64_decode("Zm9$"), 3, "非法字符")
    expect_error("b64: URL 字符混入标准字母表",
                 lambda: b64_decode("-w=="), 0, "非法字符")
    expect_error("b64: 标准字符混入 URL 字母表",
                 lambda: b64_decode("+w==", urlsafe=True), 0, "非法字符")
    expect_error("b64: 空格不忽略", lambda: b64_decode("Zm 9v"), 2, "非法字符")

    # 长度
    expect_error("b64: 无填充长度模4余1", lambda: b64_decode("Z", require_padding=False),
                 1, "长度非法")
    expect_error("b64: 填充缺失", lambda: b64_decode("Zg"), 2, "填充缺失")
    expect_error("b64: 填充缺失(长输入)",
                 lambda: b64_decode("Zm9vYmE"), 7, "填充缺失")

    # 填充
    expect_error("b64: 无填充模式拒绝 '='",
                 lambda: b64_decode("Zg==", require_padding=False), 2, "不允许")
    expect_error("b64: 填充过多", lambda: b64_decode("Zg==="), 4, "过多")
    expect_error("b64: '=' 出现在中间", lambda: b64_decode("Z=g="), 2, "末尾")
    expect_error("b64: 含填充总长非4倍数", lambda: b64_decode("Zg="), 3, "长度非法")
    expect_error("b64: 仅填充", lambda: b64_decode("=="), 2, "长度非法")

    # 规范性：非零填充位
    expect_error("b64: 非零填充位(==)", lambda: b64_decode("Zh=="), 1, "填充位")
    expect_error("b64: 非零填充位(=)", lambda: b64_decode("Zmb="), 2, "填充位")
    expect_error("b64: 非零填充位(无填充)",
                 lambda: b64_decode("Zh", require_padding=False), 1, "填充位")

    # 换行
    expect_error("b64: 换行默认拒绝", lambda: b64_decode("Zm9v\n"), 4, "换行")
    ok = b64_decode("Zm9v\r\nYmFy", allow_newlines=True) == b"foobar"
    report(ok, "b64: 显式允许换行后可解码")
    print()


# ---------------------------------------------------------------- 往返
def test_roundtrip():
    print("== 3. 随机字节串往返（含全部边界长度 0..8 与非 3 倍数长度）==")
    rng = random.Random(20260925)
    lengths = list(range(0, 9)) + [9, 10, 11, 15, 16, 17, 31, 32, 33,
                                   100, 101, 102, 255, 256, 257, 1023, 1024]
    for ln in lengths:
        raw = bytes(rng.randrange(256) for _ in range(ln))
        ok = hex_decode(hex_encode(raw)) == raw
        for urlsafe in (False, True):
            for padding in (True, False):
                s = b64_encode(raw, urlsafe=urlsafe, padding=padding)
                ok = ok and b64_decode(s, urlsafe=urlsafe,
                                       require_padding=padding) == raw
        # 换行插入后显式允许解码
        s = b64_encode(raw)
        wrapped = "\r\n".join(s[i:i + 4] for i in range(0, len(s), 4))
        ok = ok and b64_decode(wrapped, allow_newlines=True) == raw
        report(ok, "往返长度 %d" % ln, "hex + b64 四种模式 + 换行")
    print()


def main():
    test_vectors()
    test_strict()
    test_roundtrip()
    print("== 汇总：%d 项检查，%d 项失败 ==" % (checks, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
