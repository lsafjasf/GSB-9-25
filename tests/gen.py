"""随机记录生成器（seeded，可复现）。字段集合从 codec.FIELDS 派生。"""
import random
import struct

from codec.fields import FIELDS, TYPE_NULL


def random_value(rng, type_id):
    from codec import fields as F
    if type_id == F.TYPE_INT:
        return rng.randint(0, 2**32 - 1)
    if type_id == F.TYPE_STR:
        return "".join(rng.choice("abc中文é \t") for _ in range(rng.randint(0, 12)))
    if type_id == F.TYPE_FLOAT:
        return rng.uniform(-1e6, 1e6)
    if type_id == F.TYPE_BOOL:
        return bool(rng.getrandbits(1))
    raise AssertionError


def random_record(rng, allow_missing=True, allow_null=True, allow_unknown=True):
    rec = {}
    for f in FIELDS:
        if allow_missing and rng.random() < 0.3:
            continue  # 缺失字段
        if allow_null and rng.random() < 0.2:
            rec[f.name] = None  # 空值
        else:
            rec[f.name] = random_value(rng, f.type_id)
    if allow_unknown and rng.random() < 0.4:
        unknown = []
        for _ in range(rng.randint(1, 3)):
            tag = rng.choice([100, 150, 200, 255])  # 避开已知 tag
            payload = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 6)))
            unknown.append((tag, rng.choice([1, 2, 7, TYPE_NULL]), payload))
        rec["_unknown"] = unknown
    return rec


def corpus(seed=20260925, n=300):
    rng = random.Random(seed)
    return [random_record(rng) for _ in range(n)]


def strip_unknown(rec):
    return {k: v for k, v in rec.items() if k != "_unknown"}


def known_fields(rec):
    return {k: v for k, v in rec.items() if k in {f.name for f in FIELDS}}
