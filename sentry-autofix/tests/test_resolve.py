"""Tests for closing the loop: resolve in Sentry, reopen on regression (no network)."""
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from autofix import config, doctor, resolve, sync
from autofix import github_client as ghc

LABELS = dict(config.LABELS)
RESOLVED = LABELS["resolved"]


def make_cfg(auto_queue: bool = True) -> config.Config:
    return config.Config(
        sentry_token="t", sentry_org="acme", sentry_api_base="https://sentry.io/api/0",
        projects=[config.Project("billing", "me/app"), config.Project("web", "me/app")],
        sentry_query="is:unresolved", last_seen_within="7d", source_label="sentry",
        auto_queue=auto_queue, labels=dict(LABELS), max_issues_per_run=2,
        timeout_minutes=1, allowed_tools="Read", workdir=Path("work"),
    )


class FakeGitHub:
    """Records calls to the github_client functions resolve/sync use."""

    def __init__(self, filed):
        self.filed = filed
        self.calls = []
        self._saved = {}

    def __enter__(self):
        fakes = {
            "filed_issues": lambda repo, label, state="all", with_body=False:
                [i for i in self.filed if state == "all" or i["state"] == state],
            "ensure_labels": lambda *a: self.calls.append(("ensure_labels", a)),
            "set_labels": lambda repo, n, add=(), remove=():
                self.calls.append(("set_labels", n, list(add), list(remove))),
            "comment": lambda repo, n, body: self.calls.append(("comment", n)),
            "reopen": lambda repo, n: self.calls.append(("reopen", n)),
            "create_issue": lambda repo, title, body, labels:
                self.calls.append(("create_issue", title)) or "https://x/1",
        }
        for name, fn in fakes.items():
            self._saved[name] = getattr(ghc, name)
            setattr(ghc, name, fn)
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(ghc, name, fn)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def issue(number, short_id, state="closed", reason="completed", labels=("sentry",)):
    return {"number": number, "title": f"[{short_id}] boom", "state": state,
            "state_reason": reason if state == "closed" else None, "labels": list(labels),
            "body": f"**Sentry issue ID:** `{1000 + number}`\n<!-- sentry-issue-id: {1000 + number} -->"}


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.resolved_ids = []
        self._saved = resolve.SentryClient.resolve_in_next_release
        resolve.SentryClient.resolve_in_next_release = (
            lambda client, issue_id: self.resolved_ids.append(issue_id))

    def tearDown(self):
        resolve.SentryClient.resolve_in_next_release = self._saved

    def test_issue_id_from_body(self):
        self.assertEqual(resolve.sentry_issue_id("x <!-- sentry-issue-id: 42 --> y"), "42")
        self.assertEqual(resolve.sentry_issue_id("**Sentry issue ID:** `77`"), "77")
        self.assertIsNone(resolve.sentry_issue_id("no id here"))

    def test_only_completed_and_not_yet_resolved(self):
        filed = [issue(1, "A-1"),                                  # resolve
                 issue(2, "A-2", reason="not_planned"),            # leave alone
                 issue(3, "A-3", labels=("sentry", RESOLVED)),     # already done
                 issue(4, "A-4", state="open")]                    # still open
        with FakeGitHub(filed) as gh:
            self.assertEqual(resolve.run(make_cfg()), 1)
        self.assertEqual(self.resolved_ids, ["1001"])
        self.assertIn(("set_labels", 1, [RESOLVED], []), gh.calls)
        self.assertEqual(len(gh.named("ensure_labels")), 1)  # once, though 2 projects share the repo

    def test_dry_run_changes_nothing(self):
        with FakeGitHub([issue(1, "A-1")]) as gh:
            self.assertEqual(resolve.run(make_cfg(), dry_run=True), 1)
        self.assertEqual(self.resolved_ids, [])
        self.assertEqual(gh.calls, [])

    def test_sentry_error_leaves_issue_for_next_run(self):
        def fail(client, issue_id):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        resolve.SentryClient.resolve_in_next_release = fail
        with FakeGitHub([issue(1, "A-1")]) as gh, self.assertLogs("autofix.resolve", "ERROR") as logs:
            self.assertEqual(resolve.run(make_cfg()), 0)
        self.assertEqual(gh.named("set_labels"), [])  # no label, so it's retried next run
        self.assertIn("Read & Write", logs.output[0])


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(sync.SentryClient, n)
                       for n in ("new_issues", "latest_event", "web_search_url")}
        sync.SentryClient.new_issues = lambda c, slug: (
            [{"id": "1001", "shortId": "A-1", "title": "boom"},
             {"id": "1002", "shortId": "A-2", "title": "boom"},
             {"id": "1009", "shortId": "A-9", "title": "new"}] if slug == "billing" else [])
        sync.SentryClient.latest_event = lambda c, i: {}
        sync.SentryClient.web_search_url = lambda c, slug: "https://sentry.io/x"

    def tearDown(self):
        for n, fn in self._saved.items():
            setattr(sync.SentryClient, n, fn)

    def test_is_regression(self):
        self.assertTrue(sync.is_regression(issue(1, "A-1", labels=("sentry", RESOLVED)), RESOLVED))
        self.assertFalse(sync.is_regression(issue(1, "A-1"), RESOLVED))  # closed, not resolved by us
        self.assertFalse(sync.is_regression(issue(1, "A-1", state="open"), RESOLVED))
        self.assertFalse(sync.is_regression(None, RESOLVED))

    def test_reopens_resolved_regression_and_requeues(self):
        filed = [issue(1, "A-1", labels=("sentry", RESOLVED, LABELS["pr_open"])),
                 issue(2, "A-2", reason="not_planned")]  # a person decided; stays closed
        with FakeGitHub(filed) as gh:
            created = sync.run(make_cfg())
        self.assertEqual(created, 1)  # only A-9 is new
        self.assertEqual(gh.named("reopen"), [("reopen", 1)])
        _, n, add, remove = gh.named("set_labels")[0]
        self.assertEqual((n, add), (1, [LABELS["queued"]]))
        self.assertIn(RESOLVED, remove)
        self.assertIn(LABELS["pr_open"], remove)

    def test_no_requeue_when_auto_queue_off(self):
        with FakeGitHub([issue(1, "A-1", labels=("sentry", RESOLVED))]) as gh:
            sync.run(make_cfg(auto_queue=False))
        self.assertEqual(gh.named("set_labels")[0][2], [])


class SentryRequestTests(unittest.TestCase):
    def test_resolve_request_shape(self):
        from autofix.sentry_client import SentryClient
        with mock.patch("urllib.request.urlopen") as urlopen:
            SentryClient(make_cfg()).resolve_in_next_release("1001")
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.full_url, "https://sentry.io/api/0/organizations/acme/issues/1001/")
        self.assertEqual(json.loads(req.data), {"status": "resolvedInNextRelease"})
        self.assertEqual(req.get_header("Authorization"), "Bearer t")


class DoctorScopeTests(unittest.TestCase):
    def test_write_scope(self):
        self.assertIsNone(doctor.sentry_write_problem(["event:read", "event:write"]))
        self.assertIsNone(doctor.sentry_write_problem(None))  # unknown -> warn, not fail
        self.assertIn("Read & Write", doctor.sentry_write_problem(["event:read", "org:read"]))


if __name__ == "__main__":
    unittest.main()
