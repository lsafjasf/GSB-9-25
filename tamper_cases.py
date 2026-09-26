"""篡改用例集：构造各类篡改并展示检出结果。python3 tamper_cases.py"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_chain import GENESIS, make_record, verify_records


def build(n, tag="r"):
    recs, prev = [], GENESIS
    for i in range(n):
        r = make_record(i, prev, {"op": f"{tag}{i}", "amount": i * 10})
        recs.append(r)
        prev = bytes.fromhex(r["digest"])
    return recs


BASE = build(12)
TRUE_HEAD = BASE[-1]["digest"]

cases = []

# 0) 基准：未篡改
cases.append(("0. 未篡改（对照组）", copy.deepcopy(BASE), None, "ok"))

# 1) 内容被修改（摘要未重算）
r = copy.deepcopy(BASE)
r[5]["content"] = {"op": "forged", "amount": 99999}
cases.append(("1. 修改第5条内容", r, None, "content_modified @5"))

# 2) 内容被修改且重算该条摘要（后继未重算）
r = copy.deepcopy(BASE)
r[5] = make_record(5, bytes.fromhex(r[5]["prev"]), {"op": "forged"})
cases.append(("2. 修改第5条并重算其摘要", r, None, "content_modified @5/6"))

# 3) 条目被插入（伪造一条并接好 prev）
r = copy.deepcopy(BASE)
forged = make_record(6, bytes.fromhex(r[5]["digest"]), {"op": "evil"})
r.insert(6, forged)
cases.append(("3. 在位置6插入伪造条目", r, None, "entry_inserted @6/7"))

# 4) 复制整条记录到别处
r = copy.deepcopy(BASE)
r.insert(9, copy.deepcopy(r[2]))
cases.append(("4. 把第2条完整复制到位置9", r, None, "entry_inserted @9"))

# 5) 条目被删除
r = copy.deepcopy(BASE)
del r[8]
cases.append(("5. 删除第8条", r, None, "entry_deleted @8"))

# 6) 链尾被截断（有可信链头）
r = copy.deepcopy(BASE)[:9]
cases.append(("6. 截断链尾（保留前9条，有可信链头）", r, TRUE_HEAD, "truncated @9"))

# 7) 链尾被截断（无可信链头 => 内部自洽，不可检出，说明检查点的必要性）
r = copy.deepcopy(BASE)[:9]
cases.append(("7. 截断链尾（无可信链头，不可检出）", r, None, "ok（需检查点机制）"))

# 8) 两条链拼接
r = copy.deepcopy(BASE) + build(6, tag="x")
cases.append(("8. 另一条链拼接到链尾", r, None, "entry_inserted @12"))

print(f"{'用例':<34} {'检出类型':<18} {'位置':<6} 说明")
print("-" * 96)
for name, recs, head, expect in cases:
    res = verify_records(recs, expected_head=head)
    err = res.error or "ok"
    pos = "-" if res.position is None else str(res.position)
    print(f"{name:<34} {err:<18} {pos:<6} 期望: {expect}")
    print(f"  └─ {res}")
