"""logtpl 自测：round-trip、顺序无关性、边界场景、指标正确性。

运行: python3 -m unittest test_logtpl -v
"""
import random
import unittest

from logtpl import (Clusterer, cluster, parse_line, reconstruct, render,
                    count_placeholders, canonical_key)


def make_corpus(seed=0):
    """构造一个含多种模板 + 单例行的语料。"""
    rng = random.Random(seed)
    lines = []
    for i in range(50):
        lines.append('user=u%d logged in from 10.0.0.%d in %dms'
                     % (i, i % 255, i * 3))
        lines.append('GET /api/v1/items/%d 200 %dms' % (1000 + i, i))
        lines.append('worker=w%d job=job-%x done' % (i % 4, i * 7))
    lines.append('a=1 b=2 c=3')   # 与下一行互为重排变体
    lines.append('a=4 b=5 c=6')
    lines.append('c=7 b=8 a=9')
    lines.append('c=10 b=11 a=12')
    lines.append('this singleton line appears once')  # 单例 → unclustered
    rng.shuffle(lines)
    return lines


class TestRoundTrip(unittest.TestCase):
    """每条模板必须能用占位符+捕获值逐字符还原原始行。"""

    LINES = [
        'user=alice logged in from 10.0.0.1 in 12ms',
        'GET /api/v1/users/123 200 3.5s',
        'msg="he said \\"hi\\" ok" level=INFO',        # 嵌套/转义引号
        "err='it\'s bad' code=500",
        'q="outer \\"inner \\"deep\\" inner\\" outer"',
        '{"user":"alice","id":123,"ok":true}',            # 结构化字段
        'req id=550e8400-e29b-41d4-a716-446655440000 path=/var/log/a.log',
        'txn 8f3a2bc1 failed after 3 retries',
        'hash=deadbeefcafe status=ok',
        '10.0.0.1:8080 -> 192.168.1.1:443',
        'unterminated "quote stays literal-safe',
        "mixed 'single' and \"double\" quotes",
        'trailing, punct; everywhere! (yes) [no] {maybe}',
        '空 白 和 unicode 字符 π 数字 42',
        '',
        '   ',
        'plain words only',
        'key= key2=a',
        'a=' ,
        'x' * 100_000 + ' tail=42',                       # 超长行
    ]

    def test_roundtrip(self):
        for ln in self.LINES:
            parts, values = parse_line(ln)
            self.assertEqual(reconstruct(parts, values), ln,
                             'round-trip failed for %r' % ln)

    def test_corpus_roundtrip(self):
        for ln in make_corpus():
            parts, values = parse_line(ln)
            self.assertEqual(reconstruct(parts, values), ln)


class TestExtraction(unittest.TestCase):
    def test_placeholder_classification(self):
        parts, _ = parse_line(
            'user=u1 ip=10.0.0.1 took 12ms path=/a/b.log id=8f3a2bc1 n=42 s="hi there"')
        t = render(parts)
        self.assertIn('user=<STR>', t)
        self.assertIn('ip=<IP>', t)
        self.assertIn('<DUR>', t)
        self.assertIn('path=<PATH>', t)
        self.assertIn('<ID>', t)
        self.assertIn('n=<NUM>', t)
        self.assertIn('s=<STR>', t)

    def test_same_template_for_variable_lines(self):
        p1, _ = parse_line('user=u1 from 10.0.0.1 in 12ms')
        p2, _ = parse_line('user=u2 from 10.0.0.2 in 99ms')
        self.assertEqual(p1, p2)

    def test_long_line(self):
        ln = 'prefix ' + 'y' * 200_000 + ' n=123'
        parts, values = parse_line(ln)
        self.assertEqual(reconstruct(parts, values), ln)
        self.assertIn('n=<NUM>', render(parts))

    def test_nested_quotes_single_token(self):
        ln = 'msg="he said \\\"hi\\\" ok" level=INFO'
        parts, values = parse_line(ln)
        self.assertEqual(values[0], '"he said \\\"hi\\\" ok"')
        self.assertEqual(render(parts), 'msg=<STR> level=<STR>')


class TestOrderIndependence(unittest.TestCase):
    """打乱输入后：模板集合、计数、占位符归类、未聚类清单必须稳定。"""

    def signature(self, lines):
        rep = cluster(lines)
        return (
            [(t.template, t.count, t.placeholders) for t in rep.templates],
            list(rep.unclustered),
            [list(g) for g in rep.reorder_groups],
            rep.coverage,
            rep.avg_placeholders,
        )

    def test_shuffles_give_identical_results(self):
        base = make_corpus()
        ref = self.signature(base)
        for seed in range(10):
            shuffled = list(base)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(self.signature(shuffled), ref,
                             'seed %d changed the result' % seed)

    def test_reverse_order(self):
        base = make_corpus()
        self.assertEqual(self.signature(base[::-1]), self.signature(base))


