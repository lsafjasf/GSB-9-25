#!/usr/bin/env python3
"""covmerge.py — 合并多份 LCOV 覆盖率报告并做分层阈值判定。

合并语义（详见 README.md）：
  - 文件集合取并集：只出现在部分报告中的文件照常参与合并。
  - 同一文件同一行（DA 记录）的命中计数相加（shards 是不相交的执行切片）。
  - 同一文件在不同报告中行集合不一致时取并集，缺失的行按 0 次命中处理，
    并输出 WARNING 提示（通常意味着各分片基于不同版本的源码运行）。
  - 空报告（无任何 SF 记录）跳过并提示；所有报告均无有效数据时视为输入非法。

退出码：
  0 — 全部阈值达标
  1 — 存在未达标项
  2 — 输入非法（报告不可读/无有效数据/配置非法/参数错误）

仅使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

EXIT_OK = 0
EXIT_THRESHOLD_FAILED = 1
EXIT_INVALID_INPUT = 2


# ---------------------------------------------------------------- 数据模型

@dataclass
class FileCoverage:
    """单个文件的行覆盖数据：行号 -> 命中次数。"""

    path: str
    hits: dict = field(default_factory=dict)  # {lineno: hit_count}

    @property
    def total_lines(self) -> int:
        return len(self.hits)

    @property
    def covered_lines(self) -> int:
        return sum(1 for h in self.hits.values() if h > 0)

    @property
    def percent(self) -> float:
        if not self.hits:
            return 0.0
        return 100.0 * self.covered_lines / self.total_lines


@dataclass
class ScopeResult:
    """一个阈值作用域（overall / 目录 / 文件）的判定结果。"""

    scope: str            # "overall" / "dir:<path>" / "file:<path>"
    threshold: float
    covered: int
    total: int
    files: list = field(default_factory=list)  # 作用域内的 FileCoverage

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return 100.0 * self.covered / self.total

    @property
    def gap(self) -> float:
        """距阈值的百分点缺口（达标时为 0）。"""
        return max(0.0, self.threshold - self.percent)

    @property
    def passed(self) -> bool:
        return self.percent >= self.threshold


# ---------------------------------------------------------------- LCOV 解析

def parse_lcov(text: str, source: str, warnings: list) -> dict:
    """解析一份 LCOV 文本，返回 {path: FileCoverage}。

    只关心 SF:/DA:/end_of_record 记录，其余记录（TN/FN/FNDA/BRDA/...）忽略。
    同一份报告内重复的 DA 记录命中数相加（与跨报告合并语义一致）。
    无法解析的行记录 warning 后跳过，不中断解析。
    """
    files = {}
    current = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("SF:"):
            path = line[3:].strip()
            if not path:
                warnings.append(f"{source}:{lineno}: 空 SF 路径，已忽略")
                current = None
                continue
            current = files.setdefault(path, FileCoverage(path))
        elif line.startswith("DA:"):
            if current is None:
                warnings.append(f"{source}:{lineno}: DA 记录出现在任何 SF 之前，已忽略")
                continue
            parts = line[3:].split(",")
            try:
                line_no = int(parts[0])
                hit_count = int(float(parts[1]))  # 兼容 "3" 与 "3.0"
            except (ValueError, IndexError):
                warnings.append(f"{source}:{lineno}: 无法解析的 DA 记录 {line!r}，已忽略")
                continue
            if line_no <= 0 or hit_count < 0:
                warnings.append(f"{source}:{lineno}: 非法 DA 记录 {line!r}（行号<=0 或命中数<0），已忽略")
                continue
            current.hits[line_no] = current.hits.get(line_no, 0) + hit_count
        elif line == "end_of_record":
            current = None
        # 其余记录类型（TN/FN/FNDA/FNH/LH/BRDA/...）对本工具无意义，忽略
    return files


# ---------------------------------------------------------------- 合并

def merge_reports(reports: list, warnings: list) -> dict:
    """合并多份报告：reports 为 [(source_name, {path: FileCoverage}), ...]。

    - 文件集合取并集；
    - 同一文件同一行命中数相加；
    - 同一文件在不同报告中行集合不一致时取并集并输出 warning。
    """
    merged = {}
    line_sets = {}  # path -> set-of-sets 的来源统计 {path: {source: set(linenos)}}

    for source, files in reports:
        for path, fcov in files.items():
            target = merged.setdefault(path, FileCoverage(path))
            for line_no, hits in fcov.hits.items():
                target.hits[line_no] = target.hits.get(line_no, 0) + hits
            line_sets.setdefault(path, {})[source] = set(fcov.hits)

    for path, per_source in sorted(line_sets.items()):
        distinct = {frozenset(s) for s in per_source.values()}
        if len(distinct) > 1:
            union = set().union(*distinct)
            detail = ", ".join(
                f"{src}={len(s)}行" for src, s in sorted(per_source.items())
            )
            warnings.append(
                f"文件 {path} 在不同报告中的行集合不一致（{detail}）；"
                f"已取并集共 {len(union)} 行，缺失行按 0 次命中处理。"
                f"请确认各分片基于同一源码版本运行。"
            )
    return merged


# ---------------------------------------------------------------- 阈值判定

def normalize_dir(prefix: str) -> str:
    return prefix.strip("/") + "/"


def check_thresholds(merged: dict, config: dict, warnings: list) -> list:
    """按配置做整体/目录/文件三层阈值判定，返回 [ScopeResult, ...]。"""
    results = []
    all_files = sorted(merged.values(), key=lambda f: f.path)

    overall_threshold = config.get("overall")
    if overall_threshold is not None:
        results.append(ScopeResult(
            scope="overall",
            threshold=float(overall_threshold),
            covered=sum(f.covered_lines for f in all_files),
            total=sum(f.total_lines for f in all_files),
            files=all_files,
        ))

    for directory, threshold in sorted(config.get("directories", {}).items()):
        prefix = normalize_dir(directory)
        scoped = [f for f in all_files if f.path.startswith(prefix)]
        if not scoped:
            warnings.append(f"目录阈值 {directory!r} 未匹配到任何文件，请检查配置")
        results.append(ScopeResult(
            scope=f"dir:{directory}",
            threshold=float(threshold),
            covered=sum(f.covered_lines for f in scoped),
            total=sum(f.total_lines for f in scoped),
            files=scoped,
        ))

    for path, threshold in sorted(config.get("files", {}).items()):
        fcov = merged.get(path)
        if fcov is None:
            warnings.append(f"文件阈值 {path!r} 未匹配到任何覆盖率数据，按 0% 处理")
            results.append(ScopeResult(
                scope=f"file:{path}", threshold=float(threshold),
                covered=0, total=0, files=[],
            ))
        else:
            results.append(ScopeResult(
                scope=f"file:{path}", threshold=float(threshold),
                covered=fcov.covered_lines, total=fcov.total_lines, files=[fcov],
            ))
    return results


# ---------------------------------------------------------------- 配置加载

def load_config(path: str):
    """加载阈值配置 JSON；非法时抛出 ValueError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取阈值配置 {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"阈值配置 {path} 必须是 JSON 对象")

    def _check_threshold(name, value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"阈值 {name} 必须是数字，得到 {value!r}")
        if not 0 <= value <= 100:
            raise ValueError(f"阈值 {name} 必须在 [0, 100] 区间，得到 {value!r}")

    if "overall" in config:
        _check_threshold("overall", config["overall"])
    for section in ("directories", "files"):
        table = config.get(section, {})
        if not isinstance(table, dict):
            raise ValueError(f"配置项 {section!r} 必须是对象")
        for key, value in table.items():
            _check_threshold(f"{section}.{key}", value)
    if "overall" not in config and not config.get("directories") and not config.get("files"):
        raise ValueError(f"阈值配置 {path} 未定义任何阈值（overall/directories/files）")
    return config


