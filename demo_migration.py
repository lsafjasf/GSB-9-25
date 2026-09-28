"""demo_migration.py — 规则集跨环境搬运 + 版本对比 + 阶梯灰度端到端演示。

直接运行：python3 demo_migration.py
仅使用标准库；不依赖外部时间（now 全部显式给定，输出可复现）。
"""

import tempfile
import os
from datetime import datetime, timezone

from feature_flags import (
    FlagStore, diff_configs, export_bundle, export_json, import_bundle,
)

STAGING_V1 = {
    "salt": "checkout-2026",
    "flags": {
        "new_checkout": {
            "default": False,
            "env": {"staging": True},
            "rules": [
                {"id": "gray", "value": True, "percentage": 10,
                 "window": {"start": "2026-09-01T00:00:00Z",
                            "end": "2026-12-31T23:59:59Z"}},
            ],
        },
        "legacy_pay": {"default": True},
    },
}

STAGING_V2 = {
    "salt": "checkout-2026",
    "flags": {
        "new_checkout": {
            "default": False,
            "env": {"staging": True},
            "rules": [
                {"id": "vip-early", "value": True, "percentage": 5},
                {"id": "gray", "value": True,
                 "rollout": [
                     {"time": "2026-10-01T00:00:00Z", "percentage": 0},
                     {"time": "2026-10-02T00:00:00Z", "percentage": 10},
                     {"time": "2026-10-04T00:00:00Z", "percentage": 50},
                     {"time": "2026-10-08T00:00:00Z", "percentage": 100},
                 ]},
            ],
        },
        "dark_mode": {"default": False, "env": {"staging": True}},
    },
}


def main() -> None:
    print("=" * 64)
    print("步骤 1：staging 导出 v2 规则集包（落盘模拟跨环境文件/网络搬运）")
    print("=" * 64)
    bundle = export_bundle(STAGING_V2, version="v2",
                           source_env="staging")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(export_json(STAGING_V2, version="v2", source_env="staging"))
        path = f.name
    print(f"已写出规则集包: {path}  ({os.path.getsize(path)} bytes)")
    print(f"format={bundle['format']}  meta={bundle['meta']}")

    print("\n" + "=" * 64)
    print("步骤 2：prod 侧导入并上线（导入即编译校验，非法包会被拒收）")
    print("=" * 64)
    with open(path, "rb") as f:
        config_v2, meta = import_bundle(f.read())
    store = FlagStore(STAGING_V1, env="prod")   # prod 原本跑 v1
    store.update(config_v2)                      # 原子替换为导入的 v2
    print(f"导入成功：version={meta.get('version')} "
          f"source_env={meta.get('source_env')}，已 update 上线")

    print("\n" + "=" * 64)
    print("步骤 3：对比 prod 旧版本(v1) 与导入版本(v2)")
    print("=" * 64)
    diff = diff_configs(STAGING_V1, STAGING_V2)
    print(diff.report())
    print(f"\n断言汇总: 新增 {len(diff.added)}，删除 {len(diff.removed)}，"
          f"参数变化 {len(diff.changed)}，empty={diff.empty}")

    print("\n" + "=" * 64)
    print("步骤 4：阶梯灰度随时间自动放量（同一组分桶用户）")
    print("=" * 64)
    users = [f"u-{i}" for i in range(10_000)]
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    checkpoints = [(0, "10-01 首档"), (1, "10-02"), (3, "10-04"),
                   (7, "10-08 全量")]
    prev = set()
    for day, label in checkpoints:
        snap = store.snapshot(now=t0 + day * 86400)
        enabled = {u for u in users if snap.is_enabled("new_checkout", u)}
        plan = snap.rollout_plan("new_checkout", "gray")
        added_now = len(enabled - prev)
        print(f"{label}: 第{plan.active_index}档 {plan.active_percentage:>3.0f}% "
              f"实际 {len(enabled) / len(users) * 100:6.2f}%  "
              f"本档新增 {added_now:>5} 人  已放量集合单调: {prev <= enabled}")
        prev = enabled

    print("\n演示完成：同 (salt,flag,user) 分桶跨时间/跨版本不变，"
          "放量集合只扩不缩。")
    os.unlink(path)


if __name__ == "__main__":
    main()
