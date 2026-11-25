"""Setup diagnostics: python -m autofix doctor [--full]

Checks, in order: .env/config, required tools, GitHub token and repo access,
Sentry token/org/projects, the Claude credential (format, and validity via a free
models-list call), and (with --full) write access and a one-line Claude call.
Every failure prints the likely cause and fix. Read-only unless --full is given.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import github_client as ghc
from .sentry_client import SentryClient, web_search_url

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

TOKEN_KINDS = {
    "github_pat_": "fine-grained personal access token",
    "ghp_": "classic personal access token",
    "gho_": "OAuth token from `gh auth login`",
    "ghs_": "GitHub App installation token",
    "ghu_": "GitHub App user token",
}

REPO_404_HINT = (
    "token can't see this repo. Check: (1) a fine-grained token's Resource owner must be "
    "the repo's owner (org or user), not your personal account; (2) the repo is in the "
    "token's 'Repository access' list; (3) an org may need to approve the token (it shows "
    "as pending until then); (4) for a gho_ token, the org must approve the GitHub CLI app."
)


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, status: str, name: str, detail: str = "") -> None:
        if status == FAIL:
            self.failed += 1
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n== {title}")


def token_kind(token: str) -> str:
    for prefix, kind in TOKEN_KINDS.items():
        if token.startswith(prefix):
            return kind
    return "unrecognized token format"


def run_cmd(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=os.environ)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return p.returncode, (p.stdout + p.stderr).strip()


def parse_http_response(raw: str) -> tuple[int | None, dict[str, str]]:
    """Status code and headers from `gh api -i` output."""
    status, headers = None, {}
    for line in raw.splitlines():
        if line.startswith("HTTP/") and status is None:
            parts = line.split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        elif ":" in line and status is not None:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        elif not line.strip() and status is not None:
            break
    return status, headers


def repo_access_problem(status: int | None, headers: dict[str, str]) -> str | None:
    if "x-github-sso" in headers:
        return ("org uses SAML SSO and this token isn't authorized for it. Authorize it: "
                + headers["x-github-sso"])
    if status == 404:
        return REPO_404_HINT
    if status == 401:
        return "GH_TOKEN is invalid or expired; create a new one and update .env"
    if status == 403:
        return "token is blocked by an org policy (OAuth app restriction or token approval)"
    if status != 200:
        return f"unexpected HTTP status {status}"
    return None


def sentry_get(cfg: config.Config, path: str) -> tuple[int, object]:
    req = urllib.request.Request(cfg.sentry_api_base + path,
                                 headers={"Authorization": f"Bearer {cfg.sentry_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def check_config(r: Report) -> config.Config | None:
    r.section("Configuration")
    shell = {k: os.environ[k] for k in config.MANAGED_ENV if os.environ.get(k)}
    try:
        cfg = config.load()
    except config.ConfigError as e:
        r.line(FAIL, ".env", str(e))
        return None
    r.line(PASS, ".env loaded", f"{len(cfg.projects)} project mapping(s): "
           + ", ".join(f"{p.sentry_project} -> {p.github_repo}"
                       + (f":{p.code_path}" if p.code_path else "") for p in cfg.projects))
    for key, value in shell.items():
        if os.environ.get(key) != value:
            r.line(WARN, f"{key} is also set in your shell",
                   "ignored (the .env value is used); remove it from your shell profile "
                   "to avoid confusion when running gh by hand")
    return cfg


def check_tools(r: Report) -> None:
    r.section("Tools")
    v = sys.version_info
    r.line(PASS, "python", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}")
    if shutil.which("python3") != sys.executable:
        r.line(WARN, "python3 on PATH", f"bin/run.sh will use {shutil.which('python3')}, "
               "not the python running this check; run doctor with that one too")
    for tool in ("gh", "git", "claude"):
        if not shutil.which(tool):
            r.line(FAIL, tool, "not found on PATH")
            continue
        code, out = run_cmd([tool, "--version"])
        r.line(PASS if code == 0 else FAIL, tool, out.splitlines()[0] if out else "")
    code, out = run_cmd(["git", "config", "user.email"])
    if code != 0 or not out:
        r.line(WARN, "git identity", "user.name/user.email not set; agent commits will fail. "
               "Run: git config --global user.name ... && git config --global user.email ...")


def check_github(r: Report, cfg: config.Config) -> None:
    r.section("GitHub")
    token = os.environ["GH_TOKEN"]
    kind = token_kind(token)
    r.line(PASS if kind != "unrecognized token format" else WARN, "GH_TOKEN type", kind)

    code, out = run_cmd(["gh", "auth", "status"])
    if "GH_TOKEN" in out:
        r.line(PASS, "gh is using GH_TOKEN from .env")
    else:
        r.line(FAIL, "gh is not using GH_TOKEN", out.splitlines()[0] if out else "")

    if not token.startswith("ghs_"):  # installation tokens can't call /user
        code, out = run_cmd(["gh", "api", "user", "--jq", ".login"])
        r.line(PASS if code == 0 else FAIL, "token identity",
               f"authenticated as {out}" if code == 0 else out)

    for project in cfg.projects:
        repo = project.github_repo
        code, out = run_cmd(["gh", "api", "-i", f"repos/{repo}"])
        status, headers = parse_http_response(out)
        problem = repo_access_problem(status, headers)
        if problem:
            r.line(FAIL, f"{repo}: read", problem)
            continue
        body = out.split("\n\n", 1)[-1] if "\n\n" in out else out
        private = '"private":true' in body.replace(" ", "")
        r.line(PASS, f"{repo}: read", "private repo visible" if private else "public repo visible")
        code, out = run_cmd(["gh", "issue", "list", "-R", repo, "--limit", "1"])
        r.line(PASS if code == 0 else FAIL, f"{repo}: list issues",
               "ok" if code == 0 else f"{out} (token needs Issues: Read)")
        if project.code_path:
            path = project.code_path
            code, out = run_cmd(["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".type?"])
            r.line(PASS if code == 0 else FAIL, f"{repo}: path {path}",
                   f"found ({project.sentry_project})" if code == 0
                   else f"not on the default branch ({project.sentry_project}); "
                   "check the :path in AUTOFIX_PROJECTS")


def check_sentry(r: Report, cfg: config.Config) -> None:
    r.section("Sentry")
    org = cfg.sentry_org
    status, body = sentry_get(cfg, f"/organizations/{org}/")
    hints = {
        0: f"can't reach {cfg.sentry_api_base} ({body})",
        401: "SENTRY_TOKEN is invalid or revoked",
        403: "token lacks org:read scope",
        404: f"org '{org}' not found: check SENTRY_ORG, or SENTRY_API_BASE "
             "(EU-hosted orgs use https://de.sentry.io/api/0)",
    }
    if status != 200:
        r.line(FAIL, f"organization {org}", hints.get(status, f"HTTP {status}"))
        return
    r.line(PASS, f"organization {org}", body.get("name", ""))  # type: ignore[union-attr]
    check_sentry_write(r, cfg)

    for project in cfg.projects:
        slug = project.sentry_project
        status, proj = sentry_get(cfg, f"/projects/{org}/{slug}/")
        if status != 200:
            hint = {404: "project slug not found (check AUTOFIX_PROJECTS)",
                    403: "token lacks project:read, or you aren't on the project's team"}
            r.line(FAIL, f"project {slug}", hint.get(status, f"HTTP {status}"))
            continue
        terms = SentryClient(cfg).search_terms()  # exactly what sync searches for
        q = urllib.parse.urlencode({"query": f"project:{slug} {terms}", "limit": 100})
        status, issues = sentry_get(cfg, f"/organizations/{org}/issues/?{q}")
        if status == 200:
            n = len(issues)  # type: ignore[arg-type]
            link = web_search_url(cfg.sentry_api_base, org, str(proj.get("id", "")),  # type: ignore[union-attr]
                                  terms, cfg.last_seen_within)
            r.line(PASS, f"project {slug}",
                   f"{'100+' if n >= 100 else n} issue(s) match '{terms}'. "
                   f"Same search in Sentry: {link}")
        else:
            r.line(FAIL, f"project {slug}",
                   "can't read issues (token needs event:read)" if status == 403
                   else f"issues query HTTP {status}")


def sentry_write_problem(scopes: list[str] | None) -> str | None:
    """Why the token can't resolve issues, or None. scopes=None means unknown."""
    if scopes is None:
        return None
    if "event:write" in scopes or "event:admin" in scopes:
        return None
    return ("token can read but not resolve issues; the resolve step needs "
            "Issue & Event: Read & Write (scope event:write)")


