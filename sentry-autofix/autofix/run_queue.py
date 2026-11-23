"""Step 2: for each open `autofix:queued` issue, clone the repo and run headless Claude Code."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from . import github_client as ghc
from .config import REPO_ROOT, Config

log = logging.getLogger(__name__)

PROMPT_PATH = REPO_ROOT / "prompts" / "fix.md"


def build_prompt(repo: str, number: int) -> str:
    return f"{PROMPT_PATH.read_text()}\n\n---\nRepository: {repo}\nIssue: #{number}\n"


def parse_final_line(text: str) -> dict | None:
    """The agent is asked to end with one line of JSON; find the last parseable one."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def run_agent(cfg: Config, repo: str, number: int, clone_dir: Path) -> dict | None:
    cmd = [
        "claude",
        "-p",
        build_prompt(repo, number),
        "--output-format",
        "json",
        "--allowedTools",
        cfg.allowed_tools,
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]
    log.info("running agent on %s#%d in %s", repo, number, clone_dir)
    proc = subprocess.run(
        cmd,
        cwd=clone_dir,
        capture_output=True,
        text=True,
        timeout=cfg.timeout_minutes * 60,
    )
    (clone_dir.parent / f"{clone_dir.name}.agent.json").write_text(proc.stdout)
    if proc.returncode != 0:
        log.error("agent exited %d: %s", proc.returncode, proc.stderr[-2000:])
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.error("agent output was not JSON")
        return None
    log.info(
        "agent cost: $%s, turns: %s",
        envelope.get("total_cost_usd"),
        envelope.get("num_turns"),
    )
    return parse_final_line(envelope.get("result", ""))


def process_issue(cfg: Config, repo: str, number: int) -> str:
    L = cfg.labels
    # Claim the issue first so an overlapping run can't pick it up too.
    ghc.set_labels(repo, number, add=[L["in_progress"]], remove=[L["queued"]])
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    clone_dir = cfg.workdir / f"{repo.replace('/', '__')}-{number}-{int(time.time())}"
    try:
        ghc.clone(repo, str(clone_dir))
        outcome = run_agent(cfg, repo, number, clone_dir)
    except subprocess.TimeoutExpired:
        outcome = None
        log.error("agent timed out after %d min", cfg.timeout_minutes)
    except Exception as exc:  # noqa: BLE001 - report any failure on the issue
        outcome = None
        log.exception("failed on %s#%d: %s", repo, number, exc)

    result = (outcome or {}).get("result")
    if result == "pr-open":
        shutil.rmtree(clone_dir, ignore_errors=True)
        return result
    if result != "needs-human":
        # The agent crashed, timed out, or didn't report; make sure a human sees it.
        ghc.comment(
            repo,
            number,
            "Autofix run ended without a result (crash, timeout, or no final "
            "report). Marking for a human. Clone kept on the runner for inspection: "
            f"`{clone_dir}`",
        )
    ghc.set_labels(
        repo, number, add=[L["needs_human"]], remove=[L["in_progress"], L["queued"]]
    )
    return "needs-human"


def run(cfg: Config, dry_run: bool = False) -> dict[str, int]:
    tally: dict[str, int] = {}
    budget = cfg.max_issues_per_run
    for project in cfg.projects:
        if budget <= 0:
            break
        repo = project.github_repo
        for issue in ghc.queued_issues(repo, cfg.labels["queued"], budget):
            if dry_run:
                log.info(
                    "[dry-run] would fix %s#%d: %s",
                    repo,
                    issue["number"],
                    issue["title"],
                )
                result = "dry-run"
            else:
                result = process_issue(cfg, repo, issue["number"])
                log.info("%s#%d -> %s", repo, issue["number"], result)
            tally[result] = tally.get(result, 0) + 1
            budget -= 1
    return tally
