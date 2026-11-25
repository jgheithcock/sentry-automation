"""Thin wrapper around the `gh` CLI.

Authentication: `gh` reads GH_TOKEN from the environment, which config.load() fills
from .env (and only from .env). The same token is used by:
  * every `gh` call in this package,
  * the agent's own `gh` calls (it inherits the environment), and
  * `git push` inside the agent's clone, via use_gh_for_git_auth() below.
`gh` never falls back to its stored login while GH_TOKEN is set.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse

LABEL_COLORS = {
    "sentry": "6c5fc7",
    "autofix:queued": "fbca04",
    "autofix:in-progress": "1d76db",
    "autofix:pr-open": "0e8a16",
    "needs-human": "d93f0b",
    "autofix:sentry-resolved": "5319e7",
}


def gh(*args: str, input_text: str | None = None) -> str:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN is not set; call config.load() first (it reads .env)")
    result = subprocess.run(
        ["gh", *args], input=input_text, capture_output=True, text=True, env=os.environ
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def ensure_labels(repo: str, labels: list[str]) -> None:
    for name in labels:
        gh("label", "create", name, "-R", repo, "--force",
           "--color", LABEL_COLORS.get(name, "ededed"))


def issues_with_label(repo: str, label: str, state: str) -> list[dict]:
    """Issues (not PRs) carrying a label, as [{"number": ..., "title": ...}].

    Uses the REST issues endpoint, which reads GitHub's database directly. Don't
    use `gh issue list --label` here: with a label filter gh switches to the
    search API, whose index lags by seconds to minutes, so issues created a
    moment earlier (e.g. by sync in the same run) would be missed.
    """
    query = urllib.parse.urlencode({"labels": label, "state": state, "per_page": 100})
    out = gh("api", "--paginate", f"repos/{repo}/issues?{query}",
             "--jq", ".[] | select(.pull_request | not) | {number, title} | @json")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def existing_short_ids(repo: str, source_label: str) -> set[str]:
    """Sentry short IDs already filed, read from titles like '[PROJ-12A] ...'."""
    issues = issues_with_label(repo, source_label, "all")
    return {sid for i in issues if (sid := short_id_from_title(i["title"]))}


def filed_issues(repo: str, source_label: str, state: str = "all",
                 with_body: bool = False) -> list[dict]:
    """Issues filed by sync, with state, state_reason, label names (and body).

    Same REST endpoint (no search lag) as issues_with_label.
    """
    fields = "number, title, state, state_reason, labels: [.labels[].name]"
    if with_body:
        fields += ", body"
    query = urllib.parse.urlencode({"labels": source_label, "state": state, "per_page": 100})
    out = gh("api", "--paginate", f"repos/{repo}/issues?{query}",
             "--jq", f".[] | select(.pull_request | not) | {{{fields}}} | @json")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def short_id_from_title(title: str) -> str | None:
    if title.startswith("[") and "]" in title:
        return title[1:title.index("]")]
    return None


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    args = ["issue", "create", "-R", repo, "--title", title, "--body-file", "-"]
    for label in labels:
        args += ["--label", label]
    return gh(*args, input_text=body).strip()  # returns the issue URL


def queued_issues(repo: str, queued_label: str, limit: int) -> list[dict]:
    """Open issues waiting for the agent, oldest first."""
    issues = issues_with_label(repo, queued_label, "open")
    return sorted(issues, key=lambda i: i["number"])[:limit]


def set_labels(repo: str, number: int, add: list[str] = (), remove: list[str] = ()) -> None:
    args = ["issue", "edit", str(number), "-R", repo]
    for label in add:
        args += ["--add-label", label]
    for label in remove:
        args += ["--remove-label", label]
    gh(*args)


def reopen(repo: str, number: int) -> None:
    gh("issue", "reopen", str(number), "-R", repo)


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "-R", repo, "--body-file", "-", input_text=body)


def clone(repo: str, dest: str) -> None:
    gh("repo", "clone", repo, dest, "--", "--quiet")
    use_gh_for_git_auth(dest)


def use_gh_for_git_auth(repo_dir: str) -> None:
    """Make plain `git push/fetch` in this clone authenticate with GH_TOKEN.

    Without this, git uses whatever credential helper the machine has (e.g. the macOS
    keychain holding a personal login), so pushes would not come from the .env token.
    The empty value clears inherited helpers; this only affects this one clone.
    """
    key = "credential.https://github.com.helper"
    for args in (["config", key, ""], ["config", "--add", key, "!gh auth git-credential"]):
        subprocess.run(["git", "-C", repo_dir, *args], check=True, capture_output=True)
