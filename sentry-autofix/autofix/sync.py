"""Step 1: create GitHub issues for new Sentry issues that don't have one yet."""

from __future__ import annotations

import logging

from . import github_client as ghc
from .config import Config
from .sentry_client import SentryClient, format_stacktrace

log = logging.getLogger(__name__)


def issue_title(sentry_issue: dict) -> str:
    title = sentry_issue.get("title", "Untitled error").strip()
    if len(title) > 180:
        title = title[:177] + "..."
    return f"[{sentry_issue['shortId']}] {title}"


def issue_body(sentry_issue: dict, stacktrace: str, code_path: str = "") -> str:
    return "\n".join(
        [
            f"**Sentry issue:** [{sentry_issue['shortId']}]({sentry_issue.get('permalink', '')})",
            f"**Sentry issue ID:** `{sentry_issue['id']}`",
            *([f"**Code path:** `{code_path}/`"] if code_path else []),
            f"**Culprit:** `{sentry_issue.get('culprit') or 'unknown'}`",
            f"**Events:** {sentry_issue.get('count', '?')} · **Users:** {sentry_issue.get('userCount', '?')}",
            f"**First seen:** {sentry_issue.get('firstSeen', '?')} · **Last seen:** {sentry_issue.get('lastSeen', '?')}",
            "",
            "### Stack trace (latest event)",
            "```",
            stacktrace,
            "```",
            "",
            "_Created automatically by sentry-autofix._",
            f"<!-- sentry-issue-id: {sentry_issue['id']} -->",
        ]
    )


def is_regression(filed: dict | None, resolved_label: str) -> bool:
    """A Sentry issue we resolved is being seen (unresolved) again."""
    return bool(filed) and filed["state"] == "closed" and resolved_label in filed["labels"]


def reopen_regression(cfg: Config, repo: str, filed: dict, short_id: str,
                      dry_run: bool) -> None:
    number, L = filed["number"], cfg.labels
    if dry_run:
        log.info("[dry-run] would reopen %s#%d: %s regressed in Sentry", repo, number, short_id)
        return
    ghc.reopen(repo, number)
    ghc.set_labels(repo, number,
                   add=[L["queued"]] if cfg.auto_queue else [],
                   remove=[L["resolved"], L["pr_open"], L["needs_human"], L["in_progress"]])
    ghc.comment(repo, number,
                f"Sentry reports {short_id} again after it was resolved (regressed in a "
                f"later release). Reopened"
                + (" and queued for the fix agent." if cfg.auto_queue else "."))
    log.info("reopened %s#%d: %s regressed", repo, number, short_id)


def run(cfg: Config, dry_run: bool = False) -> int:
    sentry = SentryClient(cfg)
    created = 0
    for project in cfg.projects:
        repo = project.github_repo
        if not dry_run:
            ghc.ensure_labels(repo, [cfg.source_label, *cfg.labels.values()])
        filed = {sid: i for i in ghc.filed_issues(repo, cfg.source_label)
                 if (sid := ghc.short_id_from_title(i["title"]))}
        known = set(filed)
        issues = sentry.new_issues(project.sentry_project)
        log.info(
            "%s: %d Sentry issue(s), %d already filed in %s",
            project.sentry_project,
            len(issues),
            len(known),
            repo,
        )
        # To check the result yourself (e.g. when it finds 0), open this link: it
        # runs the same search in Sentry's web UI.
        log.info(
            "%s: search URL used in Sentry: %s",
            project.sentry_project,
            sentry.web_search_url(project.sentry_project),
        )
        for si in issues:
            if si["shortId"] in known:
                if is_regression(filed.get(si["shortId"]), cfg.labels["resolved"]):
                    reopen_regression(cfg, repo, filed[si["shortId"]], si["shortId"], dry_run)
                    filed.pop(si["shortId"])  # once per run, even if two projects share a repo
                continue
            title = issue_title(si)
            body = issue_body(
                si,
                format_stacktrace(sentry.latest_event(si["id"])),
                project.code_path,
            )
            labels = [cfg.source_label] + (
                [cfg.labels["queued"]] if cfg.auto_queue else []
            )
            if dry_run:
                log.info("[dry-run] would create in %s: %s", repo, title)
            else:
                url = ghc.create_issue(repo, title, body, labels)
                log.info("created %s", url)
            known.add(si["shortId"])
            created += 1
    return created