class TestEdgeCases(unittest.TestCase):
    def test_empty_input(self):
        rep = cluster([])
        self.assertEqual(rep.total_lines, 0)
        self.assertEqual(rep.coverage, 0.0)
        self.assertEqual(rep.templates, [])
        self.assertEqual(rep.unclustered, [])

    def test_single_line(self):
        rep = cluster(['only one line here'])
        self.assertEqual(rep.total_lines, 1)
        self.assertEqual(rep.num_templates_total, 1)
        # min_support=2 时单例行不计入覆盖率
        self.assertEqual(rep.coverage, 0.0)
        self.assertEqual(rep.unclustered, ['only one line here'])
        # min_support=1 时全覆盖
        rep1 = cluster(['only one line here'], min_support=1)
        self.assertEqual(rep1.coverage, 1.0)
        self.assertEqual(rep1.unclustered, [])

    def test_all_different(self):
        lines = ['completely different line number %d xyz' % i for i in range(50)]
        # 注意：这些行只有数字不同 → 其实是同一模板
        rep = cluster(lines)
        self.assertEqual(len(rep.templates), 1)
        # 真正每行结构都不同的场景：
        lines2 = ['%d unique structure %s' % (i, 'x' * (i + 1)) for i in range(50)]
        rep2 = cluster(lines2)
        self.assertEqual(rep2.coverage, 0.0)
        self.assertEqual(len(rep2.unclustered), 50)

    def test_empty_and_blank_lines(self):
        rep = cluster(['', '', '   ', '   '], min_support=2)
        self.assertEqual(rep.coverage, 1.0)
        self.assertEqual(len(rep.templates), 2)


class TestMetrics(unittest.TestCase):
    def test_coverage_and_avg_placeholders(self):
        lines = (
            ['login user=u%d ok' % i for i in range(8)] +      # 模板A: 1 ph, 8行
            ['logout user=u%d at=%d' % (i, i) for i in range(2)] +  # 模板B: 2 ph, 2行
            ['orphan line one', 'orphan line two']                # 2 个单例
        )
        rep = cluster(lines)
        self.assertEqual(rep.total_lines, 12)
        self.assertEqual(rep.clustered_lines, 10)
        self.assertAlmostEqual(rep.coverage, 10 / 12)
        self.assertAlmostEqual(rep.avg_placeholders, (1 + 2) / 2)
        self.assertEqual(rep.unclustered, ['orphan line one', 'orphan line two'])

    def test_unclustered_lists_exact_lines(self):
        lines = ['a=1 b=2', 'a=3 b=4', 'lonely one', 'lonely two', 'lonely three']
        rep = cluster(lines)
        self.assertEqual(rep.unclustered, ['lonely one', 'lonely three', 'lonely two'])


class TestReorderedFields(unittest.TestCase):
    """字段顺序调整的行：模板位置敏感 → 分开；canonical_key 相同 → 显式归组。"""

    def test_reorder_detected(self):
        lines = ['a=1 b=2', 'a=3 b=4', 'b=5 a=6', 'b=7 a=8']
        rep = cluster(lines)
        templates = [t.template for t in rep.templates]
        # 判定依据：模板是位置敏感的，两者字面量顺序不同 → 不同模板
        self.assertEqual(len(templates), 2)
        self.assertIn('a=<NUM> b=<NUM>', templates)
        self.assertIn('b=<NUM> a=<NUM>', templates)
        # 但字段集合相同 → 归入同一 reorder group
        self.assertEqual(len(rep.reorder_groups), 1)
        self.assertEqual(sorted(rep.reorder_groups[0]), sorted(templates))

    def test_canonical_key(self):
        p1, _ = parse_line('x=1 y=2 done')
        p2, _ = parse_line('y=9 x=8 done')
        p3, _ = parse_line('x=1 y=2 done extra')
        self.assertEqual(canonical_key(p1), canonical_key(p2))
        self.assertNotEqual(canonical_key(p1), canonical_key(p3))

    def test_no_false_reorder_group(self):
        lines = ['a=1 b=2', 'a=3 b=4', 'a=5 c=6', 'a=7 c=8']
        rep = cluster(lines)
        self.assertEqual(rep.reorder_groups, [])


class TestStreaming(unittest.TestCase):
    def test_incremental_equals_batch(self):
        lines = make_corpus()
        c = Clusterer()
        for ln in lines:
            c.add(ln)
        sig1 = [(t.template, t.count) for t in c.report().templates]
        sig2 = [(t.template, t.count) for t in cluster(lines).templates]
        self.assertEqual(sig1, sig2)


if __name__ == '__main__':
    unittest.main()
