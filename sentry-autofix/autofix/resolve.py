"""Step 0: close the loop back to Sentry.

When a GitHub issue filed by sync is closed as completed (a person closed it, or
a merged PR with `Fixes #N` did), mark its Sentry issue "resolved in next
release". Sentry then treats the bug as fixed once the next release ships, and
reopens it (regressed) if it happens again in a later release; sync reopens the
GitHub issue in that case.

Issues closed as "not planned" are left alone in Sentry: closing one isn't a
claim that the bug is fixed. Each resolved issue gets the `resolved` label so
it is only sent to Sentry once.
"""

from __future__ import annotations

import logging
import re
import urllib.error

from . import github_client as ghc
from .config import Config
from .sentry_client import SentryClient

log = logging.getLogger(__name__)

_ISSUE_ID = re.compile(r"<!-- sentry-issue-id: (\d+) -->|\*\*Sentry issue ID:\*\* `(\d+)`")


def sentry_issue_id(body: str) -> str | None:
    """The Sentry issue ID that sync wrote into the GitHub issue body."""
    match = _ISSUE_ID.search(body or "")
    return (match.group(1) or match.group(2)) if match else None


def to_resolve(issues: list[dict], resolved_label: str) -> list[dict]:
    """Closed-as-completed issues not yet resolved in Sentry."""
    return [i for i in issues
            if i.get("state_reason") == "completed" and resolved_label not in i["labels"]]


def run(cfg: Config, dry_run: bool = False) -> int:
    sentry = SentryClient(cfg)
    resolved_label = cfg.labels["resolved"]
    done = 0
    for repo in dict.fromkeys(p.github_repo for p in cfg.projects):  # each repo once
        closed = ghc.filed_issues(repo, cfg.source_label, state="closed", with_body=True)
        pending = to_resolve(closed, resolved_label)
        if pending and not dry_run:
            ghc.ensure_labels(repo, [resolved_label])  # resolve runs before sync creates labels
        for issue in pending:
            number = issue["number"]
            issue_id = sentry_issue_id(issue.get("body", ""))
            if not issue_id:
                log.warning("%s#%d: no Sentry issue ID in the body; skipping", repo, number)
                continue
            if dry_run:
                log.info("[dry-run] would resolve Sentry issue %s (from %s#%d) "
                         "in next release", issue_id, repo, number)
                done += 1
                continue
            try:
                sentry.resolve_in_next_release(issue_id)
            except urllib.error.HTTPError as e:
                hint = (" (SENTRY_TOKEN needs Issue & Event: Read & Write)"
                        if e.code in (401, 403) else "")
                log.error("%s#%d: resolving Sentry issue %s failed: HTTP %s%s",
                          repo, number, issue_id, e.code, hint)
                continue
            ghc.set_labels(repo, number, add=[resolved_label])
            ghc.comment(repo, number,
                        f"Marked the Sentry issue as resolved in the next release "
                        f"(Sentry issue ID `{issue_id}`). If it happens again in a "
                        f"later release, this issue will be reopened.")
            log.info("%s#%d: resolved Sentry issue %s in next release", repo, number, issue_id)
            done += 1
    return done
