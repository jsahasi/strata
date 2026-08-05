#!/usr/bin/env python3
"""Run the periodic jobs: once for a cron entry, or in a loop for a container.

    python scripts/run_jobs.py --banner    say what is scheduled, run nothing
    python scripts/run_jobs.py --once      one sweep, then exit  (the default)
    python scripts/run_jobs.py --loop      sweep for ever, on the interval

WHY BOTH SHAPES EXIST. The loop is for the demonstration, so that a reviewer can
see a reminder appear without setting up a scheduler. The one-shot is for anybody
who would rather own the scheduling themselves -- a crontab line, a systemd
timer, a hosted job -- because a product that insists on holding its own clock is
a product that cannot be run the way an operations team already runs everything
else. The same lock covers both, so a cron entry and a running container cannot
sweep at the same moment.

THE EXIT CODES, AND THEY ARE THE POINT OF A CRON ENTRY

  0  the sweep ran and every job worked.
  1  the sweep ran and at least one job FAILED. The rest still ran; which failed
     is in the output above and in the audit chain.
  2  the configuration was refused and NOTHING RAN. A job name this build does
     not have, a job that is not built, a database with no lock available.
  3  another run held the lock, so this one did nothing. It did not wait.

3 is deliberately not 0. An overlapping run is not a failure, but it is also not
a run, and a cron entry that reports success for a sweep that never happened is
how a scheduled job goes quiet for a month without anybody noticing. A crontab
that expects overlap can allow it: `|| [ $? -eq 3 ]`.

WHAT THIS SCRIPT DOES NOT DECIDE. Nothing. Every setting is read by
app/jobs/runner.py from the environment, and the flags here only choose between
one sweep and many. In particular retention is a DRY RUN unless
STRATA_JOBS_RETENTION_DELETE is exactly "true", and there is no flag here to arm
it: a destructive run must be a deliberate change to the environment a deployment
carries, not a word somebody can add to a command line while debugging.
"""

from __future__ import annotations

import argparse
import pathlib
import signal
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402

load_env()

from app.jobs.runner import (  # noqa: E402
    JobConfigError,
    JobLoop,
    JobRunner,
    boot_lines,
    build_jobs,
    settings_from_env,
)

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_REFUSED = 2
EXIT_LOCK_HELD = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="run one sweep and exit. The default.",
    )
    mode.add_argument(
        "--loop",
        action="store_true",
        help="sweep for ever, on STRATA_JOBS_INTERVAL_SECONDS.",
    )
    mode.add_argument(
        "--banner",
        action="store_true",
        help="print what is scheduled and run nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = settings_from_env()
    for line in boot_lines(settings):
        print(line)

    if args.banner:
        # The banner reports; it does not run. It still exits non-zero on a
        # refusal, because deploy/entrypoint.sh uses that to decide whether to
        # start anything, and a banner that always succeeds would have the
        # entrypoint start a loop that cannot build its jobs.
        return EXIT_REFUSED if settings.refusal else EXIT_OK

    if not settings.enabled:
        # Not a failure. Nothing was asked for and nothing ran, and the banner
        # above has already said so in the words somebody can act on.
        return EXIT_OK

    try:
        jobs = build_jobs(settings)
    except JobConfigError as exc:
        print(f"strata.jobs: REFUSED. {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        runner = JobRunner(jobs, companies=settings.companies or None)
    except JobConfigError as exc:
        print(f"strata.jobs: REFUSED. {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.loop:
        return _forever(runner, settings.interval_seconds)

    result = runner.run_once()
    if not result.ran:
        return EXIT_LOCK_HELD
    return EXIT_JOB_FAILED if result.failures else EXIT_OK


def _forever(runner: JobRunner, interval_seconds: int) -> int:
    """Loop until the container stops us. TERM and INT both end it cleanly.

    Without the handlers, a docker stop kills the process wherever it happens to
    be, which is fine for the database -- an open transaction rolls back -- but
    leaves no line in the log saying the scheduler stopped on purpose. A reader
    of that log cannot then tell a deliberate stop from a crash.
    """
    loop = JobLoop(runner, interval_seconds=interval_seconds)
    finished = threading.Event()

    def _stop(signum, _frame):
        print(f"strata.jobs: signal {signum}. Stopping the loop.")
        finished.set()

    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, _stop)

    loop.start()
    try:
        finished.wait()
    finally:
        loop.stop()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
