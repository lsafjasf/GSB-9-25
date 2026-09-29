"""格式版本迁移工具：把旧版本记录批量升级到当前结构。

用法::

    python3 -m src.migrate <数据目录> [选项]

数据目录下每个 ``*.bin`` 文件视为一条线上记录（线上字节格式见
``src/codec.py`` 文件头）。工具流程：

1. **读入一致性检查**：先对全部记录做严格结构校验（magic、版本号、
   field_count 与实际字段项吻合、长度不越界、无尾部脏字节、可正常
   解码）。校验失败的记录记入失败清单并附原因，不进入迁移阶段；
   ``--check-only`` 时到此为止。
2. **批量迁移**：版本号低于当前版本的记录，用当前解码器读入（缺失
   字段按 ``FIELDS`` 默认值补齐、未知字段原样保留），再用当前编码器
   重新编码为当前版本，临时文件 + 原子替换写回，可安全中断。
3. **对拍**：每条记录写回前，分别用新代码解码迁移前/后的字节，
   业务字段与未知字段必须完全一致，否则记失败且原文件不动。
4. **报告**：输出迁移条数、跳过条数（已是当前版本）、失败条数及
   每条失败原因，并写入 ``<数据目录>/migrate_report.json``。

可中断可重入：已迁移的记录版本号已是当前版本，重跑时自动跳过；
``--limit N`` 可分批迁移；失败记录修复数据后可用 ``--only 文件名``
单独重试。

退出码：0 = 无失败；1 = 存在失败记录。
"""
import argparse
import json
import os
import struct
import sys
from typing import Any, Dict, List, Tuple

from src import codec
from src.codec import FIELDS, UNKNOWN_KEY

#: 工具能读懂（因此能迁移）的历史版本号；当前版本由 codec.VERSION 决定
SUPPORTED_VERSIONS = (0, 1)

REPORT_NAME = "migrate_report.json"


class CheckError(ValueError):
    """读入一致性检查失败，消息即失败原因。"""


def check_record(data: bytes) -> int:
    """严格结构校验一条记录，返回其版本号；不合法则抛 CheckError。"""
    if len(data) < 5:
        raise CheckError("记录长度不足 5 字节头部")
    if data[:2] != codec.MAGIC:
        raise CheckError("bad magic: 期望 b'RC'，实际 %r" % data[:2])
    version = data[2]
    if version not in SUPPORTED_VERSIONS:
        raise CheckError("不支持的版本号 %d" % version)
    (count,) = struct.unpack(">H", data[3:5])
    pos = 5
    for index in range(count):
        if pos + 4 > len(data):
            raise CheckError("第 %d 个字段头被截断" % index)
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        if pos + 4 + length > len(data):
            raise CheckError("第 %d 个字段 payload 被截断" % index)
        pos += 4 + length
    if pos != len(data):
        raise CheckError("字段区之后多出 %d 字节脏数据" % (len(data) - pos))
    try:
        codec.decode_record(data)
    except Exception as exc:  # 结构合法但解码失败（如非法 utf-8）
        raise CheckError("解码失败: %s" % exc)
    return version


def migrate_bytes(old: bytes) -> bytes:
    """把旧版本记录字节升级为当前版本字节（缺失字段补默认值显式写出）。"""
    return codec.encode_record(codec.decode_record(old))


def _business_view(record: Dict[str, Any]) -> Tuple[Dict[str, Any], list]:
    """对拍视角：业务字段值 + 保留的未知字段（不含 _missing 簿记）。"""
    values = {field.name: record[field.name] for field in FIELDS}
    return values, sorted(record.get(UNKNOWN_KEY, []))


def verify_pair(old: bytes, new: bytes) -> None:
    """对拍：新代码读入迁移前/后字节的结果必须一致，否则抛 CheckError。"""
    old_view = _business_view(codec.decode_record(old))
    new_view = _business_view(codec.decode_record(new))
    if old_view != new_view:
        raise CheckError("对拍不一致: 迁移前 %r != 迁移后 %r" % (old_view, new_view))


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _load_state(data_dir: str) -> Dict[str, Any]:
    path = os.path.join(data_dir, REPORT_NAME)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {}