# ---------------------------------------------------------------- 输出

def format_report(results: list, warnings: list, merged: dict) -> str:
    lines = []
    lines.append("=" * 64)
    lines.append("覆盖率合并报告")
    lines.append("=" * 64)
    lines.append(f"文件总数: {len(merged)}")
    lines.append("")
    lines.append(f"{'SCOPE':<28} {'CURRENT':>8} {'THRESHOLD':>9} {'GAP':>8}  RESULT")
    lines.append("-" * 64)
    for res in results:
        status = "PASS" if res.passed else "FAIL"
        gap = "-" if res.passed else f"{res.gap:.2f}"
        lines.append(
            f"{res.scope:<28} {res.percent:>7.2f}% {res.threshold:>7.2f}% {gap:>8}  {status}"
        )

    failed = [r for r in results if not r.passed]
    if failed:
        lines.append("")
        lines.append("未达标明细（精确到文件）:")
        for res in failed:
            lines.append(
                f"\n[{res.scope}] 当前 {res.percent:.2f}% / 阈值 {res.threshold:.2f}% "
                f"/ 缺口 {res.gap:.2f} 个百分点"
            )
            if not res.files:
                lines.append("  (无覆盖率数据)")
                continue
            # 缺口贡献最大的文件排前面：未覆盖行数降序
            for fcov in sorted(res.files, key=lambda f: (f.total_lines - f.covered_lines), reverse=True):
                uncovered = fcov.total_lines - fcov.covered_lines
                lines.append(
                    f"  {fcov.path}: {fcov.percent:.2f}% "
                    f"({fcov.covered_lines}/{fcov.total_lines} 行, 未覆盖 {uncovered} 行)"
                )
    if warnings:
        lines.append("")
        lines.append("警告:")
        for warn in warnings:
            lines.append(f"  WARNING: {warn}")
    return "\n".join(lines)


