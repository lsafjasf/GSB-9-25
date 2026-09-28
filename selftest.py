#!/usr/bin/env python3
"""
selftest.py — intcodec 对拍自测脚本（仅标准库，零依赖）。

内容：
  1) 与「逐元素参考实现」对拍：参考实现全部用最直白的循环写法，
     与 intcodec 的库实现互相验证 delta / rle / combo 及其逆变换。
  2) 随机序列往返对拍：含极值、长游程、全不同、交替值；逐元素一致。
  3) 边界：空序列、单元素、全相同、max/min 整数、混合极大值。
  4) AUTO 模式劣化回退检查（随机数据上 AUTO 不应选 COMBO）。
  5) 坏帧拒绝检查。
  6) 长度 1,000,000 的序列耗时与往返校验。
  7) 分块自适应：与整段模式对拍（同一序列分块/整段解码逐元素一致）、
     固定/自适应/显式区间三种切分、单块帧与整帧等价。
  8) 块表损坏：错误信息必须带可定位的块序号与字节偏移。

运行：python3 selftest.py
"""

import os
import random
import sys
import time

import intcodec as ic


# --------------------------------------------------------------------------
# 逐元素参考实现：刻意写得最朴素，独立于库实现
# --------------------------------------------------------------------------

def ref_delta_encode(seq):
    out = []
    for i in range(len(seq)):
        if i == 0:
            out.append(seq[i])
        else:
            out.append(seq[i] - seq[i - 1])
    return out


def ref_delta_decode(deltas):
    out = []
    acc = 0
    for i in range(len(deltas)):
        if i == 0:
            acc = deltas[i]
        else:
            acc = acc + deltas[i]
        out.append(acc)
    return out


def ref_rle_encode(seq):
    runs = []
    for i in range(len(seq)):
        x = seq[i]
        if len(runs) > 0 and runs[-1][0] == x:
            v, c = runs[-1]
            runs[-1] = (v, c + 1)
        else:
            runs.append((x, 1))
    return runs


def ref_rle_decode(runs):
    out = []
    for value, count in runs:
        for _ in range(count):
            out.append(value)
    return out


def ref_combo_encode(seq):
    return ref_rle_encode(ref_delta_encode(seq))


def ref_combo_decode(runs):
    return ref_delta_decode(ref_rle_decode(runs))


FAILURES = []


def check(cond, msg):
    if cond:
        print("  ok  %s" % msg)
    else:
        FAILURES.append(msg)
        print("FAIL  %s" % msg)


def check_raises(fn, needle, msg):
    """fn 必须抛 CodecError 且信息包含 needle（用于块表损坏定位检查）。"""
    try:
        fn()
    except ic.CodecError as exc:
        check(needle in str(exc), "%s（报错: %s）" % (msg, exc))
        return
    check(False, "%s（未抛 CodecError）" % msg)

# --------------------------------------------------------------------------
# 1. 参考实现对拍（变换层）
# --------------------------------------------------------------------------

def cross_check_transforms(seq, tag):
    d_lib = ic.delta_encode(seq)
    d_ref = ref_delta_encode(seq)
    check(d_lib == d_ref, "[%s] delta_encode 与参考实现逐元素一致" % tag)
    check(ref_delta_decode(d_lib) == list(seq), "[%s] delta 往返(参考解码)" % tag)
    check(ic.delta_decode(d_ref) == list(seq), "[%s] delta 往返(库解码/参考编码)" % tag)

    r_lib = ic.rle_encode(seq)
    r_ref = ref_rle_encode(seq)
    check(r_lib == r_ref, "[%s] rle_encode 与参考实现逐游程一致" % tag)
    check(ref_rle_decode(r_lib) == list(seq), "[%s] rle 往返(参考解码)" % tag)
    check(ic.rle_decode(r_ref) == list(seq), "[%s] rle 往返(库解码/参考编码)" % tag)

    c_lib = ic.combo_encode(seq)
    c_ref = ref_combo_encode(seq)
    check(c_lib == c_ref, "[%s] combo_encode 与参考实现一致" % tag)
    check(ref_combo_decode(c_lib) == list(seq), "[%s] combo 往返(参考解码)" % tag)
    check(ic.combo_decode(c_ref) == list(seq), "[%s] combo 往返(库解码/参考编码)" % tag)


