"""Small Sentry REST client (stdlib only)."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .config import Config

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


class SentryClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _get(self, url: str) -> tuple[object, str | None]:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.cfg.sentry_token}"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
            match = _NEXT_LINK.search(resp.headers.get("Link", ""))
            return body, (match.group(1) if match else None)

    def search_terms(self) -> str:
        """The issue search sync runs for each project (without the project filter).

        Paste this into the search bar on Sentry's Issues page to run it by hand,
        or open web_search_url(), which sync logs and doctor prints.
        """
        return f"{self.cfg.sentry_query} lastSeen:-{self.cfg.last_seen_within}"

    def new_issues(self, project_slug: str, max_pages: int = 5) -> list[dict]:
        """Unresolved issues in one project with an event within the configured window."""
        query = f"project:{project_slug} {self.search_terms()}"
        params = urllib.parse.urlencode({"query": query, "sort": "date", "limit": 100})
        url: str | None = (
            f"{self.cfg.sentry_api_base}/organizations/{self.cfg.sentry_org}/issues/?{params}"
        )
        issues: list[dict] = []
        for _ in range(max_pages):
            if not url:
                break
            page, url = self._get(url)
            issues.extend(page)  # type: ignore[arg-type]
        return issues

    def project_id(self, project_slug: str) -> str:
        url = f"{self.cfg.sentry_api_base}/projects/{self.cfg.sentry_org}/{project_slug}/"
        project, _ = self._get(url)
        return str(project["id"])  # type: ignore[index]

    def web_search_url(self, project_slug: str) -> str:
        """A link that opens the same search sync runs, in Sentry's web UI."""
        try:
            project = self.project_id(project_slug)
        except Exception:  # still give a usable link; pick the project by hand
            project = ""
        return web_search_url(self.cfg.sentry_api_base, self.cfg.sentry_org, project,
                              self.search_terms(), self.cfg.last_seen_within)

    def resolve_in_next_release(self, issue_id: str) -> None:
        """Mark resolved as of the project's next release (needs event:write).

        Events from the current release don't reopen it; an event from a later
        release marks it regressed, which sync picks up.
        """
        url = f"{self.cfg.sentry_api_base}/organizations/{self.cfg.sentry_org}/issues/{issue_id}/"
        req = urllib.request.Request(
            url, method="PUT", data=json.dumps({"status": "resolvedInNextRelease"}).encode(),
            headers={"Authorization": f"Bearer {self.cfg.sentry_token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60):
            pass

    def latest_event(self, issue_id: str) -> dict:
        url = (
            f"{self.cfg.sentry_api_base}/organizations/{self.cfg.sentry_org}"
            f"/issues/{issue_id}/events/latest/"
        )
        event, _ = self._get(url)
        return event  # type: ignore[return-value]


def web_search_url(api_base: str, org: str, project_id: str, query: str,
                   period: str) -> str:
    """Sentry web UI link for an issue search.

    The web UI filters by numeric project ID (the API accepts project:<slug> in
    the query instead). sentry.io API hosts (sentry.io, us.sentry.io,
    de.sentry.io) all map to the sentry.io UI, which redirects to the org's
    own page; a self-hosted Sentry serves the UI from its API host.
    """
    parts = urllib.parse.urlsplit(api_base)
    host = parts.netloc
    web = "https://sentry.io" if host == "sentry.io" or host.endswith(".sentry.io") \
        else f"{parts.scheme}://{host}"
    params = {"query": query, "statsPeriod": period, "sort": "date"}
    if project_id:
        params["project"] = project_id
    return f"{web}/organizations/{org}/issues/?{urllib.parse.urlencode(params)}"


def format_stacktrace(event: dict, max_frames: int = 20) -> str:
    """Render the exception(s) in a Sentry event as a readable, Python-style traceback."""
    blocks: list[str] = []
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for exc in entry.get("data", {}).get("values", []) or []:
            lines = []
            frames = ((exc.get("stacktrace") or {}).get("frames")) or []
            for f in frames[-max_frames:]:
                marker = "" if f.get("inApp") else "  [library]"
                lines.append(
                    f'  File "{f.get("filename") or f.get("absPath") or "?"}", '
                    f'line {f.get("lineNo", "?")}, in {f.get("function") or "?"}{marker}'
                )
                context = f.get("context") or []
                for line_no, code in context:
                    if line_no == f.get("lineNo"):
                        lines.append(f"    {code.strip()}")
            lines.append(f'{exc.get("type", "Error")}: {exc.get("value", "")}')
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(no stack trace in latest event)"
