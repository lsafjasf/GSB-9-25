"""logtpl 自测：python3 -m unittest test_logtpl -v"""
import random
import sys
import unittest

sys.path.insert(0, ".")
from logtpl import (  # noqa: E402
    canonicalize_kv,
    cluster_lines,
    extract_template,
    render_report,
)

CORPUS = [
    '2026-09-25T10:00:01Z INFO http 10.0.0.1:8080 "GET /api/users/123" 200 12ms',
    '2026-09-25T10:00:02Z INFO http 10.0.0.2:8080 "GET /api/users/456" 200 9ms',
    '2026-09-25T10:00:03Z WARN http 172.16.0.9:8080 "POST /api/orders" 502 1300ms',
    '2026-09-25 10:00:04 ERROR db query failed after 3.5s host=db-01 retries=3',
    '2026-09-25 10:00:05 ERROR db query failed after 0.8s host=db-02 retries=5',
    'job 6f1c2a4e-9b3d-4e5f-8a7b-1c2d3e4f5a6b finished in 250ms exit=0',
    'job 11111111-2222-3333-4444-555555555555 finished in 1.2s exit=1',
    'alloc at 0x7f9c4a20 size=4096',
    'alloc at 0x1b2c3d4e size=8192',
    'deploy version v1.2.3 to /opt/app/releases/v1.2.3',
    'deploy version 2.10.0 to /opt/app/releases/2.10.0',
    'user=alice action=login ip=10.1.1.1 status=ok',
    'action=login ip=10.1.1.2 status=ok user=bob',  # 字段换序
    '404 500',          # 无字面量锚点 -> unclustered
    '12345',            # 纯数字 -> unclustered
    'healthcheck ok',   # 无变量
]


class TestExtract(unittest.TestCase):
    def test_single_line(self):
        t = extract_template('GET /api/users/42 200 12ms from 10.0.0.1')
        self.assertEqual(t.text, 'GET <PATH> <NUM> <DURATION> from <IP>')
        self.assertEqual(t.placeholders, ('PATH', 'NUM', 'DURATION', 'IP'))

    def test_variable_classification(self):
        cases = {
            '2026-09-25T10:00:01Z': 'DATETIME',
            '2026-09-25 10:00:04': 'DATETIME',
            '10.0.0.1': 'IP',
            '10.0.0.1:8080': 'IP',
            'https://example.com/a/b?q=1': 'URL',
            '6f1c2a4e-9b3d-4e5f-8a7b-1c2d3e4f5a6b': 'UUID',
            '250ms': 'DURATION',
            '3.5s': 'DURATION',
            '/opt/app/releases': 'PATH',
            'v1.2.3': 'VER',
            '0x7f9c4a20': 'HEX',
            'deadbeefcafe1234': 'HEX',
            'u311120': 'ID',
            'a1b2c3': 'ID',
            '42': 'NUM',
        }
        for text, name in cases.items():
            t = extract_template('x %s y' % text)
            self.assertEqual(t.text, 'x <%s> y' % name, text)

    def test_roundtrip_all_lines(self):
        for line in CORPUS:
            t = extract_template(line)
            self.assertEqual(t.render(t.extract_values(line)), line, line)

    def test_roundtrip_repeated_same_value(self):
        line = 'a 1 b 1 c 1'
        t = extract_template(line)
        self.assertEqual(t.render(t.extract_values(line)), line)

    def test_nested_quotes_and_structured(self):
        line = 'parse error at "{"a": "b", "n": 7}" in /etc/app/conf.json code=500'
        t = extract_template(line)
        self.assertEqual(t.render(t.extract_values(line)), line)
        self.assertIn('<PATH>', t.text)
        self.assertIn('<NUM>', t.text)

    def test_very_long_line(self):
        line = 'start ' + 'x' * 200_000 + ' id=12345 end'
        t = extract_template(line)
        self.assertEqual(t.render(t.extract_values(line)), line)
        self.assertEqual(t.placeholders, ('NUM',))

    def test_no_variable_line_is_own_template(self):
        t = extract_template('healthcheck ok')
        self.assertEqual(t.text, 'healthcheck ok')
        self.assertEqual(t.placeholders, ())


class TestCluster(unittest.TestCase):
    def test_all_different_lines(self):
        lines = ['event alpha 1', 'event beta 2', 'event gamma 3']
        r = cluster_lines(lines)
        self.assertEqual(len(r.clusters), 3)
        self.assertTrue(all(c.count == 1 for c in r.clusters))

    def test_coverage_and_unclustered(self):
        r = cluster_lines(CORPUS)
        self.assertEqual(r.total, len(CORPUS))
        unclustered_lines = {line for line, _ in r.unclustered}
        self.assertEqual(unclustered_lines, {'404 500', '12345'})
        self.assertEqual(r.clustered_lines, len(CORPUS) - 2)
        self.assertAlmostEqual(r.coverage, (len(CORPUS) - 2) / len(CORPUS))
        self.assertGreater(r.avg_placeholders, 0)

    def test_order_independence(self):
        base = cluster_lines(CORPUS)
        for seed in range(20):
            shuffled = CORPUS[:]
            random.Random(seed).shuffle(shuffled)
            r = cluster_lines(shuffled)
            self.assertEqual(r.template_set(), base.template_set())
            self.assertEqual(r.unclustered, base.unclustered)
            self.assertEqual(r.coverage, base.coverage)
            self.assertEqual(r.avg_placeholders, base.avg_placeholders)

    def test_field_reorder_default_separate(self):
        # 判定依据：模板是位置敏感的，字面量骨架不同 -> 不同模板
        r = cluster_lines([
            'user=alice action=login status=ok',
            'action=login status=ok user=bob',
        ])
        self.assertEqual(len(r.clusters), 2)

    def test_field_reorder_sort_kv_merges(self):
        r = cluster_lines([
            'user=alice action=login status=ok',
            'action=login status=ok user=bob',
        ], sort_kv=True)
        self.assertEqual(len(r.clusters), 1)
        self.assertEqual(r.clusters[0].count, 2)
        self.assertEqual(
            r.clusters[0].template,
            'action=<VAL> status=<VAL> user=<VAL>',
        )

    def test_canonicalize_kv(self):
        self.assertEqual(
            canonicalize_kv('b=2 a=1 c=3'),
            'a=1 b=2 c=3',
        )
        # 非 k=v 词元打断分组
        self.assertEqual(
            canonicalize_kv('x b=2 a=1 y'),
            'x a=1 b=2 y',
        )

    def test_report_renders(self):
        r = cluster_lines(CORPUS)
        report = render_report(r)
        self.assertIn('coverage', report)
        self.assertIn('unclustered', report)


if __name__ == '__main__':
    unittest.main()
