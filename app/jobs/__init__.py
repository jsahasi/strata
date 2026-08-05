"""Periodic work: the clock this product did not have.

Everything lives in app/jobs/runner.py, which carries the reasoning. This file
only names the public surface, so that a caller writes `from app.jobs import
JobRunner` and a job somebody else builds writes `from app.jobs import Job,
register` -- and neither has to know which module they came out of.

Three things a reader should get from here without opening the runner:

  * NOTHING RUNS UNLESS STRATA_JOBS_ENABLED IS "true". No import of this package
    starts a thread, opens a database or takes a lock.
  * Retention is a DRY RUN unless STRATA_JOBS_RETENTION_DELETE is "true", and the
    mode is printed at every start whichever way it lands.
  * The source fetch is not built. STRATA_JOBS=sources is refused with a sentence
    saying so, because a scheduler that appears to fetch and does not is worse
    than one that says it cannot.
"""

from app.jobs.runner import (
    ACTION_JOB_FAILED,
    ACTION_JOB_RAN,
    ACTION_JOB_RECOVERED,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_JOB_NAMES,
    ENV_COMPANIES,
    ENV_ENABLED,
    ENV_INTERVAL,
    ENV_JOBS,
    ENV_RETENTION_DELETE,
    JOB_RETENTION,
    JOB_SOURCES,
    JOB_WORKFLOW,
    JOBS_ACTOR,
    KNOWN_JOB_NAMES,
    LOG_PREFIX,
    RETENTION_EVERY_SECONDS,
    SUBJECT_JOB,
    WORKFLOW_EVERY_SECONDS,
    Job,
    JobConfigError,
    JobLoop,
    JobOutcome,
    JobRunner,
    LockHeld,
    RunRecord,
    Settings,
    SweepResult,
    boot_lines,
    build_jobs,
    company_roster,
    exclusive,
    lock_path_for,
    loop_from_env,
    register,
    registered,
    settings_from_env,
)

__all__ = [
    "ACTION_JOB_FAILED",
    "ACTION_JOB_RAN",
    "ACTION_JOB_RECOVERED",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_JOB_NAMES",
    "ENV_COMPANIES",
    "ENV_ENABLED",
    "ENV_INTERVAL",
    "ENV_JOBS",
    "ENV_RETENTION_DELETE",
    "JOBS_ACTOR",
    "JOB_RETENTION",
    "JOB_SOURCES",
    "JOB_WORKFLOW",
    "KNOWN_JOB_NAMES",
    "LOG_PREFIX",
    "Job",
    "JobConfigError",
    "JobLoop",
    "JobOutcome",
    "JobRunner",
    "LockHeld",
    "RETENTION_EVERY_SECONDS",
    "RunRecord",
    "SUBJECT_JOB",
    "Settings",
    "SweepResult",
    "WORKFLOW_EVERY_SECONDS",
    "boot_lines",
    "build_jobs",
    "company_roster",
    "exclusive",
    "lock_path_for",
    "loop_from_env",
    "register",
    "registered",
    "settings_from_env",
]