# --------------------------------------------------------------------------
# 2. 帧层四模式往返 + 自描述模式识别
# --------------------------------------------------------------------------

def check_frames(seq, tag, expect_winner=None):
    for mode in (ic.RAW, ic.DELTA, ic.RLE, ic.COMBO):
        blob = ic.serialize(mode, seq)
        got_mode, got = ic.deserialize(blob)
        check(got_mode == mode, "[%s] %s 帧头模式可识别" % (tag, ic.MODE_NAMES[mode]))
        check(got == list(seq), "[%s] %s 帧往返逐元素一致 (%d B)"
              % (tag, ic.MODE_NAMES[mode], len(blob)))

    blob, report = ic.encode_auto(seq)
    used = ic.decoded_mode(blob)
    check(used == report["mode"], "[%s] AUTO 帧头与选择一致(%s)" % (tag, report["winner"]))
    check(ic.decode(blob) == list(seq), "[%s] AUTO 往返逐元素一致" % tag)
    if expect_winner is not None:
        check(report["winner"] == expect_winner,
              "[%s] AUTO 选择 %s（期望 %s）" % (tag, report["winner"], expect_winner))
    return report


# --------------------------------------------------------------------------
# 序列生成器
# --------------------------------------------------------------------------

LIMITS = [
    0, 1, -1, 2, -2, 63, 64, -64, -65,
    2 ** 31 - 1, -2 ** 31, 2 ** 31,
    2 ** 63 - 1, -2 ** 63, 2 ** 63, -2 ** 63 - 1,
    2 ** 127, -(2 ** 127 + 1), 2 ** 256 - 1, -2 ** 256,
]


def gen_random_mixed(rng, n):
    out = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.45:           # 小整数（含负）
            out.append(rng.randint(-100, 100))
        elif roll < 0.70:         # 64 位全范围（含极值）
            out.append(rng.choice(LIMITS) if rng.random() < 0.3
                       else rng.randint(-2 ** 63, 2 ** 63 - 1))
        elif roll < 0.85:         # 超长大整数
            out.append(rng.choice([2 ** 256, -2 ** 256, 2 ** 127 - 1]))
        else:                     # 制造局部游程
            out.append(out[-1] if out else 0)
    return out


def gen_runs(rng, n):
    out = []
    while len(out) < n:
        v = rng.choice(LIMITS) if rng.random() < 0.2 else rng.randint(-50, 50)
        out.extend([v] * rng.randint(1, 200)
                   if rng.random() < 0.85 else [v] * rng.randint(1, 3))
    return out[:n]


def gen_all_distinct(rng, n):
    start = rng.randint(-10 ** 6, 10 ** 6)
    step = rng.randint(64, 5000)  # 差分值较大 → DELTA 也不省
    return [start + i * step for i in range(n)]


def gen_alternating(rng, n):
    a, b = rng.randint(-1000, 1000), rng.randint(-1000, 1000)
    while b == a:
        b = rng.randint(-1000, 1000)
    return [a if i % 2 == 0 else b for i in range(n)]


def gen_random_walk(rng, n):
    x = rng.randint(-10 ** 9, 10 ** 9)
    out = [x]
    for _ in range(n - 1):
        x += rng.choice([-2, -1, 0, 1, 2, 3])  # 平稳小幅 + 偶发等差
        out.append(x)
    return out


