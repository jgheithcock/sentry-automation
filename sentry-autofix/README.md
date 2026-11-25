# sentry-autofix

Polls Sentry for new issues, files them as GitHub issues, and runs headless
Claude Code on each queued issue to write a failing test, fix the bug, and open
a draft PR. When the GitHub issue is closed, it marks the Sentry issue resolved
in the next release. Runs anywhere with `python3`, `gh`, `git` and `claude`
installed: a laptop for the POC, an EC2 instance in production. No inbound
network access is needed.

## How it works

```
cron (every 15–60 min) -> bin/run.sh -> `python3 -m autofix all`
  1. resolve: GitHub issues closed as completed -> Sentry issue
     "resolved in next release", label: autofix:sentry-resolved
  2. sync: new Sentry issues -> GitHub issues "[SHORT-ID] title",
     labels: sentry, autofix:queued
     (a resolved issue that regressed -> GitHub issue reopened and queued)
  3. run-queue:
      autofix:queued issues -> fresh clone -> `claude -p prompts/fix.md`
      agent comments progress, opens draft PR from claude/issue-N-*,
      labels issue as autofix:pr-open, or needs-human if it can't fix it
```

All state lives in GitHub labels, so there is no local database to migrate.

| Label                     | Meaning                                        |
| ------------------------- | ---------------------------------------------- |
| `sentry`                  | Created by this tool (used for de-duplication) |
| `autofix:queued`          | Waiting for the agent; add by hand to queue    |
| `autofix:in-progress`     | Agent is working on it                         |
| `autofix:pr-open`         | Draft PR opened                                |
| `needs-human`             | Agent couldn't fix it, or the run failed       |
| `autofix:sentry-resolved` | Closed, and the Sentry issue marked resolved   |

## Configuration

There are exactly two places settings live, and they never overlap:

- `.env` - Everything about the environment: GitHub token, Sentry
  token/org/API URL, Claude credentials, and `AUTOFIX_PROJECTS` (which Sentry
  projects map to which GitHub repos) (**Not committed**)
- `autofix/config.py` - Behavior only, in the settings block at the top:
  labels, poll window, limits, timeouts, allowed tools (**Committed to repo**)

`.env` is the only source for credentials. Values already set in your shell
(e.g. a `GH_TOKEN` exported in `~/.zshrc`) are ignored, and the tool stops with
a clear error if a required value is missing. Claude must be configured with
exactly one of `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK=1`, or
`AUTOFIX_CLAUDE_LOGIN=1` (use the Claude account `claude` is signed in with;
meant for testing on a laptop).

### Mapping Sentry projects to repos

`AUTOFIX_PROJECTS` in `.env` is a comma-separated list of
`sentry-project=owner/repo`. Several Sentry projects can share one repo. If a
repo holds several services (e.g. one folder per Lambda), add the folder after
a colon:

```
AUTOFIX_PROJECTS=fn-a=myorg/backend:lambdas/a,fn-b=myorg/backend:lambdas/b
```

The folder is added to each GitHub issue as **Code path:**, and the agent
starts there and uses that folder's tests. `doctor` checks that it exists.

## Setup

1. Install the tools: `python3`, `git`, `gh`, and Claude Code
   (`npm install -g @anthropic-ai/claude-code`). There are no Python packages
   to install; everything else is the standard library.
2. `cp .env.example .env` and fill in the tokens (see comments in the 
   [file](./.env.example)).
3. Optionally adjust behavior in the settings block at the top of
   `autofix/config.py` (labels, limits, timeouts). The defaults are fine to
   start.
4. Check everything (tools, tokens, repo and Sentry access):
   `python3 -m autofix doctor` (add `--full` to also test write access and a
   Claude call).
5. Dry run (reads only; creates nothing):
   ```bash
   python3 -m autofix all --dry-run
   ```
6. Real run: `bin/run.sh`
7. Schedule it (`crontab -e`), using the full path to this folder:
   ```
   */15 * * * * cd /path/to/sentry-autofix && bin/run.sh >> logs/cron.log 2>&1
   ```

Commands: `python3 -m autofix resolve`, `python3 -m autofix sync`,
`python3 -m autofix run-queue`, `python3 -m autofix all` (all three in that
order), each with optional `--dry-run`. Logs go to
`logs/YYYY-MM-DD.log`; each agent run's raw output is saved next to its clone
in `work/`.

### Closing the loop in Sentry

When a person closes a GitHub issue filed by this tool as completed (or
merges a PR whose body says `Fixes #N`), the next run marks the Sentry issue
"resolved in next release" and adds `autofix:sentry-resolved`. Sentry then
counts the bug as fixed once the next release ships. Issues closed as "not
planned" are left alone in Sentry.

If the bug comes back in a later release, Sentry marks it regressed and sync
reopens the GitHub issue, swaps its labels back to `autofix:queued` (when
`AUTO_QUEUE` is on) and says why in a comment.

The agent's PRs also include `Fixes <SHORT-ID>`, so if your releases send
commits to Sentry, Sentry resolves the issue from the release itself as well.

### Seeing what sync searched for

Sync searches each Sentry project for `SENTRY_QUERY` plus
`lastSeen:-LAST_SEEN_WITHIN` (both set in `autofix/config.py`; by default
`is:unresolved lastSeen:-7d`): every unresolved issue that is still happening,
however long ago it first appeared. Each run logs a link that opens that same
search in Sentry's web UI, and `python3 -m autofix doctor` prints it along with
how many issues match right now. If a run finds 0 issues, open the link to
check for yourself. The usual cause is that nothing has had an event within
the window: widen `LAST_SEEN_WITHIN` (e.g. `"30d"`) for a test run.

Sentry issues that already have a GitHub issue (open or closed) are never
filed again. If a bug comes back after its GitHub issue was closed, reopen
that issue and add `autofix:queued` to run the agent on it again.

## Tests

```bash
python3 -m unittest discover tests
```

## Moving to another machine (e.g. EC2)

1. Install the tools as in Setup step 1.
2. Clone this repo as the service user.
3. Create `.env` with that machine's values (the only file that changes):
   - GitHub: token for the bot account or GitHub App (scoped to the target
     repos).
   - Sentry: internal-integration token (Issue & Event: Read & Write,
     Project: Read). Write is only used to mark issues resolved.
   - Claude: API key, or `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_REGION` with an
     instance IAM role.
4. Run `python3 -m autofix doctor --full`, then a dry run, then add the cron
   line.

Nothing else is machine-specific.
