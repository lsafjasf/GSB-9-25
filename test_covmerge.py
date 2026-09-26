#!/usr/bin/env python3
"""covmerge.py 的自测（仅标准库 unittest）。

运行: python3 test_covmerge.py -v
"""

import json
import os
import tempfile
import unittest

import covmerge


def make_report(tmpdir, name, records):
    """records: {path: {lineno: hits}}，写成 LCOV 文件并返回路径。"""
    lines = []
    for path, hits in records.items():
        lines.append(f"SF:{path}")
        for lineno, count in hits.items():
            lines.append(f"DA:{lineno},{count}")
        lines.append("end_of_record")
    full = os.path.join(tmpdir, name)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return full


def make_config(tmpdir, config):
    full = os.path.join(tmpdir, "thresholds.json")
    with open(full, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    return full


class ParseLcovTest(unittest.TestCase):
    def test_basic_parse(self):
        warnings = []
        files = covmerge.parse_lcov(
            "TN:\nSF:a.py\nDA:1,3\nDA:2,0\nend_of_record\n", "r1", warnings)
        self.assertEqual(files["a.py"].hits, {1: 3, 2: 0})
        self.assertEqual(warnings, [])

    def test_duplicate_da_records_are_summed(self):
        warnings = []
        files = covmerge.parse_lcov(
            "SF:a.py\nDA:1,1\nDA:1,2\nend_of_record\n", "r1", warnings)
        self.assertEqual(files["a.py"].hits, {1: 3})

    def test_malformed_lines_warn_and_skip(self):
        warnings = []
        files = covmerge.parse_lcov(
            "DA:5,1\nSF:a.py\nDA:abc,x\nDA:-1,2\nDA:3,1\nend_of_record\n",
            "r1", warnings)
        self.assertEqual(files["a.py"].hits, {3: 1})
        self.assertEqual(len(warnings), 3)

    def test_float_hit_counts_accepted(self):
        warnings = []
        files = covmerge.parse_lcov("SF:a.py\nDA:1,2.0\nend_of_record\n", "r1", warnings)
        self.assertEqual(files["a.py"].hits, {1: 2})


class MergeSemanticsTest(unittest.TestCase):
    """合并语义验证：相加（而非取最大值）、文件并集、行并集。"""

    def test_same_line_hits_are_summed_not_maxed(self):
        warnings = []
        r1 = covmerge.parse_lcov("SF:a.py\nDA:1,2\nDA:2,0\nend_of_record\n", "r1", warnings)
        r2 = covmerge.parse_lcov("SF:a.py\nDA:1,3\nDA:2,1\nend_of_record\n", "r2", warnings)
        merged = covmerge.merge_reports([("r1", r1), ("r2", r2)], warnings)
        self.assertEqual(merged["a.py"].hits[1], 5)  # 2+3，不是 max(2,3)=3
        self.assertEqual(merged["a.py"].hits[2], 1)

    def test_file_set_is_union(self):
        warnings = []
        r1 = covmerge.parse_lcov("SF:a.py\nDA:1,1\nend_of_record\n", "r1", warnings)
        r2 = covmerge.parse_lcov("SF:b.py\nDA:1,1\nend_of_record\n", "r2", warnings)
        merged = covmerge.merge_reports([("r1", r1), ("r2", r2)], warnings)
        self.assertEqual(set(merged), {"a.py", "b.py"})

    def test_inconsistent_line_sets_union_with_warning(self):
        warnings = []
        r1 = covmerge.parse_lcov("SF:a.py\nDA:1,1\nDA:2,1\nend_of_record\n", "r1", warnings)
        r2 = covmerge.parse_lcov("SF:a.py\nDA:2,1\nDA:3,0\nend_of_record\n", "r2", warnings)
        merged = covmerge.merge_reports([("r1", r1), ("r2", r2)], warnings)
        self.assertEqual(set(merged["a.py"].hits), {1, 2, 3})
        self.assertEqual(merged["a.py"].hits[1], 1)  # 只在 r1 出现，r2 视为 0 次
        self.assertTrue(any("行集合不一致" in w for w in warnings))

    def test_file_in_only_some_reports_no_warning(self):
        warnings = []
        r1 = covmerge.parse_lcov("SF:a.py\nDA:1,1\nend_of_record\nSF:b.py\nDA:1,1\nend_of_record\n", "r1", warnings)
        r2 = covmerge.parse_lcov("SF:a.py\nDA:1,0\nend_of_record\n", "r2", warnings)
        merged = covmerge.merge_reports([("r1", r1), ("r2", r2)], warnings)
        self.assertEqual(set(merged), {"a.py", "b.py"})
        self.assertEqual(warnings, [])  # 文件只在部分报告出现是正常分片场景

    def test_merge_many_reports(self):
        warnings = []
        reports = [
            (f"r{i}", covmerge.parse_lcov(f"SF:a.py\nDA:1,1\nend_of_record\n", f"r{i}", []))
            for i in range(10)
        ]
        merged = covmerge.merge_reports(reports, warnings)
        self.assertEqual(merged["a.py"].hits[1], 10)


class ThresholdTest(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        self.merged = {
            "src/core/a.py": covmerge.FileCoverage("src/core/a.py", {1: 1, 2: 1, 3: 0, 4: 0}),
            "src/core/b.py": covmerge.FileCoverage("src/core/b.py", {1: 1, 2: 1}),
            "src/util/c.py": covmerge.FileCoverage("src/util/c.py", {1: 0, 2: 0}),
        }

    def test_layered_thresholds(self):
        config = {
            "overall": 50,
            "directories": {"src/core": 60, "src/util": 10},
            "files": {"src/core/a.py": 60},
        }
        results = covmerge.check_thresholds(self.merged, config, self.warnings)
        by_scope = {r.scope: r for r in results}
        # overall: 4/8 = 50% >= 50
        self.assertTrue(by_scope["overall"].passed)
        # src/core: 4/6 ≈ 66.7% >= 60
        self.assertTrue(by_scope["dir:src/core"].passed)
        # src/util: 0/2 = 0% < 10
        self.assertFalse(by_scope["dir:src/util"].passed)
        self.assertAlmostEqual(by_scope["dir:src/util"].gap, 10.0)
        # 文件级: 2/4 = 50% < 60，缺口 10 个百分点
        file_res = by_scope["file:src/core/a.py"]
        self.assertFalse(file_res.passed)
        self.assertAlmostEqual(file_res.percent, 50.0)
        self.assertAlmostEqual(file_res.gap, 10.0)

    def test_threshold_for_missing_file_counts_as_zero(self):
        config = {"files": {"src/ghost.py": 1}}
        results = covmerge.check_thresholds(self.merged, config, self.warnings)
        self.assertFalse(results[0].passed)
        self.assertEqual(results[0].percent, 0.0)
        self.assertTrue(any("未匹配" in w for w in self.warnings))

    def test_threshold_for_missing_dir_warns(self):
        config = {"directories": {"src/nowhere": 50}}
        results = covmerge.check_thresholds(self.merged, config, self.warnings)
        self.assertFalse(results[0].passed)
        self.assertTrue(any("未匹配到任何文件" in w for w in self.warnings))


class ConfigValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_reject_out_of_range_threshold(self):
        path = make_config(self.tmpdir, {"overall": 120})
        with self.assertRaises(ValueError):
            covmerge.load_config(path)

    def test_reject_non_numeric_threshold(self):
        path = make_config(self.tmpdir, {"overall": "high"})
        with self.assertRaises(ValueError):
            covmerge.load_config(path)

    def test_reject_empty_config(self):
        path = make_config(self.tmpdir, {})
        with self.assertRaises(ValueError):
            covmerge.load_config(path)

    def test_reject_malformed_json(self):
        path = os.path.join(self.tmpdir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            covmerge.load_config(path)


class ExitCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_exit_ok(self):
        report = make_report(self.tmpdir, "r1.info", {"a.py": {1: 1, 2: 1}})
        config = make_config(self.tmpdir, {"overall": 100})
        self.assertEqual(covmerge.run([report], config), covmerge.EXIT_OK)

    def test_exit_threshold_failed(self):
        report = make_report(self.tmpdir, "r1.info", {"a.py": {1: 1, 2: 0}})
        config = make_config(self.tmpdir, {"overall": 80})
        self.assertEqual(covmerge.run([report], config), covmerge.EXIT_THRESHOLD_FAILED)

    def test_exit_invalid_missing_report(self):
        config = make_config(self.tmpdir, {"overall": 80})
        code = covmerge.run([os.path.join(self.tmpdir, "nope.info")], config)
        self.assertEqual(code, covmerge.EXIT_INVALID_INPUT)

    def test_exit_invalid_all_reports_empty(self):
        empty = os.path.join(self.tmpdir, "empty.info")
        open(empty, "w").close()
        config = make_config(self.tmpdir, {"overall": 80})
        self.assertEqual(covmerge.run([empty], config), covmerge.EXIT_INVALID_INPUT)

    def test_exit_invalid_bad_config(self):
        report = make_report(self.tmpdir, "r1.info", {"a.py": {1: 1}})
        bad = os.path.join(self.tmpdir, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("[]")
        self.assertEqual(covmerge.run([report], bad), covmerge.EXIT_INVALID_INPUT)

    def test_empty_report_among_valid_ones_is_skipped_with_warning(self):
        good = make_report(self.tmpdir, "good.info", {"a.py": {1: 1}})
        empty = os.path.join(self.tmpdir, "empty.info")
        open(empty, "w").close()
        config = make_config(self.tmpdir, {"overall": 100})
        self.assertEqual(covmerge.run([good, empty], config), covmerge.EXIT_OK)


class SampleReportsTest(unittest.TestCase):
    """用仓库内 samples/ 的样例报告做端到端验证（合并语义文档用例）。"""

    SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

    def test_merged_values(self):
        warnings = []
        reports = []
        for name in ("shard1.info", "shard2.info", "shard3.info"):
            path = os.path.join(self.SAMPLES, name)
            with open(path, encoding="utf-8") as fh:
                reports.append((name, covmerge.parse_lcov(fh.read(), name, warnings)))
        merged = covmerge.merge_reports(reports, warnings)
        self.assertEqual(set(merged), {
            "src/core/engine.py", "src/core/parser.py",
            "src/utils/misc.py", "src/web/handler.py",
        })
        # engine.py 第 3 行: 2+0+1=3（相加语义），第 5 行仅 shard2 有
        self.assertEqual(merged["src/core/engine.py"].hits[3], 3)
        self.assertEqual(merged["src/core/engine.py"].hits[5], 1)
        self.assertEqual(merged["src/core/engine.py"].percent, 100.0)
        self.assertAlmostEqual(merged["src/utils/misc.py"].percent, 200 / 3)
        self.assertTrue(any("行集合不一致" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
