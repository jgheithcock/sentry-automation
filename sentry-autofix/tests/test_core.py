"""Unit tests for the pure logic (no network). Run: python3 -m unittest discover tests"""
import unittest

from autofix.github_client import short_id_from_title
from autofix.run_queue import parse_final_line
from autofix.sentry_client import format_stacktrace, web_search_url
from autofix.sync import issue_body, issue_title

SENTRY_ISSUE = {
    "id": "4567890123",
    "shortId": "BILLING-1A",
    "title": "ValueError: could not convert string to float: '1,234.50'",
    "culprit": "billing.amounts in parse_amount",
    "permalink": "https://example-org.sentry.io/issues/4567890123/",
    "count": "17",
    "userCount": 4,
    "firstSeen": "2026-11-20T10:00:00Z",
    "lastSeen": "2026-11-20T12:00:00Z",
}

EVENT = {
    "entries": [{
        "type": "exception",
        "data": {"values": [{
            "type": "ValueError",
            "value": "could not convert string to float: '1,234.50'",
            "stacktrace": {"frames": [
                {"filename": "billing/amounts.py", "lineNo": 12, "function": "parse_amount",
                 "inApp": True, "context": [[12, "    return float(text)\n"]]},
            ]},
        }]},
    }]
}


class TitleTests(unittest.TestCase):
    def test_title_round_trips_short_id(self):
        self.assertEqual(short_id_from_title(issue_title(SENTRY_ISSUE)), "BILLING-1A")

    def test_untagged_title_has_no_short_id(self):
        self.assertIsNone(short_id_from_title("Some manual issue"))

    def test_long_title_truncated(self):
        long = dict(SENTRY_ISSUE, title="x" * 500)
        self.assertLessEqual(len(issue_title(long)), 200)


class StacktraceTests(unittest.TestCase):
    def test_formats_frame_and_error(self):
        text = format_stacktrace(EVENT)
        self.assertIn('File "billing/amounts.py", line 12, in parse_amount', text)
        self.assertIn("return float(text)", text)
        self.assertIn("ValueError: could not convert string to float", text)

    def test_no_exception(self):
        self.assertIn("no stack trace", format_stacktrace({"entries": []}))

    def test_body_contains_ids(self):
        body = issue_body(SENTRY_ISSUE, format_stacktrace(EVENT))
        self.assertIn("`4567890123`", body)
        self.assertIn("BILLING-1A", body)
        self.assertNotIn("Code path", body)
        body = issue_body(SENTRY_ISSUE, format_stacktrace(EVENT), "lambdas/compare")
        self.assertIn("**Code path:** `lambdas/compare/`", body)


class FinalLineTests(unittest.TestCase):
    def test_parses_last_json_line(self):
        text = 'Done.\n{"issue": 3, "result": "pr-open", "pr": "u", "summary": "s"}'
        self.assertEqual(parse_final_line(text)["result"], "pr-open")

    def test_backticked_line(self):
        text = '`{"issue": 3, "result": "needs-human", "pr": null, "summary": "s"}`'
        self.assertEqual(parse_final_line(text)["result"], "needs-human")

    def test_missing(self):
        self.assertIsNone(parse_final_line("no json here"))


class WebSearchUrlTests(unittest.TestCase):
    def test_saas_hosts_map_to_sentry_io(self):
        for base in ("https://sentry.io/api/0", "https://de.sentry.io/api/0"):
            url = web_search_url(base, "acme", "42", "is:unresolved lastSeen:-24h", "24h")
            self.assertTrue(url.startswith("https://sentry.io/organizations/acme/issues/?"))
            self.assertIn("project=42", url)
            self.assertIn("query=is%3Aunresolved+lastSeen%3A-24h", url)

    def test_self_hosted_keeps_host(self):
        url = web_search_url("https://sentry.example.com/api/0", "acme", "", "q", "7d")
        self.assertTrue(url.startswith("https://sentry.example.com/organizations/acme/issues/?"))
        self.assertNotIn("project=", url)


class LabelListingTests(unittest.TestCase):
    """Label lookups must use the REST issues API, never search (which lags)."""

    def test_queued_uses_rest_and_sorts_oldest_first(self):
        from autofix import github_client as g
        calls = []

        def fake_gh(*args, **kwargs):
            calls.append(args)
            return '{"number":2,"title":"[A-2] b"}\n{"number":1,"title":"[A-1] a"}\n'

        saved = g.gh
        g.gh = fake_gh
        try:
            self.assertEqual([i["number"] for i in g.queued_issues("me/r", "autofix:queued", 5)],
                             [1, 2])
            self.assertEqual(g.existing_short_ids("me/r", "sentry"), {"A-1", "A-2"})
        finally:
            g.gh = saved
        for args in calls:
            self.assertEqual(args[0], "api")
            self.assertNotIn("search", " ".join(args))


if __name__ == "__main__":
    unittest.main()
