"""测试共用的随机记录生成器（固定种子，可复现）。"""
import random
import string

from src.codec import FIELDS, UNKNOWN_KEY

_EXTREME_INTS = [0, 1, -1, 2**31 - 1, -(2**31)]
_EXTREME_FLOATS = [0.0, -0.0, 1.5, -2.75, 1e300, -1e-300, float("inf"), float("-inf")]


def random_record(rng: random.Random, allow_missing=True, allow_unknown=True):
    """生成随机记录：含空值、极端值、随机缺失字段、随机未知字段。"""
    record = {}
    for field in FIELDS:
        if allow_missing and rng.random() < 0.25:
            continue  # 缺失字段：编码时应取默认值
        if field.name == "id":
            record["id"] = rng.choice(_EXTREME_INTS + [rng.randint(-(2**31), 2**31 - 1)])
        elif field.name in ("name", "email"):
            alphabet = string.ascii_letters + string.digits + "中文测试@._"
            record[field.name] = rng.choice(
                ["", "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))]
            )
        elif field.name == "active":
            record["active"] = rng.random() < 0.5
        elif field.name == "score":
            record["score"] = rng.choice(
                _EXTREME_FLOATS + [rng.uniform(-1e6, 1e6)]
            )
        elif field.name == "avatar":
            record["avatar"] = rng.choice(
                [b"", bytes(rng.randrange(256) for _ in range(rng.randint(0, 64)))]
            )
    if allow_unknown and rng.random() < 0.3:
        known_tags = {f.tag for f in FIELDS}
        unknown = []
        for _ in range(rng.randint(1, 3)):
            tag = rng.choice([t for t in range(7, 256) if t not in known_tags])
            type_id = rng.randint(0x01, 0x05)
            payload = bytes(rng.randrange(256) for _ in range(rng.randint(0, 16)))
            unknown.append((tag, type_id, payload))
        record[UNKNOWN_KEY] = unknown
    return record
