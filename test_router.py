"""Self-tests for router.py. Run: python3 -m unittest -v test_router"""

import unittest

from router import (
    ParamDecodeError,
    RouteConflictError,
    RouteLoadError,
    Router,
    load_routes,
)


def make_router(*rules):
    """rules: (method, host, path) tuples; '-' = any."""
    r = Router()
    for method, host, path in rules:
        r.add_route(method, host, path)
    return r


class TestBasicMatching(unittest.TestCase):
    def test_static_exact(self):
        r = make_router(("GET", "-", "/users/list"))
        m = r.match("GET", "example.com", "/users/list")
        self.assertIsNotNone(m)
        self.assertEqual(m.rule.pattern, "/users/list")
        self.assertEqual(m.params, {})

    def test_static_no_match(self):
        r = make_router(("GET", "-", "/users/list"))
        self.assertIsNone(r.match("GET", "example.com", "/users/other"))

    def test_param_extraction(self):
        r = make_router(("GET", "-", "/users/{id}/posts/{pid}"))
        m = r.match("GET", "example.com", "/users/42/posts/abc")
        self.assertEqual(m.params["id"].value, "42")
        self.assertEqual(m.params["pid"].value, "abc")
        self.assertEqual(m.params["id"].raw, "42")

    def test_param_names_are_per_rule(self):
        # sibling rules sharing one trie param edge keep their own names
        r = make_router(
            ("GET", "-", "/a/{x}"),
            ("GET", "-", "/a/{y}/b"),
        )
        m1 = r.match("GET", "h", "/a/1")
        m2 = r.match("GET", "h", "/a/1/b")
        self.assertIn("x", m1.params)
        self.assertIn("y", m2.params)

    def test_single_wildcard_one_segment_only(self):
        r = make_router(("GET", "-", "/files/*/meta"))
        self.assertIsNotNone(r.match("GET", "h", "/files/a/meta"))
        self.assertIsNone(r.match("GET", "h", "/files/a/b/meta"))

    def test_tail_wildcard_zero_or_more(self):
        r = make_router(("GET", "-", "/static/**"))
        self.assertIsNotNone(r.match("GET", "h", "/static"))
        self.assertIsNotNone(r.match("GET", "h", "/static/a/b/c"))
        self.assertIsNone(r.match("GET", "h", "/other/a"))

    def test_method_filter(self):
        r = make_router(
            ("GET", "-", "/thing"),
            ("POST", "-", "/thing"),
        )
        self.assertEqual(r.match("GET", "h", "/thing").rule.method, "GET")
        self.assertEqual(r.match("POST", "h", "/thing").rule.method, "POST")
        self.assertIsNone(r.match("DELETE", "h", "/thing"))

    def test_host_prefix_filter(self):
        r = make_router(
            ("GET", "api.", "/info"),
            ("GET", "-", "/info"),
        )
        m = r.match("GET", "api.example.com", "/info")
        self.assertEqual(m.rule.host_prefix, "api.")
        m = r.match("GET", "www.example.com", "/info")
        self.assertEqual(m.rule.host_prefix, "")

    def test_query_string_ignored(self):
        r = make_router(("GET", "-", "/a/{x}"))
        m = r.match("GET", "h", "/a/1?x=2&y=3")
        self.assertEqual(m.params["x"].value, "1")


class TestPriority(unittest.TestCase):
    def test_static_beats_param_beats_star_beats_starstar(self):
        r = make_router(
            ("GET", "-", "/a/**"),
            ("GET", "-", "/a/*"),
            ("GET", "-", "/a/{p}"),
            ("GET", "-", "/a/lit"),
        )
        m = r.match("GET", "h", "/a/lit")
        self.assertEqual(m.rule.pattern, "/a/lit")
        self.assertEqual(
            [c.pattern for c in m.candidates],
            ["/a/lit", "/a/{p}", "/a/*", "/a/**"],
        )
        self.assertEqual([c.pattern for c in m.shadowed],
                         ["/a/{p}", "/a/*", "/a/**"])

    def test_first_differing_segment_decides(self):
        r = make_router(
            ("GET", "-", "/a/{x}/c"),
            ("GET", "-", "/a/b/{y}"),
        )
        m = r.match("GET", "h", "/a/b/c")
        self.assertEqual(m.rule.pattern, "/a/b/{y}")  # static 'b' > param at seg 1

    def test_same_shape_registration_order(self):
        # identical path shape + method: the earlier registration wins on
        # overlapping hosts (host is a filter, not a priority key)
        r = make_router(
            ("GET", "api.", "/x/{a}"),
            ("GET", "-", "/x/{a}"),
        )
        m = r.match("GET", "api.example.com", "/x/1")
        self.assertEqual(m.rule.host_prefix, "api.")
        self.assertEqual(len(m.candidates), 2)
        m = r.match("GET", "web.example.com", "/x/1")
        self.assertEqual(m.rule.host_prefix, "")

    def test_same_shape_different_methods_coexist(self):
        r = make_router(
            ("GET", "-", "/x/{a}"),
            ("POST", "-", "/x/{a}"),
        )
        self.assertEqual(r.match("GET", "h", "/x/1").rule.method, "GET")
        self.assertEqual(r.match("POST", "h", "/x/1").rule.method, "POST")

    def test_shadowed_candidates_reported_in_hit_order(self):
        # registered '*' first, but param outranks it: hit order follows
        # priority, not registration order
        r = make_router(
            ("GET", "-", "/u/*"),
            ("GET", "-", "/u/{id}"),
        )
        m = r.match("GET", "h", "/u/9")
        self.assertEqual([c.pattern for c in m.candidates], ["/u/{id}", "/u/*"])
        self.assertEqual(m.shadowed, m.candidates[1:])


