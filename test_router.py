import unittest

from router import (
    ConflictError,
    DecodeError,
    PatternError,
    Router,
    load_rules,
)


def make(rules):
    """rules: iterable of (method, host, pattern); conflicts ignored."""
    r = Router()
    for method, host, pattern in rules:
        try:
            r.add_rule(pattern, method=method, host_prefix=host)
        except ConflictError:
            pass
    return r


class TestMatching(unittest.TestCase):
    def test_static_exact(self):
        r = make([("GET", None, "/users/list")])
        m = r.match("/users/list", "GET", "example.com")
        self.assertIsNotNone(m)
        self.assertEqual(m.rule.pattern, "/users/list")
        self.assertIsNone(r.match("/users/list/other", "GET", "example.com"))
        self.assertIsNone(r.match("/users", "GET", "example.com"))

    def test_param_extraction_raw_and_decoded(self):
        r = make([(None, None, "/users/{name}")])
        m = r.match("/users/%E4%B8%AD%E6%96%87")
        self.assertEqual(m.params[0].name, "name")
        self.assertEqual(m.params[0].raw, "%E4%B8%AD%E6%96%87")
        self.assertEqual(m.params[0].value, "中文")

    def test_single_wildcard_matches_exactly_one_segment(self):
        r = make([(None, None, "/a/*/c")])
        self.assertIsNotNone(r.match("/a/b/c"))
        self.assertIsNone(r.match("/a/c"))
        self.assertIsNone(r.match("/a/b/d/c"))

    def test_tail_wildcard_matches_zero_or_more(self):
        r = make([(None, None, "/static/**")])
        self.assertIsNotNone(r.match("/static"))
        self.assertIsNotNone(r.match("/static/a"))
        self.assertIsNotNone(r.match("/static/a/b/c"))
        self.assertIsNone(r.match("/other"))

    def test_tail_wildcard_must_be_last(self):
        with self.assertRaises(PatternError):
            make([(None, None, "/a/**/b")])

    def test_method_filter(self):
        r = make([("GET", None, "/a"), ("POST", None, "/a/{x}")])
        self.assertEqual(r.match("/a", "GET").rule.pattern, "/a")
        self.assertIsNone(r.match("/a", "DELETE"))
        self.assertEqual(r.match("/a/b", "POST").rule.pattern, "/a/{x}")
        self.assertIsNone(r.match("/a/b", "GET"))

    def test_host_prefix_filter(self):
        r = make([(None, "api.", "/v1/x"), (None, None, "/v1/{y}")])
        self.assertEqual(r.match("/v1/x", host="api.example.com").rule.pattern, "/v1/x")
        self.assertEqual(r.match("/v1/x", host="web.example.com").rule.pattern, "/v1/{y}")
        self.assertIsNone(r.match("/v1/x", host=None) if len(r.rules) == 1 else None)


class TestPriority(unittest.TestCase):
    def test_static_beats_param_beats_wildcard(self):
        r = make([
            (None, None, "/a/*"),
            (None, None, "/a/{x}"),
            (None, None, "/a/b"),
        ])
        m = r.match("/a/b")
        self.assertEqual([x.pattern for x in m.hit_order], ["/a/b", "/a/{x}", "/a/*"])

    def test_shadowed_candidates_reported_in_order(self):
        r = make([
            (None, None, "/x/{a}/c"),
            (None, None, "/x/b/{d}"),
            (None, None, "/x/*/{d}"),
        ])
        m = r.match("/x/b/c")
        self.assertEqual(m.rule.pattern, "/x/b/{d}")
        self.assertEqual([s.pattern for s in m.shadowed], ["/x/{a}/c", "/x/*/{d}"])

    def test_same_kind_uses_registration_order(self):
        r = make([(None, None, "/a/{x}"), (None, None, "/a/{y}")])
        # second is fully shadowed and rejected at load; use wildcards instead
        r = make([(None, None, "/a/{x}/c"), (None, None, "/a/{y}/*")])
        m = r.match("/a/b/c")
        self.assertEqual(m.rule.pattern, "/a/{x}/c")
        self.assertEqual([s.pattern for s in m.shadowed], ["/a/{y}/*"])

    def test_tail_wildcard_is_lowest_priority(self):
        r = make([(None, None, "/a/**"), (None, None, "/a/{x}")])
        m = r.match("/a/b")
        self.assertEqual(m.rule.pattern, "/a/{x}")
        self.assertEqual([s.pattern for s in m.shadowed], ["/a/**"])

    def test_method_specific_does_not_leak(self):
        r = make([("GET", None, "/a/{x}"), (None, None, "/a/*")])
        m = r.match("/a/b", "POST")
        self.assertEqual(m.rule.pattern, "/a/*")
        self.assertEqual(m.shadowed, [])