def check_sentry_write(r: Report, cfg: config.Config) -> None:
    """The resolve step marks issues resolved, which needs event:write."""
    status, root = sentry_get(cfg, "/")
    scopes = None
    if status == 200 and isinstance(root, dict):
        scopes = (root.get("auth") or {}).get("scopes")
    problem = sentry_write_problem(scopes)
    if problem:
        r.line(FAIL, "token can resolve issues", problem)
    elif scopes is None:
        r.line(WARN, "token can resolve issues",
               "couldn't read the token's scopes; make sure it has Issue & Event: "
               "Read & Write, or the resolve step will fail")
    else:
        r.line(PASS, "token can resolve issues", "has event:write")


def check_write_access(r: Report, cfg: config.Config) -> None:
    r.section("GitHub write access (--full)")
    for project in cfg.projects:
        repo = project.github_repo
        label = "autofix-doctor-check"
        code, out = run_cmd(["gh", "label", "create", label, "-R", repo, "--force"])
        if code == 0:
            run_cmd(["gh", "label", "delete", label, "-R", repo, "--yes"])
            r.line(PASS, f"{repo}: write issues/labels")
        else:
            r.line(FAIL, f"{repo}: write issues/labels", f"{out} (token needs Issues: Read and write)")

        tmp = tempfile.mkdtemp(prefix="autofix-doctor-")
        try:
            dest = os.path.join(tmp, "repo")
            code, out = run_cmd(["gh", "repo", "clone", repo, dest, "--", "--depth", "1", "--quiet"],
                                timeout=300)
            if code != 0:
                r.line(FAIL, f"{repo}: clone", out)
                continue
            ghc.use_gh_for_git_auth(dest)
            code, out = run_cmd(["git", "push", "--dry-run", "origin",
                                 "HEAD:refs/heads/claude/doctor-check"], cwd=dest)
            r.line(PASS if code == 0 else FAIL, f"{repo}: push with GH_TOKEN",
                   "ok (dry run, nothing pushed)" if code == 0
                   else f"{out.splitlines()[-1] if out else ''} (token needs Contents: Read and write)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


CONSOLE_KEYS = "console.anthropic.com > API keys"


def claude_key_problem(key: str) -> str | None:
    """Catch keys that can never work before spending an API call."""
    if key.startswith("sk-ant-"):
        return None
    if key.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-")) or key.startswith("sk-"):
        return ("this looks like an OpenAI key. Claude Code only accepts Anthropic credentials: "
                f"create a key at {CONSOLE_KEYS} (it starts with sk-ant-), or use "
                "CLAUDE_CODE_USE_BEDROCK=1 with AWS_REGION instead")
    return (f"unrecognized key format (Anthropic API keys start with sk-ant-); "
            f"check the value, or create one at {CONSOLE_KEYS}")


def parse_claude_json(out: str) -> dict:
    """The result object from `claude --output-format json`, ignoring any stderr lines."""
    for candidate in [out] + [l for l in reversed(out.splitlines()) if l.lstrip().startswith("{")]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def claude_error_hint(status: int | None, message: str, bedrock: bool, login: bool = False) -> str:
    """Likely cause and fix for a failed Claude call."""
    msg = message.lower()
    if "credit balance" in msg:
        return "the Anthropic account has no credits: add some under Billing in console.anthropic.com"
    if status == 401:
        if login:
            return ("Claude Code isn't signed in for this user: run `claude`, then /login "
                    "(or `claude auth login`) with your Claude account, and retry")
        if bedrock:
            return ("AWS rejected the credentials: check the instance IAM role or "
                    "`aws sts get-caller-identity`")
        return ("ANTHROPIC_API_KEY was rejected: it is invalid, revoked, or not an Anthropic key. "
                f"Create a new one at {CONSOLE_KEYS} and put it in .env")
    if status == 403:
        if bedrock:
            return (f"enable Claude model access in the Bedrock console for AWS_REGION, and give "
                    "the IAM role bedrock:InvokeModel and bedrock:InvokeModelWithResponseStream")
        return ("the key works but its workspace can't use this model: check the workspace's "
                "model access in console.anthropic.com, or set MODEL in autofix/config.py")
    if status == 404:
        return "model not found: check MODEL in autofix/config.py (empty uses Claude Code's default)"
    if status == 429:
        return "rate limited or over the spend limit: check Limits in console.anthropic.com, then retry"
    if status in (500, 529) or "overloaded" in msg:
        return "Anthropic API is overloaded or erroring: retry in a few minutes"
    return ""


def anthropic_key_status(key: str) -> tuple[int, str]:
    """Validate a key by listing models: free, uses no tokens, changes nothing."""
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            models = body.get("data") or [{}]
            return resp.status, f"models visible, e.g. {models[0].get('id', '?')}"
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        return e.code, message or e.reason
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def check_claude_credentials(r: Report) -> None:
    r.section("Claude")
    if os.environ.get("AUTOFIX_CLAUDE_LOGIN") == "1":
        r.line(PASS, "credential", "Claude Code's own sign-in (tested by --full). "
               "Fine for testing; use an API key or Bedrock for the unattended server")
        return
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        r.line(PASS, "credential", f"Amazon Bedrock in {os.environ.get('AWS_REGION')} "
               "(tested by --full)")
        return
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    problem = claude_key_problem(key)
    if problem:
        r.line(FAIL, "ANTHROPIC_API_KEY", problem)
        return
    status, detail = anthropic_key_status(key)
    if status == 200:
        r.line(PASS, "ANTHROPIC_API_KEY", f"valid Anthropic key ({detail})")
    elif status == 0:
        r.line(WARN, "ANTHROPIC_API_KEY", f"can't reach api.anthropic.com ({detail}); "
               "key not verified")
    else:
        hint = claude_error_hint(status, detail, bedrock=False) or detail
        r.line(FAIL, "ANTHROPIC_API_KEY", f"HTTP {status}: {hint}")


def check_claude(r: Report) -> None:
    r.section("Claude call (--full)")
    code, out = run_cmd(["claude", "-p", "Reply with exactly: OK", "--max-turns", "1",
                         "--output-format", "json"], timeout=180)
    data = parse_claude_json(out)
    result = str(data.get("result", "")).strip()
    if code == 0 and not data.get("is_error") and "OK" in result:
        r.line(PASS, "claude call", result[:80])
        return
    status = data.get("api_error_status")
    message = result or out.strip()[-300:]
    hint = claude_error_hint(status if isinstance(status, int) else None, message,
                             os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1",
                             os.environ.get("AUTOFIX_CLAUDE_LOGIN") == "1")
    detail = (f"HTTP {status}: " if status else "") + message[:200]
    r.line(FAIL if (code != 0 or data.get("is_error")) else WARN, "claude call",
           detail + (f". Fix: {hint}" if hint else ""))


def run(full: bool = False) -> int:
    r = Report()
    cfg = check_config(r)
    check_tools(r)
    if cfg:
        check_github(r, cfg)
        check_sentry(r, cfg)
        check_claude_credentials(r)
        if full:
            check_write_access(r, cfg)
            check_claude(r)
    print(f"\n{'All checks passed.' if not r.failed else f'{r.failed} check(s) failed.'}"
          + ("" if full or not cfg else " Run with --full to also test write access and Claude."))
    return 1 if r.failed else 0
