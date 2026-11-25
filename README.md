# sentry-automation

Turns Sentry errors into GitHub issues, then has a headless Claude Code agent
write a failing test, fix the bug if possible, and open a draft PR for a human
to review. The agent labels the issue with `needs-human` if unfixed. A summary
of the agent's actions are written to the issue in all cases.

It runs as a plain cron job anywhere with `python3`, `git`, `gh` and `claude`
installed (a laptop for trying it out, a server such as EC2 for real use).
No database and no inbound network access is needed: all state lives in GitHub
issue labels.

## What's here

* [`sentry-autofix/`](sentry-autofix/) - Automation files for these tasks:
  * Sentry sync: monitors Sentry and creates GitHub issues for new errors
  * The fix-agent runner: works through the queued GitHub issues, fixing
    them and updating each issue (uses
    [`prompts/fix.md`](sentry-autofix/prompts/fix.md))
  * Resolve: when a GitHub issue is closed as completed, marks its Sentry
    issue resolved in the next release (and reopens the GitHub issue if the
    bug comes back)
  * `doctor`: validates the setup and workflow
* [`sentry-autofix-sandbox/`](sentry-autofix-sandbox/) - A tiny app with a
  planted bug for validating the workflow.

## Try it

1. Create your test target from the sandbox (see
   [its README](sentry-autofix-sandbox/README.md#create-your-own-test-target)).
2. Set up the sentry-autofix tool (see [setup steps](sentry-autofix/README.md#setup)).
3. Check everything with `python3 -m autofix doctor --full`, file the sample
   issue, and run `python3 -m autofix run-queue`. You should get a draft PR
   that fixes the planted bug and adds a test for it.
4. Turn it on for real:
   * On the machine it will run on (see [moving to another machine][move]),
     point `AUTOFIX_PROJECTS` at your real Sentry projects and repos, and use
     production credentials: a GitHub token scoped to those repos, and an
     Anthropic API key or Bedrock (`AUTOFIX_CLAUDE_LOGIN` is for testing only).
   * Run `python3 -m autofix doctor --full`, then
     `python3 -m autofix all --dry-run` to see what it would file.
   * Add the cron line from the setup steps (it runs `bin/run.sh`). That's the
     only on switch; remove the line to turn it off.
   * Optional: set `AUTO_QUEUE = False` in `autofix/config.py` to have a
     person choose which issues the agent works on, by adding the
     `autofix:queued` label.

[move]: sentry-autofix/README.md#moving-to-another-machine-eg-ec2