class TestDecoding(unittest.TestCase):
    def test_percent_decoding_and_raw_preserved(self):
        r = make_router(("GET", "-", "/f/{name}"))
        m = r.match("GET", "h", "/f/a%2Fb%20c")
        self.assertEqual(m.params["name"].value, "a/b c")
        self.assertEqual(m.params["name"].raw, "a%2Fb%20c")

    def test_utf8_decoding(self):
        r = make_router(("GET", "-", "/f/{name}"))
        m = r.match("GET", "h", "/f/%E4%B8%AD%E6%96%87")
        self.assertEqual(m.params["name"].value, "中文")

    def test_invalid_percent_sequence_reports_segment_index(self):
        r = make_router(("GET", "-", "/f/{a}/{b}"))
        with self.assertRaises(ParamDecodeError) as ctx:
            r.match("GET", "h", "/f/ok/%ZZ")
        self.assertEqual(ctx.exception.index, 2)
        self.assertIn("%ZZ", ctx.exception.raw)

    def test_truncated_percent_sequence(self):
        r = make_router(("GET", "-", "/f/{a}"))
        with self.assertRaises(ParamDecodeError):
            r.match("GET", "h", "/f/abc%1")

    def test_invalid_utf8_rejected(self):
        r = make_router(("GET", "-", "/f/{a}"))
        with self.assertRaises(ParamDecodeError) as ctx:
            r.match("GET", "h", "/f/%FF%FE")
        self.assertIn("UTF-8", str(ctx.exception))


class TestConflictDetection(unittest.TestCase):
    def test_duplicate_reported_with_lineno(self):
        text = "GET - /a/b\nGET - /a/b\n"
        with self.assertRaises(RouteLoadError) as ctx:
            load_routes(text)
        msg = str(ctx.exception)
        self.assertIn("line 2", msg)
        self.assertIn("line 1", msg)
        self.assertIn("duplicate", msg)

    def test_fully_shadowed_rule_reported(self):
        # same path shape; the later rule only narrows method/host, so the
        # earlier any-method/any-host rule wins on every shared request
        text = (
            "* - /a/**\n"
            "GET api. /a/**\n"
        )
        with self.assertRaises(RouteLoadError) as ctx:
            load_routes(text)
        self.assertIn("shadowed", str(ctx.exception))

    def test_more_specific_rule_is_not_shadowed(self):
        # '/a/{x}' covers '/a/b' as a match set, but static wins on ties of
        # path shape, so '/a/b' stays reachable and must NOT be flagged
        text = "GET - /a/{x}\nGET - /a/b\nGET - /ok\n"
        r = load_routes(text)
        self.assertEqual(r.match("GET", "h", "/a/b").rule.pattern, "/a/b")

    def test_shadow_by_param_shape(self):
        # /a/{x} registered first makes /a/{y} unreachable
        text = "GET - /a/{x}\nGET - /a/{y}\n"
        with self.assertRaises(RouteLoadError) as ctx:
            load_routes(text)
        self.assertIn("shadowed", str(ctx.exception))

    def test_not_shadowed_when_static_wins(self):
        # /a/b is still reachable even though /a/{x} exists: static wins
        r = make_router(("GET", "-", "/a/{x}"), ("GET", "-", "/a/b"))
        m = r.match("GET", "h", "/a/b")
        self.assertEqual(m.rule.pattern, "/a/b")

    def test_not_shadowed_across_methods(self):
        r = make_router(("GET", "-", "/a/**"), ("POST", "-", "/a/b"))
        self.assertEqual(r.match("POST", "h", "/a/b").rule.pattern, "/a/b")

    def test_not_shadowed_across_host_prefixes(self):
        r = make_router(("GET", "api.", "/a/**"), ("GET", "web.", "/a/b"))
        self.assertEqual(r.match("GET", "web.x", "/a/b").rule.pattern, "/a/b")

    def test_non_strict_load_collects_problems(self):
        text = "GET - /a/{x}\nGET - /a/{y}\nGET - /ok\n"
        r = load_routes(text, strict=False)
        self.assertEqual(len(r.load_problems), 1)
        self.assertIsNotNone(r.match("GET", "h", "/ok"))

    def test_malformed_patterns_reported_with_lineno(self):
        for bad in ("GET - /a/**/b", "GET - /a/{}", "GET - /a/{1x}", "GET - rel/path"):
            with self.assertRaises(RouteLoadError) as ctx:
                load_routes("GET - /ok\n" + bad + "\n")
            self.assertIn("line 2", str(ctx.exception), bad)


class TestLoader(unittest.TestCase):
    def test_comments_and_blanks(self):
        r = load_routes("# comment\n\nGET - /a\n")
        self.assertEqual(len(r.rules), 1)

    def test_two_field_form(self):
        r = load_routes("GET /a\n")
        self.assertEqual(r.rules[0].host_prefix, "")

    def test_bad_field_count(self):
        with self.assertRaises(RouteLoadError):
            load_routes("GET\n")


if __name__ == "__main__":
    unittest.main()