def run(data_dir: str, check_only: bool = False, limit: int = 0,
        only: List[str] = None) -> Dict[str, Any]:
    """执行迁移，返回报告 dict（同时打印并落盘 migrate_report.json）。"""
    names = sorted(n for n in os.listdir(data_dir) if n.endswith(".bin"))
    if only:
        wanted = set(only)
        names = [n for n in names if n in wanted]

    migrated: List[str] = []
    skipped: List[str] = []
    failed: Dict[str, str] = {}
    pending: List[str] = []  # check-only 模式下待迁移的记录

    # ---- 阶段 1：读入一致性检查（先于任何写操作） ----
    stage1_pass: List[Tuple[str, bytes, int]] = []
    for name in names:
        path = os.path.join(data_dir, name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            version = check_record(data)
        except (OSError, CheckError) as exc:
            failed[name] = "读入一致性检查失败: %s" % exc
            continue
        stage1_pass.append((name, data, version))

    # ---- 阶段 2：逐条迁移 + 对拍 + 原子写回 ----
    if not check_only:
        for name, data, version in stage1_pass:
            if limit and len(migrated) >= limit:
                break  # 模拟/支持中断：剩余记录下次重跑继续
            if version == codec.VERSION:
                skipped.append(name)
                continue
            try:
                new_data = migrate_bytes(data)
                verify_pair(data, new_data)
                _atomic_write(os.path.join(data_dir, name), new_data)
            except (OSError, CheckError, ValueError) as exc:
                failed[name] = "迁移失败: %s" % exc
                continue
            migrated.append(name)
    else:
        for name, _data, version in stage1_pass:
            (skipped if version == codec.VERSION else pending).append(name)

    report = {
        "data_dir": os.path.abspath(data_dir),
        "check_only": check_only,
        "current_version": codec.VERSION,
        "migrated_count": len(pending if check_only else migrated),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "migrated": pending if check_only else migrated,
        "skipped": skipped,
        "failed": failed,
    }
    previous = _load_state(data_dir)
    if previous.get("failed"):
        # 保留本轮未覆盖到的历史失败原因，便于修复后用 --only 单独重试
        for name, reason in previous["failed"].items():
            if name not in names and name not in failed:
                failed.setdefault(name, reason + "（未重试）")
        report["failed"] = failed
        report["failed_count"] = len(failed)

    with open(os.path.join(data_dir, REPORT_NAME), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return report


def _print_report(report: Dict[str, Any]) -> None:
    print("数据目录: %s" % report["data_dir"])
    print("当前版本: v%d%s" % (report["current_version"],
                               "（仅检查，未写入）" if report["check_only"] else ""))
    label = "待迁移条数" if report["check_only"] else "迁移条数"
    print("%s: %d" % (label, report["migrated_count"]))
    print("跳过条数: %d（已是当前版本）" % report["skipped_count"])
    print("失败条数: %d" % report["failed_count"])
    for name, reason in sorted(report["failed"].items()):
        print("  失败 %s: %s" % (name, reason))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m src.migrate",
        description="把旧版本记录批量升级到当前格式版本（可中断可重入）")
    parser.add_argument("data_dir", help="存放 *.bin 记录的目录")
    parser.add_argument("--check-only", action="store_true",
                        help="只做读入一致性检查，不写入")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="本轮最多迁移 N 条（分批/中断后续跑）")
    parser.add_argument("--only", nargs="+", default=None, metavar="NAME",
                        help="只处理指定文件名的记录（失败记录单独重试）")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.data_dir):
        print("数据目录不存在: %s" % args.data_dir, file=sys.stderr)
        return 2
    report = run(args.data_dir, check_only=args.check_only,
                 limit=args.limit, only=args.only)
    _print_report(report)
    return 1 if report["failed_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