def gen_timestamps(rng, n):
    t = 1_700_000_000
    out = []
    for _ in range(n):
        gap = rng.choice([10, 10, 10, 11, 10, 20])  # 几乎恒定的间隔
        t += gap
        out.append(t)
    return out


GENERATORS = {
    "random_mixed": gen_random_mixed,
    "runs": gen_runs,
    "all_distinct": gen_all_distinct,
    "alternating": gen_alternating,
    "random_walk": gen_random_walk,
    "timestamps": gen_timestamps,
}


def main():
    t0 = time.perf_counter()

    print("== 1. 固定边界用例 ==")
    edge_cases = [
        ("empty", [], None),
        ("single_zero", [0], None),
        ("single_max64", [2 ** 63 - 1], None),
        ("single_min64", [-2 ** 63], None),
        ("single_huge", [2 ** 512], None),
        ("single_huge_neg", [-(2 ** 512) - 3], None),
        ("all_same_small", [7] * 10000, "RLE"),
        ("all_same_max", [2 ** 63 - 1] * 1000, "RLE"),
        ("all_same_min", [-2 ** 63] * 1000, "RLE"),
        ("strict_linear", [5 + 3 * i for i in range(2000)], "COMBO"),
        ("timestamps", gen_timestamps(random.Random(0), 2000), "COMBO"),
    ]
    for tag, seq, expect in edge_cases:
        cross_check_transforms(seq, tag)
        check_frames(seq, tag, expect)

    print("== 2. 随机对拍（%d 组生成器 x 多种子） ==" % len(GENERATORS))
    rng_master = random.Random(20260925)
    for name, fn in GENERATORS.items():
        for trial in range(6):
            seed = rng_master.randrange(1 << 63)
            rng = random.Random(seed)
            n = rng.choice([0, 1, 2, 3, 7, 31, 127, 1000, 4096])
            seq = fn(rng, n)
            tag = "%s/trial%d/n%d/seed%d" % (name, trial, n, seed)
            cross_check_transforms(seq, tag)
            report = check_frames(seq, tag)
            # 劣化保护：COMBO 不比 RAW 小就绝不能被 AUTO 选中
            if report["sizes"]["COMBO"] >= report["sizes"]["RAW"]:
                check(report["winner"] != "COMBO",
                      "[%s] 劣化保护生效（未选 COMBO）" % tag)

    print("== 3. 极值混合：32/64/128/256 位边界 + 负数 ==")
    extreme = list(LIMITS) + [x + 1 for x in LIMITS] + [-x for x in LIMITS]
    rng = random.Random(7)
    rng.shuffle(extreme)
    cross_check_transforms(extreme, "extremes")
    check_frames(extreme, "extremes")

    print("== 4. 坏帧 / 非法输入必须报错 ==")
    try:
        ic.deserialize(b"")
        check(False, "空字节被拒绝")
    except ic.CodecError:
        check(True, "空字节被拒绝")
    try:
        ic.deserialize(b"XXXX" + bytes([ic.RAW]) + b"\x00")
        check(False, "坏 magic 被拒绝")
    except ic.CodecError:
        check(True, "坏 magic 被拒绝")
    try:
        ic.deserialize(ic.MAGIC + bytes([9]) + b"\x00")
        check(False, "坏模式字节被拒绝")
    except ic.CodecError:
        check(True, "坏模式字节被拒绝")
    truncated = ic.serialize(ic.COMBO, [1, 1, 2, 3, 3])
    try:
        ic.deserialize(truncated[:-2])
        check(False, "截断帧被拒绝")
    except ic.CodecError:
        check(True, "截断帧被拒绝")
    try:
        ic.encode([1, 2.0, 3])
        check(False, "非 int 输入被拒绝")
    except ic.CodecError:
        check(True, "非 int 输入被拒绝")
    try:
        ic.encode([1, True])
        check(False, "bool 输入被拒绝")
    except ic.CodecError:
        check(True, "bool 输入被拒绝")

    print("== 5. 长度 1,000,000 序列耗时 ==")
    big_rng = random.Random(42)
    big = gen_timestamps(big_rng, 1_000_000)
    t = time.perf_counter()
    blob, report = ic.encode_auto(big)
    t_enc = time.perf_counter() - t
    t = time.perf_counter()
    back = ic.decode(blob)
    t_dec = time.perf_counter() - t
    check(back == big, "百万序列（时间戳）往返逐元素一致，模式=%s" % report["winner"])
    print("     timestamps: n=1,000,000 mode=%s 编码 %.3fs 解码 %.3fs"
          % (report["winner"], t_enc, t_dec))

    big2 = gen_random_mixed(random.Random(43), 1_000_000)
    t = time.perf_counter()
    blob2, report2 = ic.encode_auto(big2)
    t_enc2 = time.perf_counter() - t
    t = time.perf_counter()
    back2 = ic.decode(blob2)
    t_dec2 = time.perf_counter() - t
    check(back2 == big2, "百万序列（随机混合）往返逐元素一致，模式=%s" % report2["winner"])
    print("     random_mix: n=1,000,000 mode=%s 编码 %.3fs 解码 %.3fs"
          % (report2["winner"], t_enc2, t_dec2))

    # 单模式计时（原始对照）
    t = time.perf_counter()
    blob_raw = ic.serialize(ic.RAW, big)
    t_raw = time.perf_counter() - t
    print("     对照 RAW 编码时间戳序列：%.3fs" % t_raw)

    print("== 6. 分块自适应：与整段模式对拍 ==")
    rng_master = random.Random(20260929)
    for name, fn in GENERATORS.items():
        for trial in range(4):
            seed = rng_master.randrange(1 << 63)
            rng = random.Random(seed)
            n = rng.choice([0, 1, 2, 3, 7, 31, 127, 1000, 4096])
            seq = fn(rng, n)
            n = len(seq)  # 个别生成器对 n=0 仍产出 1 元素，以实际长度为准
            tag = "%s/trial%d/n%d" % (name, trial, n)
            whole = ic.decode(ic.encode_auto(seq)[0])
            check(whole == list(seq), "[%s] 整段 AUTO 基准一致" % tag)
            for cfg, kw in [("fixed7", {"chunk_size": 7}),
                            ("fixed128", {"chunk_size": 128}),
                            ("fixed1M", {"chunk_size": max(1, n)}),
                            ("adaptive64", {"adaptive": True, "block_size": 64}),
                            ("adaptive512", {"adaptive": True, "block_size": 512})]:
                blob, rep = ic.encode_chunked(seq, **kw)
                got = ic.decode(blob)
                check(got == whole,
                      "[%s] 分块(%s, %d块) 与整段解码逐元素一致"
                      % (tag, cfg, rep["chunks"]))
                check(ic.decoded_mode(blob) == ic.CHUNKED,
                      "[%s] 分块(%s) 帧头模式为 CHUNKED" % (tag, cfg))
                # 块表可独立解析且覆盖全序列
                entries = ic.parse_chunk_table(blob)
                check(sum(e["elem_count"] for e in entries) == n,
                      "[%s] 分块(%s) 块表元素总数 == n" % (tag, cfg))

    # 单块帧：分块帧退化为单块时，块内模式与整段 analyze 选择一致
    for tag, seq, _ in edge_cases:
        if not seq:
            continue
        blob, rep = ic.encode_chunked(seq, chunk_size=len(seq))
        whole_mode = ic.analyze(seq)["mode"]
        entry = ic.parse_chunk_table(blob)[0]
        check(rep["chunks"] == 1 and entry["mode"] == whole_mode,
              "[%s] 单块帧模式与整段选择一致(%s)" % (tag, ic.MODE_NAMES[whole_mode]))
        check(ic.decode(blob) == ic.decode(ic.serialize(whole_mode, seq)),
              "[%s] 单块帧与整段帧解码一致" % tag)

    # 显式区间切分 + 非法区间拒绝
    seq = gen_random_walk(random.Random(1), 1000)
    spans = [(0, 300), (300, 301), (301, 1000)]
    blob = ic.serialize_chunked(seq, spans=spans)
    check(ic.decode(blob) == seq, "显式区间分块往返一致")
    check_raises(lambda: ic.serialize_chunked(seq, spans=[(0, 500), (499, 1000)]),
                 "块#1", "重叠区间被拒绝并定位块号")
    check_raises(lambda: ic.serialize_chunked(seq, spans=[(0, 999)]),
                 "覆盖", "未覆盖全序列被拒绝")
    check_raises(lambda: ic.serialize_chunked(seq, spans=[(0, 1001)]),
                 "块#0", "区间越界被拒绝并定位块号")

    print("== 7. 块表损坏：错误可定位 ==")
    seq = (gen_timestamps(random.Random(2), 500)
           + [42] * 500
           + gen_random_mixed(random.Random(3), 500))
    blob, rep = ic.encode_chunked(seq, chunk_size=100)
    entries = ic.parse_chunk_table(blob)
    check(rep["chunks"] == 15, "分块数符合预期(15块)")

    # 7.1 篡改第 3 块的模式字节
    bad = bytearray(blob)
    off = entries[3]["table_offset"]
    bad[off] = 9
    check_raises(lambda: ic.decode(bytes(bad)), "块#3",
                 "块#3 模式字节损坏 → 报错含块号与偏移")
    # 7.2 篡改末块的 body_len（放大 → 末块 body 越界）
    bad = bytearray(blob)
    off = entries[-1]["table_offset"]
    pos = ic.read_uvarint(blob, off + 1)[1]  # 跳过 elem_count，定位 body_len 字段
    bad[pos] = 0xFF
    check_raises(lambda: ic.decode(bytes(bad)), "块#14",
                 "末块 body_len 篡改 → 报错定位到末块")
    # 7.3 篡改第 0 块元素数为 0
    bad = bytearray(blob)
    bad[entries[0]["table_offset"] + 1] = 0
    check_raises(lambda: ic.decode(bytes(bad)), "块#0",
                 "块#0 元素数非法 → 报错含块号")
    # 7.4 块表截断（只保留前 2 个表项）
    cut = entries[2]["table_offset"]
    check_raises(lambda: ic.decode(blob[:cut]), "块#2",
                 "块表截断 → 报错定位到缺失的块#2")
    # 7.5 帧尾截断（body 缺失）
    check_raises(lambda: ic.decode(blob[:-10]), "块#14",
                 "末块 body 截断 → 报错定位到最后一块")
    # 7.6 帧尾多余字节
    check_raises(lambda: ic.decode(blob + b"\x00"), "块表",
                 "body 总长与帧长不符被拒绝")
    # 7.7 块 body 内容损坏（翻转第 7 块 body 首字节高位制造非法 varint 链）
    bad = bytearray(blob)
    boff = entries[7]["body_offset"]
    bad[boff] = 0xFF
    try:
        ic.decode(bytes(bad))
        # 0xFF 也可能恰好仍是合法 varint 前缀，此时要求结果必不一致
        check(False, "块#7 body 损坏必须报错")
    except ic.CodecError as exc:
        check("块#7" in str(exc), "块#7 body 损坏 → 报错含块号（%s）" % exc)
    # 7.8 非分块帧走 parse_chunk_table 报错
    check_raises(lambda: ic.parse_chunk_table(ic.serialize(ic.RAW, [1, 2])),
                 "不是分块帧", "对整段帧解析块表被拒绝")

    elapsed = time.perf_counter() - t0
    print()
    if FAILURES:
        print("失败 %d 项：" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        sys.exit(1)
    print("全部通过（用时 %.1fs）。" % elapsed)


if __name__ == "__main__":
    main()