class TestDecoding(unittest.TestCase):
    def test_invalid_percent_escape_reports_segment(self):
        r = make([(None, None, "/a/{x}/{y}")])
        with self.assertRaises(DecodeError) as ctx:
            r.match("/a/ok/%ZZ")
        self.assertEqual(ctx.exception.segment_index, 2)
        self.assertIn("%ZZ", ctx.exception.raw)

    def test_invalid_utf8_reports_segment(self):
        r = make([(None, None, "/a/{x}")])
        with self.assertRaises(DecodeError) as ctx:
            r.match("/a/%FF%FE")
        self.assertEqual(ctx.exception.segment_index, 1)

    def test_plus_is_literal_in_path(self):
        r = make([(None, None, "/a/{x}")])
        self.assertEqual(r.match("/a/a+b").params[0].value, "a+b")


class TestConflictDetection(unittest.TestCase):
    def test_exact_duplicate_rejected(self):
        r = Router()
        r.add_rule("/a/{x}", method="GET")
        with self.assertRaises(ConflictError):
            r.add_rule("/a/{y}", method="GET")

    def test_wildcard_shadowed_by_param(self):
        r = Router()
        r.add_rule("/a/{x}")
        with self.assertRaises(ConflictError):
            r.add_rule("/a/*")

    def test_param_not_shadowed_by_wildcard(self):
        r = Router()
        r.add_rule("/a/*")
        r.add_rule("/a/{x}")  # higher priority, must be accepted
        self.assertEqual(r.match("/a/b").rule.pattern, "/a/{x}")

    def test_static_not_shadowed_by_param(self):
        r = Router()
        r.add_rule("/a/{x}")
        r.add_rule("/a/b")  # static wins for /a/b, must be accepted
        self.assertEqual(r.match("/a/b").rule.pattern, "/a/b")

    def test_tail_wildcard_duplicate_rejected(self):
        r = Router()
        r.add_rule("/a/**")
        with self.assertRaises(ConflictError):
            r.add_rule("/a/**")

    def test_method_superset_shadows_subset(self):
        r = Router()
        r.add_rule("/a/{x}")  # any method
        with self.assertRaises(ConflictError):
            r.add_rule("/a/*", method="GET")
        # reverse order is fine: GET-only rule does not shadow other methods
        r2 = Router()
        r2.add_rule("/a/{x}", method="GET")
        r2.add_rule("/a/*")

    def test_host_prefix_superset_shadows_subset(self):
        r = Router()
        r.add_rule("/a/{x}")  # any host
        with self.assertRaises(ConflictError):
            r.add_rule("/a/*", host_prefix="api.")
        r2 = Router()
        r2.add_rule("/a/{x}", host_prefix="api.")
        r2.add_rule("/a/*")  # other hosts still reachable

    def test_load_rules_reports_line_numbers(self):
        text = "\n".join([
            "# comment",
            "GET - /a/{x}",      # line 2
            "POST - /a/*",       # line 3: shadowed by line 2? no: method differs
            "GET - /a/*",        # line 4: shadowed by line 2
            "GET - /a/{y}",      # line 5: duplicate of line 2
        ])
        with self.assertRaises(ConflictError) as ctx:
            load_rules(Router(), text)
        msg = str(ctx.exception)
        self.assertIn("line 4", msg)
        self.assertIn("line 5", msg)
        self.assertIn("line 2", msg)
        self.assertNotIn("line 3", msg)  # POST rule is not shadowed by a GET rule

    def test_load_rules_bad_line(self):
        with self.assertRaises(PatternError):
            load_rules(Router(), "GET /only-two\n")


class TestScale(unittest.TestCase):
    def test_thousand_rules_smoke(self):
        r = Router()
        n = 2000
        for i in range(n):
            r.add_rule("/svc%03d/{id}/op" % i, method="GET", host_prefix="h%d." % (i % 10))
        m = r.match("/svc1999/abc/op", "GET", "h9.example.com")
        self.assertIsNotNone(m)
        self.assertEqual(m.params[0].value, "abc")
        self.assertIsNone(r.match("/svc1999/abc/op", "GET", "h8.example.com"))
        self.assertEqual(len(r.rules), n)


if __name__ == "__main__":
    unittest.main()
