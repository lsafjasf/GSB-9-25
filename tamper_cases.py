"""篡改用例集：对同一基准链施加各类篡改，打印检出结果。

运行：python3 tamper_cases.py
"""

import copy

from audit_chain import AuditChain, Record, compute_hash


def base_chain(n=20):
    c = AuditChain()
    for i in range(n):
        c.append({"op": "update", "row": i, "by": f"user{i % 5}"})
    return c


CASES = []


def case(name, expect):
    def deco(fn):
        CASES.append((name, expect, fn))
        return fn
    return deco


@case("基准：未篡改", "OK")
def no_tamper(recs):
    return recs


@case("内容被修改（第 7 条 data 被改，摘要未重算）", "modified @ 7")
def modify_content(recs):
    r = recs[7]
    recs[7] = Record(r.seq, r.prev, {"op": "DELETE *"}, r.hash)
    return recs


@case("中间记录被替换（第 10 条被伪造记录顶替并重算摘要）", "modified @ 10")
def replace_middle(recs):
    v = recs[10]
    recs[10] = Record(v.seq, v.prev, {"op": "forged"},
                      compute_hash(v.seq, v.prev, {"op": "forged"}))
    return recs


@case("条目被插入（第 4 条的副本插到位置 9）", "inserted @ 9")
def insert_copy(recs):
    recs.insert(9, recs[4])
    return recs


@case("条目被删除（删掉第 6 条）", "deleted @ 6")
def delete_one(recs):
    del recs[6]
    return recs


@case("链尾被截断（删掉最后 5 条）", "truncated @ 15")
def truncate_tail(recs):
    del recs[15:]
    return recs


@case("复制整条记录到别处（第 3 条原样追加到末尾）", "inserted @ 20")
def duplicate_record(recs):
    recs.append(recs[3])
    return recs


@case("两条链拼接（另一条合法链接到本链之后）", "inserted @ 20")
def concat_chains(recs):
    other = base_chain(10)
    return recs + other.records


@case("改内容并重算整条链（仅锚点可检出）", "modified @ 19（锚点比对）")
def recompute_all(recs):
    c = AuditChain()
    for i, r in enumerate(recs):
        c.append({"op": "EVIL"} if i == 5 else copy.deepcopy(r.data))
    return c.records


def main():
    base = base_chain()
    anchor = base.anchor()
    print(f"基准链：{anchor.count} 条，链尾摘要 {anchor.tail_hash[:16]}…\n")
    print(f"{'用例':<42} {'检出结果'}")
    print("-" * 88)
    for name, expect, fn in CASES:
        c = AuditChain()
        c.records = fn(copy.deepcopy(base.records))
        r = c.verify(anchor=anchor)
        status = "OK" if r.ok else f"{r.error} @ {r.position}"
        print(f"{name:<42} {status:<28} （预期：{expect}）")
        if not r.ok:
            print(f"{'':<42} └─ {r.detail}")


if __name__ == "__main__":
    main()
