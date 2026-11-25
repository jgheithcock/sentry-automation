"""Configuration has exactly two sources, with no overlap:

* .env                - everything about the environment: credentials, Sentry org/API URL,
                        which Sentry projects map to which GitHub repos. Never committed.
                        Values in .env always replace anything already set in the shell.
* the settings below  - behavior only (labels, limits, timeouts, allowed tools). Committed.
                        Nothing environment-specific goes here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Behavior settings. Edit these here; they are the same on every machine.
# ---------------------------------------------------------------------------

# Extra Sentry search terms applied to every project.
SENTRY_QUERY = "is:unresolved"
# Only issues with an event within this window are considered (e.g. "24h",
# "7d"), however long ago they first appeared: a bug still happening is a
# candidate.
LAST_SEEN_WITHIN = "30d"

# Label added to every issue this tool creates (used for de-duplication).
SOURCE_LABEL = "sentry"
# Add the queued label automatically so the fix agent picks issues up.
# Set to False to have a human add the queued label by hand.
AUTO_QUEUE = True
LABELS = {
    "queued": "autofix:queued",
    "in_progress": "autofix:in-progress",
    "pr_open": "autofix:pr-open",
    "needs_human": "needs-human",
    # Added once the closed issue's Sentry issue is marked resolved in next release.
    "resolved": "autofix:sentry-resolved",
}

# Max issues the fix agent works on per run (they run one at a time).
MAX_ISSUES_PER_RUN = 2
# Kill a single agent run after this many minutes.
TIMEOUT_MINUTES = 30
# Tools the headless agent may use. Narrow Bash once you know each repo's test command,
# e.g. "Read,Edit,Write,Glob,Grep,Bash(git:*),Bash(gh:*),Bash(pytest:*)"
ALLOWED_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
# Where fresh clones are made for each fix (relative to this repo, or absolute).
WORKDIR = "work"
# Model alias or ID passed to `claude --model`. Empty = Claude Code's default.
MODEL = ""

# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_ENV = [
    "GH_TOKEN",
    "SENTRY_TOKEN",
    "SENTRY_ORG",
    "SENTRY_API_BASE",
    "AUTOFIX_PROJECTS",
]

# Variables this tool depends on. Any value inherited from the shell is discarded
# so the only possible source is .env.
MANAGED_ENV = REQUIRED_ENV + [
    "GITHUB_TOKEN",
    "GH_HOST",
    "GH_ENTERPRISE_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_REGION",
    "AUTOFIX_CLAUDE_LOGIN",
]


class ConfigError(SystemExit):
    pass


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and # comments are ignored; empty values are dropped."""
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy .env.example to .env and fill it in."
        )
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            values[key] = value
    return values


def apply_dotenv(path: Path) -> dict[str, str]:
    """Make .env the only source: clear managed variables, then set everything from .env."""
    values = read_dotenv(path)
    for key in MANAGED_ENV:
        os.environ.pop(key, None)
    os.environ.update(values)

    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if missing:
        raise ConfigError(f"Missing in {path}: {', '.join(missing)}")

    has_key = bool(values.get("ANTHROPIC_API_KEY"))
    has_bedrock = values.get("CLAUDE_CODE_USE_BEDROCK") == "1"
    has_login = values.get("AUTOFIX_CLAUDE_LOGIN") == "1"
    if has_key + has_bedrock + has_login != 1:
        raise ConfigError(
            f"Set exactly one Claude credential in {path}: ANTHROPIC_API_KEY, "
            "CLAUDE_CODE_USE_BEDROCK=1 (with AWS_REGION), or AUTOFIX_CLAUDE_LOGIN=1."
        )
    if has_bedrock and not values.get("AWS_REGION"):
        raise ConfigError(f"CLAUDE_CODE_USE_BEDROCK=1 needs AWS_REGION in {path}")
    return values


@dataclass
class Project:
    sentry_project: str
    github_repo: str
    # Folder in the repo holding this project's code ("" = whole repo), e.g. one
    # Lambda in a monorepo. Shown on the GitHub issue so the agent starts there.
    code_path: str = ""


def parse_projects(spec: str) -> list[Project]:
    """'slug=owner/repo, other=owner/mono:lambdas/other' -> [Project, ...]

    The optional ':path' after the repo names the folder holding that Sentry
    project's code, for repos that hold several services (e.g. one per Lambda).
    """
    projects = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        slug, sep, target = item.partition("=")
        repo, _, path = target.partition(":")
        slug, repo, path = slug.strip(), repo.strip(), path.strip().strip("/")
        if not sep or not slug or repo.count("/") != 1 or "" in repo.split("/"):
            raise ConfigError(
                f"Bad AUTOFIX_PROJECTS entry {item!r}; expected "
                "sentry-project=owner/repo or sentry-project=owner/repo:path/in/repo"
            )
        if path.startswith("~") or ".." in path.split("/"):
            raise ConfigError(f"Bad path in AUTOFIX_PROJECTS entry {item!r}; use a path "
                              "relative to the repo root, e.g. lambdas/compare")
        projects.append(Project(slug, repo, path))
    if not projects:
        raise ConfigError("AUTOFIX_PROJECTS has no entries")
    return projects


@dataclass
class Config:
    # From .env
    sentry_token: str
    sentry_org: str
    sentry_api_base: str
    projects: list[Project]
    # From the behavior settings above
    sentry_query: str
    last_seen_within: str
    source_label: str
    auto_queue: bool
    labels: dict
    max_issues_per_run: int
    timeout_minutes: int
    allowed_tools: str
    workdir: Path
    model: str = ""


def load(env_path: Path | None = None) -> Config:
    env = apply_dotenv(env_path or REPO_ROOT / ".env")
    workdir = Path(WORKDIR).expanduser()
    if not workdir.is_absolute():
        workdir = REPO_ROOT / workdir
    return Config(
        sentry_token=env["SENTRY_TOKEN"],
        sentry_org=env["SENTRY_ORG"],
        sentry_api_base=env["SENTRY_API_BASE"].rstrip("/"),
        projects=parse_projects(env["AUTOFIX_PROJECTS"]),
        sentry_query=SENTRY_QUERY,
        last_seen_within=LAST_SEEN_WITHIN,
        source_label=SOURCE_LABEL,
        auto_queue=AUTO_QUEUE,
        labels=dict(LABELS),
        max_issues_per_run=MAX_ISSUES_PER_RUN,
        timeout_minutes=TIMEOUT_MINUTES,
        allowed_tools=ALLOWED_TOOLS,
        workdir=workdir,
        model=MODEL,
    )
