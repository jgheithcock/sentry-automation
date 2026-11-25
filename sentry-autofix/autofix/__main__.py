"""Entry point: python -m autofix {resolve,sync,run-queue,all,doctor} [--dry-run | --full]

sync/run-queue/all hold a lock so overlapping cron runs exit instead of doubling up.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from datetime import date

from . import config, doctor, resolve, run_queue, sync


def main() -> int:
    """Run an autofix command and return its process exit code.

    `resolve` marks Sentry issues resolved (in next release) when their GitHub
    issue is closed as completed.
    `sync` files new Sentry issues as GitHub issues, and reopens ones that
    regressed after being resolved.
    `run-queue` runs Claude Code on each queued issue.
    `all` runs resolve, sync, then run-queue.
    `doctor` checks the setup and returns 1 when a check fails.
    """
    parser = argparse.ArgumentParser(prog="autofix")
    parser.add_argument("command", choices=["resolve", "sync", "run-queue", "all", "doctor"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve/sync/run-queue: read from Sentry/GitHub but change nothing",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="doctor: also test GitHub write access (no-op label + dry-run push) "
        "and make one small Claude call",
    )
    args = parser.parse_args()

    if args.command == "doctor":
        return doctor.run(full=args.full)

    log_dir = config.REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"{date.today()}.log"),
        ],
    )

    lock = open(config.REPO_ROOT / ".autofix.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another run is in progress; exiting")
        return 0

    cfg = config.load()
    if args.command in ("resolve", "all"):
        resolved = resolve.run(cfg, dry_run=args.dry_run)
        logging.info("resolve done: %d Sentry issue(s) resolved", resolved)
    if args.command in ("sync", "all"):
        created = sync.run(cfg, dry_run=args.dry_run)
        logging.info("sync done: %d issue(s) created", created)
    if args.command in ("run-queue", "all"):
        tally = run_queue.run(cfg, dry_run=args.dry_run)
        logging.info("run-queue done: %s", tally or "nothing queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