def results_to_json(results: list, warnings: list, merged: dict) -> str:
    payload = {
        "file_count": len(merged),
        "passed": all(r.passed for r in results),
        "scopes": [
            {
                "scope": r.scope,
                "percent": round(r.percent, 4),
                "threshold": r.threshold,
                "gap": round(r.gap, 4),
                "covered_lines": r.covered,
                "total_lines": r.total,
                "passed": r.passed,
                "files": [
                    {
                        "path": f.path,
                        "percent": round(f.percent, 4),
                        "covered_lines": f.covered_lines,
                        "total_lines": f.total_lines,
                    }
                    for f in r.files
                ] if not r.passed else None,
            }
            for r in results
        ],
        "warnings": warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 主流程

def run(report_paths: list, config_path: str, output_format: str = "text",
        timing: bool = False) -> int:
    started = time.perf_counter()
    warnings = []

    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    reports = []
    for path in report_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"ERROR: 无法读取报告 {path}: {exc}", file=sys.stderr)
            return EXIT_INVALID_INPUT
        files = parse_lcov(text, path, warnings)
        if not files:
            warnings.append(f"报告 {path} 为空或不含任何有效覆盖率记录，已跳过")
        reports.append((path, files))

    if not any(files for _, files in reports):
        print("ERROR: 所有报告均无有效覆盖率数据，无法合并", file=sys.stderr)
        return EXIT_INVALID_INPUT

    merge_started = time.perf_counter()
    merged = merge_reports(reports, warnings)
    merge_elapsed = time.perf_counter() - merge_started

    results = check_thresholds(merged, config, warnings)

    if output_format == "json":
        print(results_to_json(results, warnings, merged))
    else:
        print(format_report(results, warnings, merged))

    if timing:
        total_lines = sum(f.total_lines for f in merged.values())
        print(
            f"\n[perf] 报告数={len(report_paths)} 合并后文件数={len(merged)} "
            f"总行数={total_lines} 合并耗时={merge_elapsed * 1000:.1f}ms "
            f"总耗时={(time.perf_counter() - started) * 1000:.1f}ms",
            file=sys.stderr,
        )

    return EXIT_OK if all(r.passed for r in results) else EXIT_THRESHOLD_FAILED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="合并多份 LCOV 覆盖率报告并做分层阈值判定（退出码: 0=达标 1=未达标 2=输入非法）",
    )
    parser.add_argument("--reports", nargs="+", required=True, metavar="INFO",
                        help="一个或多个 LCOV 报告文件路径")
    parser.add_argument("--config", required=True, metavar="JSON",
                        help="阈值配置 JSON（overall/directories/files）")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="输出格式（默认 text）")
    parser.add_argument("--timing", action="store_true",
                        help="在 stderr 输出合并耗时等性能数据")
    args = parser.parse_args(argv)
    return run(args.reports, args.config, output_format=args.format, timing=args.timing)


if __name__ == "__main__":
    sys.exit(main())
