"""端到端演示：旧快照升级、损坏 vs 版本未知、失败不覆盖现场。

运行：python3 demo.py
"""
import os
import tempfile

from snapshot_lib import (
    CorruptedSnapshotError,
    UnknownVersionError,
    encode_snapshot,
    load_state,
    save_state,
)


def main():
    work = tempfile.mkdtemp(prefix="sessnap-demo-")
    path = os.path.join(work, "session.snap")

    # 1) 一份“老版本 v1”快照（手工落盘，模拟历史数据）
    v1 = {"user_id": "u-42", "messages": [{"role": "user", "content": "你好"}]}
    with open(path, "wb") as fh:
        fh.write(encode_snapshot(v1, version=1))
    snap = load_state(path)
    print(f"[升级] v1 -> v{snap.version}: {snap.state} (upgraded_from={snap.upgraded_from})")

    # 再次升级/读取结果一致（幂等）
    again = load_state(path)
    print(f"[幂等] 重读结果一致: {again.state == snap.state}")

    # 升级后按当前版本重存
    save_state(path, snap.state, version=snap.version)

    # 2) 内容损坏 -> CorruptedSnapshotError，原文件保留
    good = open(path, "rb").read()
    bad = bytearray(good)
    bad[len(bad) - 5] ^= 0xFF
    with open(path, "wb") as fh:
        fh.write(bytes(bad))
    try:
        load_state(path)
    except CorruptedSnapshotError as exc:
        print(f"[损坏] {exc}")
    print(f"[保留现场] 损坏文件未被动过: {open(path, 'rb').read() == bytes(bad)}")

    # 3) 版本未知（内容完整）-> UnknownVersionError
    with open(path, "wb") as fh:
        fh.write(encode_snapshot({"future": True}, version=99))
    try:
        load_state(path)
    except UnknownVersionError as exc:
        print(f"[版本未知] {exc}")
    print(f"[保留现场] 未知版本文件完好: {open(path, 'rb').read() == encode_snapshot({'future': True}, 99)}")


if __name__ == "__main__":
    main()
