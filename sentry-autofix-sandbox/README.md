# sentry-autofix-sandbox

A tiny repo with a known bug, used to test the sentry-autofix agent without
touching real code.

## Create your own test target

This folder lives in `sentry-automation` so everything can be read and shared
in one place, but the tool never points at that repo. The fix agent gets write
access to every repo it works on (pushing branches, opening PRs, editing and
running code), so a test target inside `sentry-automation` would let it edit
its own prompt and settings. Instead, create a throwaway copy of this folder as
its own repo and point the tool at that. Real use works the same way: the
tool's credentials only reach the application repos it fixes.

From the `sentry-automation` checkout:

```bash
cp -R sentry-autofix-sandbox ~/sentry-autofix-sandbox
cd ~/sentry-autofix-sandbox
git init -q -b main && git add . && git commit -qm "Sentry autofix sandbox test"
gh repo create sentry-autofix-sandbox --private --source . --push
```

Then set `AUTOFIX_PROJECTS=<sentry-project>=<your-user>/sentry-autofix-sandbox`
in sentry-autofix's `.env`. To start over, delete the repo and run these steps
again.

## The planted bug

`billing/amounts.py` -> `parse_amount("1,234.50")` raises
`ValueError: could not convert string to float` because thousands separators
aren't stripped. The existing tests pass because none of them use a comma.

A good fix strips the commas, and adds a test such as
`parse_amount("1,234.50") == 1234.5` that fails before the fix and passes after.

## Running the tests

```bash
python3 -m unittest discover tests
```

## Filing the test issue

From your test-target repo, after the labels exist (a `python3 -m autofix sync`
run in sentry-autofix creates them, or create them by hand):

```bash
error="ValueError: could not convert string to float: '1,234.50'"
gh issue create \
  --title "[BILLING-1A] $error" \
  --body-file docs/sample-issue.md \
  --label sentry --label autofix:queued
```

To reset after a test: close the PR, delete its `claude/...` branch, and close
the issue.
