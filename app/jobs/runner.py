"""The thing that runs periodic work. One company at a time, one run at a time.

Nothing in this product has ever run on a clock. Three jobs were written and
called by nobody: app/state/workflow.py::advance_overdue, which sends the
reminders and takes the escalations and the bypasses; app/state/retention.py::
purge_all, which deletes rows past their window; and a source fetch that is
still not built at all. Each of them takes a session, a company and a moment,
and each of them was waiting for something to hold the clock. This is that
thing, and it is deliberately small: a registry of jobs, a lock, a loop and a
ledger. There is no queue, no broker and no new dependency (ADR-14).

WHY THE PIECES ARE SHAPED THE WAY THEY ARE
------------------------------------------

**Per company, one transaction each.** A sweep that walks every tenant inside
one transaction is a cross-tenant write waiting to happen. One company's failure
rolls back another company's committed work, and one wrong company_id reaches
rows that are not its own. So the unit of work here is (job, company), each in
its own session, and every company id goes through the same _require_scope that
guards every tenant read in app/state/queries.py. A job's run() takes company_id
KEYWORD-ONLY WITH NO DEFAULT, because a positional tenant argument is one
refactor away from being the wrong one and a default is an unscoped sweep
waiting for somebody to forget.

**One run at a time, and the second caller is told what happened to it.** Two
overlapping sweeps double-remind, and worse, double-bypass -- and a bypass is an
action taken WITHOUT an approval, so two of them is two unapproved actions the
record shows as one. The lock is SQLite's own BEGIN EXCLUSIVE, taken on a small
file BESIDE the database rather than on the database itself: locking the database
would block every write the application makes for the length of a sweep, which
turns a safety measure into an outage. The second caller DOES NOT WAIT. It runs
nothing, returns SweepResult(ran=False) with a refusal in plain words, logs one
line, and scripts/run_jobs.py exits 3. Waiting would queue a sweep behind a sweep
and run it a moment later, which is the same double reminder arriving late.

The lock works between processes AND between threads of one process: SQLite
keeps its own table of open inodes precisely because POSIX advisory locks do not
conflict within a process, so the in-app loop and a cron entry cannot both sweep.
tests/test_jobs.py pins both cases.

**A failing job must not kill the loop, and must not be silent.** A failure is
caught per (job, company). The next company still runs, the next tick still
happens, and the failure is:

  * counted -- consecutive_failures is on every RunRecord, so the run AFTER this
    one can see that the last one failed;
  * logged EVERY time, because a job that has been broken for six hours and said
    so once is a job nobody knows is broken;
  * written to the audit chain ONCE per streak, with a second row when it
    recovers. Once, not once per tick: a job failing every minute would otherwise
    put three hundred and sixty identical rows into an append-only table that
    cannot be pruned.

THE AUDIT ROW CARRIES THE EXCEPTION'S TYPE AND NOT ITS TEXT, and that is a
deliberate loss. record_event stores reason verbatim, the chain cannot be edited,
and an exception from a database driver quotes the statement it failed on --
which is how a customer's row reaches a table no purge can reach. The full text
goes to the container log, which rotates. The log is the place for the message
and the chain is the place for the fact.

**Retention is a dry run unless something explicitly arms it.** Deleting a
customer's rows on a timer nobody configured is the single worst thing this could
do, so the destructive path needs STRATA_JOBS_RETENTION_DELETE set to exactly
"true". Nothing else arms it -- not "1", not "yes", not "on" -- and a value this
cannot read lands DRY and says so by name. The mode is printed at every start,
whichever way it landed, because a switch nobody can see in the log is a switch
nobody checked. purge_all's own _require_flag then refuses anything but a real
bool, so the two guards are independent.

**The source fetch is named and refused.** It is not built. Asking for it raises
with a sentence saying so rather than registering a job that quietly does
nothing, because a scheduler that appears to fetch and does not is exactly the
failure app/state/sources.py spends its docstring refusing to commit. When it
exists it arrives through register() and nothing in this file imports it.

WHAT IS RECORDED, AND WHERE IT IS NOT
-------------------------------------
Every run of every job for every company produces a RunRecord -- what ran, for
which company, what changed, how long it took -- and every RunRecord is logged
and kept in an in-memory ledger. THE LEDGER DIES WITH THE PROCESS. There is no
table for it: adding one would mean editing app/state/models.py, which another
agent owns this build, and a scheduler that invents a table nobody else knows
about is the migration nobody expects. So the durable record is the audit chain,
which gets a row when a job CHANGED something, when it starts failing and when it
recovers -- and a tick that changed nothing survives only in the container log.
That is a real limit and it is written here rather than discovered later.

A job that changed something writes ONE row saying so. It does not restate what
changed: advance_overdue already writes a row per reminder, escalation and
bypass, and purge_all writes one per rule it enforced. This row adds the part
those cannot say -- that a clock did it, at this moment, taking this long.

WHAT THIS DOES NOT DO
---------------------
No catch-up. A tick missed while the process was down is a tick that did not
happen; nothing replays it. The jobs are all written so that the next tick sees
the same overdue work, so the effect is a late reminder rather than a lost one --
but retention measured on a window that moved is not the same as retention that
ran, and nothing here pretends otherwise.

No back-off. A job failing every tick is retried every tick. At one minute a
tick against a local SQLite file that costs nothing; against a remote service it
would need one, and it is not built.

No supervision. If the process holding the loop dies, nothing restarts it, and
the only sign is that the log lines stop. deploy/entrypoint.sh prints the pid at
boot so a reader can check it is still there.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.state import db as state_db
from app.state.audit import ACTOR_SYSTEM, record_event
from app.state.models import (
    AuditEvent,
    ChatSession,
    Feedback,
    Invitation,
    LoginSession,
    ShareLink,
    WorkflowRun,
)
from app.state.queries import _require_scope
from app.state.retention import purge_all
from app.state.workflow import advance_overdue

# ---------------------------------------------------------------------------
# The vocabulary
#
# THESE THREE BELONG IN app/state/audit.py BESIDE THE REST OF THE ACTION CODES
# AND MUST BE MOVED THERE RATHER THAN RESTATED. They are here for the reason
# app/state/retention.py gives for its own two: that file is owned by another
# agent in this build and a parallel edit would lose their work. When they move,
# this block is deleted and the import changes; nothing else does. It is in the
# handoff.
#
# THEY ARE THREE CODES AND NOT ONE. job.ran means a job did something. job.failed
# means it could not, and nothing it would have done was done. job.recovered
# means it works again and names how many attempts did not. Reading the second as
# the first would report work that never happened, which is the reading the
# ACTION_ constants exist to prevent.
# ---------------------------------------------------------------------------

ACTION_JOB_RAN = "job.ran"
ACTION_JOB_FAILED = "job.failed"
ACTION_JOB_RECOVERED = "job.recovered"

SUBJECT_JOB = "job"

# The actor on every row this module writes. A machine acted, so actor_kind stays
# ACTOR_SYSTEM and actor_user_id stays absent -- record_event refuses a user id on
# a system actor, and naming a person for work nobody asked for would be a false
# attribution in the one table that exists to be believed.
JOBS_ACTOR = "system:jobs"

# Every line this module prints starts with this, so a reviewer can find the
# scheduler in a container log with one grep and see whether anything is running.
LOG_PREFIX = "strata.jobs:"


# ---------------------------------------------------------------------------
# The jobs this build knows by name
# ---------------------------------------------------------------------------

JOB_WORKFLOW = "workflow"
JOB_RETENTION = "retention"
JOB_SOURCES = "sources"

#: Every name STRATA_JOBS will accept. A name outside this tuple is refused
#: rather than skipped: an operator who asked for something got nothing, and a
#: scheduler that silently drops a misspelt job is a scheduler nobody can trust
#: the enabled list of.
KNOWN_JOB_NAMES = (JOB_WORKFLOW, JOB_RETENTION, JOB_SOURCES)

# One minute for the approval clock. Deadlines in this product are set in hours,
# so a minute is far finer than anything depends on -- and finer on purpose: a
# reviewer watching the demo should not wait an hour to see a reminder appear.
WORKFLOW_EVERY_SECONDS = 60

# Once a day for retention. The windows are 90 and 365 days, so a run an hour
# would delete the same rows a day earlier at best and hammer the database for
# nothing. Daily is also the cadence the privacy page describes, and the two
# should not disagree.
RETENTION_EVERY_SECONDS = 24 * 60 * 60

DEFAULT_INTERVAL_SECONDS = 60
#: What runs when the loop is switched on and nothing says which jobs. The clock
#: only. Retention is destructive even in dry mode -- it reads every tenant's
#: personal-data tables -- so it is opt in, never a default.
DEFAULT_JOB_NAMES = (JOB_WORKFLOW,)


# ---------------------------------------------------------------------------
# The settings, and the names they are read from
# ---------------------------------------------------------------------------

ENV_ENABLED = "STRATA_JOBS_ENABLED"
ENV_JOBS = "STRATA_JOBS"
ENV_INTERVAL = "STRATA_JOBS_INTERVAL_SECONDS"
ENV_RETENTION_DELETE = "STRATA_JOBS_RETENTION_DELETE"
ENV_COMPANIES = "STRATA_JOBS_COMPANIES"

#: The one spelling that means yes. Anything else means no, and says so.
TRUE_WORD = "true"


class JobConfigError(Exception):
    """The configuration asks for something this build cannot do.

    Raised rather than worked around. Every case it covers -- a job name nobody
    recognises, a job that is not built, a database with no lock available -- is
    a case where carrying on would run less work than the operator asked for
    while looking exactly like success.
    """


class LockHeld(RuntimeError):
    """Another run holds the sweep lock. This one did not wait and did not run."""


# ---------------------------------------------------------------------------
# What a job is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobOutcome:
    """What one job did for one company.

    changed is the question the audit chain asks: did anything move. It is
    separate from the summary because a summary is prose and a caller must never
    have to parse prose to find out whether rows changed.

    summary is one line of plain words and is never empty. "nothing to do" is an
    answer; a blank is a job that did not say.
    """

    changed: bool
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.changed, bool):
            raise ValueError(
                f"changed must be True or False; got {self.changed!r}. A merely "
                "truthy value decides whether the chain records work as done."
            )
        if not self.summary:
            raise ValueError("a job that reports nothing has not reported")


#: What a job's callable looks like. company_id and now are keyword-only, so a
#: job cannot be handed a tenant by position and cannot read the clock itself.
JobCallable = Callable[..., JobOutcome]


@dataclass(frozen=True)
class Job:
    """One piece of periodic work, with its own cadence.

    every_seconds is the job's own clock, not the loop's. The loop ticks at one
    interval and each job runs when its own has passed, so retention at a day and
    the approval clock at a minute live in the same loop without one of them
    setting the pace for the other.

    destructive is a fact about the job, printed at boot beside its name. It is
    not a switch -- what arms retention is its own setting -- it is so that a
    reader of the boot log can see which of the enabled jobs can delete a row.
    """

    name: str
    every_seconds: float
    run: JobCallable
    description: str
    destructive: bool = False
    #: The mode line printed at boot, where a job has one. Retention uses it to
    #: say DRY RUN or DELETES ROWS on every single start.
    mode: str = ""


# ---------------------------------------------------------------------------
# The seam for a job this file does not know about
#
# NOTHING HERE IMPORTS THE SOURCE FETCH. It is being written by somebody else in
# this build and does not exist yet; guessing at its module and function name
# would be an import that breaks the day they choose a different one. So the
# arrangement is the other way round: whatever builds it calls register() with a
# Job, and STRATA_JOBS=sources then finds it. Until then the name is KNOWN and
# REFUSED, which is the honest answer -- see _SOURCES_NOT_BUILT.
# ---------------------------------------------------------------------------

_REGISTERED: dict[str, Job] = {}


def register(job: Job) -> None:
    """Add a job the runner did not ship with. Refuses to replace one silently."""
    if not isinstance(job, Job):
        raise JobConfigError(f"register() takes a Job; got {type(job).__name__}")
    if job.name in _REGISTERED:
        raise JobConfigError(
            f"a job called {job.name!r} is already registered. Replacing one "
            "quietly would mean the boot log names a job and a different one runs."
        )
    _REGISTERED[job.name] = job


def registered() -> Mapping[str, Job]:
    """A read-only view of what has registered itself."""
    return dict(_REGISTERED)


_SOURCES_NOT_BUILT = (
    f"{JOB_SOURCES!r} is not built in this repository. Nothing in this product "
    "fetches anything: there is no crawler and no poller, ScheduledRun is a table "
    "of cadences with nothing behind it, and app/state/sources.py refuses to imply "
    "otherwise. This is a refusal rather than a job that does nothing, because a "
    "scheduler that appears to fetch and does not is worse than one that says it "
    "cannot. When the fetch exists it calls app.jobs.register() with a Job and this "
    "name resolves."
)


# ---------------------------------------------------------------------------
# The two jobs that do exist
# ---------------------------------------------------------------------------


def _workflow_job() -> Job:
    """The approval clock: reminders, escalations and bypasses, per company.

    advance_overdue is idempotent by three separate mechanisms -- a counted
    reminder, a closed row, and the audit chain asked before writing -- so a tick
    that runs twice at the same moment is safe. The lock is still taken, because
    "safe when it happens" and "cannot happen" are different guarantees and the
    bypass path is where the difference would land.

    A stall is NOT counted as a change. Nothing moved: the step is still open and
    still assigned, and advance_overdue has already written its own row saying so
    once. Counting it would put a job.ran row in the chain every minute for a
    route somebody has to fix by hand.
    """

    def run(session: Session, *, company_id: str, now: datetime) -> JobOutcome:
        report = advance_overdue(session, company_id, now=now)
        changed = bool(
            report.reminders_sent
            or report.escalated_step_run_ids
            or report.bypassed_step_run_ids
            or report.completed_run_ids
        )
        summary = (
            f"{report.checked} running approvals checked; "
            f"{report.reminders_sent} reminded; "
            f"{len(report.escalated_step_run_ids)} escalated; "
            f"{len(report.bypassed_step_run_ids)} bypassed WITHOUT an approval; "
            f"{len(report.completed_run_ids)} runs completed; "
            f"{len(report.stalled_step_run_ids)} stalled and needing a person; "
            f"{len(report.unrouted_step_run_ids)} on nobody's desk"
        )
        return JobOutcome(changed=changed, summary=summary)

    return Job(
        name=JOB_WORKFLOW,
        every_seconds=WORKFLOW_EVERY_SECONDS,
        run=run,
        description="the approval clock: reminders, escalations and bypasses",
        destructive=False,
    )


def _retention_job(*, delete: bool) -> Job:
    """The retention schedule, dry unless something armed it.

    delete is checked here for being a real bool and again inside purge_all,
    which refuses anything merely truthy. Two guards, independently written,
    because dry_run=0 is falsey and would arm the destructive path by accident --
    and by the time anybody noticed, the rows would be gone.

    A dry run never reports changed=True however many rows it matched, so the
    chain never carries a row saying a purge ran when nothing was deleted. What
    it WOULD have removed goes in the summary and therefore into the log.
    """
    if not isinstance(delete, bool):
        raise JobConfigError(
            f"the retention job needs True or False; got {delete!r}. A value that "
            "is merely truthy decides whether a customer's rows are destroyed."
        )

    def run(session: Session, *, company_id: str, now: datetime) -> JobOutcome:
        reports = purge_all(
            session, company_id=company_id, now=now, dry_run=not delete
        )
        matched = sum(report.matched for report in reports)
        removed = sum(report.removed for report in reports)
        lines = "; ".join(report.line() for report in reports if report.matched)
        if delete:
            summary = f"removed {removed} rows under {len(reports)} rules"
        else:
            summary = (
                f"DRY RUN: would remove {matched} rows under {len(reports)} rules; "
                "nothing was deleted"
            )
        if lines:
            summary = f"{summary}. {lines}"
        return JobOutcome(changed=bool(removed), summary=summary)

    mode = (
        f"DELETES ROWS ({ENV_RETENTION_DELETE}={TRUE_WORD})"
        if delete
        else f"DRY RUN: nothing will be deleted (set {ENV_RETENTION_DELETE}={TRUE_WORD} to arm it)"
    )
    return Job(
        name=JOB_RETENTION,
        every_seconds=RETENTION_EVERY_SECONDS,
        run=run,
        description="the retention schedule in app/state/retention.py",
        destructive=True,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """What the environment asked for, and everything it could not be read as.

    notes carries every value this could not read, in the words a person would
    need to fix it. They are printed at boot: a setting that fell back quietly is
    a setting somebody believes is on (best-practices.html section 26).

    refusal is set where the configuration names a job that does not exist. It is
    a field rather than an exception because settings_from_env is also what the
    boot banner reads, and a banner that raises prints nothing at all -- which is
    the one moment a reader most needs the reason.
    """

    enabled: bool
    job_names: tuple[str, ...]
    interval_seconds: int
    retention_delete: bool
    companies: tuple[str, ...]
    notes: tuple[str, ...] = ()
    refusal: str = ""


def _is_true(value: str) -> bool:
    return value.strip().lower() == TRUE_WORD


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def settings_from_env(environ: Mapping[str, str] | None = None) -> Settings:
    """Read the five settings. Never raises; every problem comes back as words."""
    source = os.environ if environ is None else environ
    notes: list[str] = []

    raw_enabled = source.get(ENV_ENABLED, "")
    enabled = _is_true(raw_enabled)
    if raw_enabled.strip() and not enabled:
        notes.append(
            f'{ENV_ENABLED} is "{raw_enabled}", which is not "{TRUE_WORD}". '
            "Nothing is scheduled."
        )

    raw_jobs = source.get(ENV_JOBS, "")
    if not enabled:
        job_names: tuple[str, ...] = ()
    elif raw_jobs.strip():
        job_names = _split(raw_jobs)
    else:
        job_names = DEFAULT_JOB_NAMES

    refusal = ""
    unknown = [name for name in job_names if name not in KNOWN_JOB_NAMES]
    if unknown:
        refusal = (
            f"{ENV_JOBS} names {', '.join(repr(name) for name in unknown)}, which "
            f"this build does not have. The names it has are "
            f"{', '.join(KNOWN_JOB_NAMES)}. Nothing is scheduled: running the rest "
            "would mean the operator asked for work that silently never happens."
        )

    raw_interval = source.get(ENV_INTERVAL, "")
    interval = DEFAULT_INTERVAL_SECONDS
    if raw_interval.strip():
        try:
            candidate = int(raw_interval)
        except ValueError:
            candidate = 0
        if candidate >= 1:
            interval = candidate
        else:
            notes.append(
                f'{ENV_INTERVAL} is "{raw_interval}", which is not a number of '
                f"seconds this can tick on. Using {DEFAULT_INTERVAL_SECONDS}."
            )

    raw_delete = source.get(ENV_RETENTION_DELETE, "")
    retention_delete = _is_true(raw_delete)
    if raw_delete.strip() and not retention_delete:
        notes.append(
            f'{ENV_RETENTION_DELETE} is "{raw_delete}", which this does not read '
            f'as an instruction to delete -- only the word "{TRUE_WORD}" does. '
            "Retention is a DRY RUN and no row will be deleted."
        )

    return Settings(
        enabled=enabled,
        job_names=job_names,
        interval_seconds=interval,
        retention_delete=retention_delete,
        companies=_split(source.get(ENV_COMPANIES, "")),
        notes=tuple(notes),
        refusal=refusal,
    )


def build_jobs(
    settings: Settings, *, extra: Mapping[str, Job] | None = None
) -> tuple[Job, ...]:
    """Turn the enabled names into jobs, or refuse and say which name is wrong."""
    if settings.refusal:
        raise JobConfigError(settings.refusal)

    registry = dict(_REGISTERED if extra is None else extra)
    jobs: list[Job] = []
    for name in settings.job_names:
        if name in registry:
            jobs.append(registry[name])
        elif name == JOB_WORKFLOW:
            jobs.append(_workflow_job())
        elif name == JOB_RETENTION:
            jobs.append(_retention_job(delete=settings.retention_delete))
        elif name == JOB_SOURCES:
            raise JobConfigError(_SOURCES_NOT_BUILT)
        else:  # pragma: no cover - settings.refusal catches this first
            raise JobConfigError(f"no job called {name!r}")
    return tuple(jobs)


def boot_lines(
    settings: Settings, *, extra: Mapping[str, Job] | None = None
) -> tuple[str, ...]:
    """What the container log says at boot about what is scheduled.

    This is the sentence the live site currently gets to make -- that there is no
    scheduler in this product -- and the reason it is worth printing even when
    nothing is enabled. A reviewer reading the log must be able to tell "no job is
    scheduled" from "a job is scheduled and has not fired yet", and a blank log
    reads as the second.
    """
    # A REFUSED CONFIGURATION IS OFF, and the first line has to say so. An
    # earlier version opened with "periodic work is ON" and put the refusal
    # underneath, which is the shape of log a reader skims and comes away
    # believing a scheduler is running. The state of the world goes first.
    running = settings.enabled and not settings.refusal

    if settings.refusal:
        lines = [
            f"{LOG_PREFIX} periodic work is OFF: the configuration was REFUSED, "
            "so nothing at all is scheduled.",
            f"{LOG_PREFIX} REFUSED. {settings.refusal}",
        ]
    elif settings.enabled:
        lines = [
            f"{LOG_PREFIX} periodic work is ON.",
            f"{LOG_PREFIX} the loop ticks every {settings.interval_seconds}s; "
            "each job runs on its own cadence.",
        ]
    else:
        lines = [
            f"{LOG_PREFIX} periodic work is OFF.",
            f"{LOG_PREFIX} nothing is scheduled. {ENV_ENABLED} is not "
            f'"{TRUE_WORD}", so no reminder, no escalation, no bypass and no '
            "purge will run on a clock in this process.",
        ]

    # Under a refusal nothing is enabled, whatever the names said, so every job
    # line has to read that way rather than naming the ones spelled correctly.
    named = settings if running else replace(settings, job_names=())

    registry = dict(_REGISTERED if extra is None else extra)
    for name in KNOWN_JOB_NAMES:
        lines.append(_job_line(name, named, registry))

    if running:
        roster = ", ".join(settings.companies) if settings.companies else (
            f"every company found in the database ({ENV_COMPANIES} is not set)"
        )
        lines.append(f"{LOG_PREFIX} companies: {roster}")

    for note in settings.notes:
        lines.append(f"{LOG_PREFIX} NOTE {note}")
    return tuple(lines)


def _job_line(name: str, settings: Settings, registry: Mapping[str, Job]) -> str:
    """One line per known job, on or off. Never absent, because absence reads as on."""
    if name not in settings.job_names:
        if name == JOB_SOURCES and name not in registry:
            # Named even though nobody asked for it. "not enabled" would read as
            # a job somebody could switch on, and this one cannot be.
            return (
                f"{LOG_PREFIX} {JOB_SOURCES}: NOT BUILT. Nothing in this product "
                "fetches anything, on a clock or otherwise."
            )
        return f"{LOG_PREFIX} {name}: not enabled."

    try:
        job = build_jobs(
            Settings(
                enabled=True,
                job_names=(name,),
                interval_seconds=settings.interval_seconds,
                retention_delete=settings.retention_delete,
                companies=settings.companies,
            ),
            extra=registry,
        )[0]
    except JobConfigError as exc:
        return f"{LOG_PREFIX} {name}: REFUSED. {exc}"

    line = f"{LOG_PREFIX} {name}: ENABLED, every {job.every_seconds}s -- {job.description}"
    if job.mode:
        line = f"{line} -- {job.mode}"
    elif job.destructive:  # pragma: no cover - every destructive job states a mode
        line = f"{line} -- CAN DELETE ROWS"
    return line


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def lock_path_for(database_url: str) -> Path:
    """Where the sweep lock lives: beside the database, never inside it.

    Taking the lock on the database itself would hold a write lock across a whole
    sweep, which on SQLite blocks every write the application makes -- a safety
    measure that causes the outage it was meant to prevent. So it is its own tiny
    file, and BEGIN EXCLUSIVE on that file is what two sweeps contend for.

    A database that is not SQLite is REFUSED rather than run without a lock. The
    URL is the seam to Postgres (app/state/db.py) and the day somebody crosses it
    this must become an advisory lock, not a file: two application servers do not
    share a filesystem, and a lock they cannot both see is no lock at all. Running
    unlocked would look identical right up to the first double bypass.
    """
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise JobConfigError(
            "the run lock is a SQLite file beside the database, and this build's "
            "database URL is not SQLite. Refusing to sweep without a lock: two "
            "overlapping sweeps double-remind and double-bypass. On a shared "
            "database this needs an advisory lock the servers can both see."
        )
    rest = database_url[len(prefix) :]
    if not rest or rest.startswith(":memory:"):
        raise JobConfigError(
            "the database is in memory, so there is nothing for a second process "
            "to contend on and nowhere to put a lock file. Refusing to sweep."
        )
    return Path(rest + ".jobs.lock")


@contextmanager
def exclusive(path: Path | str) -> Iterator[None]:
    """Hold the sweep lock, or raise LockHeld at once. Never waits.

    timeout=0 is the whole point: SQLite would otherwise sit on a busy handler
    and hand the lock over when the first sweep finished, which runs the second
    sweep a moment later rather than not at all.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise LockHeld(
                f"another run holds the job lock at {path}. This run did nothing "
                "and did not wait: waiting would run the same sweep a moment "
                "later, which is the same double reminder arriving late."
            ) from exc
        try:
            yield
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:  # pragma: no cover - only on a torn connection
                pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# What a run produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """One job, one company, one tick. The unit everything else is counted in."""

    job: str
    company_id: str
    started_at: datetime
    seconds: float
    changed: bool
    summary: str
    error: str = ""
    #: How many times in a row this (job, company) has failed, this one included.
    #: 0 on a run that worked. It is on the record rather than only in a counter
    #: so that the run AFTER a failure can see there was one.
    consecutive_failures: int = 0

    def line(self) -> str:
        """One line for the container log. Failures say so in capitals."""
        if self.error:
            return (
                f"{LOG_PREFIX} {self.job} {self.company_id} FAILED after "
                f"{self.seconds:.3f}s (attempt {self.consecutive_failures} in a "
                f"row) -- {self.error}"
            )
        return (
            f"{LOG_PREFIX} {self.job} {self.company_id} {self.seconds:.3f}s -- "
            f"{self.summary}"
        )


@dataclass(frozen=True)
class SweepResult:
    """What one call to run_once did, including the case where it did nothing."""

    started_at: datetime
    seconds: float
    ran: bool
    refusal: str
    records: tuple[RunRecord, ...]

    @property
    def failures(self) -> tuple[RunRecord, ...]:
        return tuple(record for record in self.records if record.error)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

#: The default for lock_path: work it out from the database URL. A sentinel
#: rather than None, because None has to mean something different and dangerous
#: (no lock at all) and the two must not be spelled the same way.
DERIVE_LOCK = "derive-the-lock-path-from-the-database-url"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: The tables the roster is read from: every table the two built jobs touch, plus
#: the audit log, which every tenant that has ever done anything has a row in.
#: share_opens is reached through share_links, which is where its tenancy lives.
_ROSTER_TABLES = (
    AuditEvent,
    WorkflowRun,
    LoginSession,
    ChatSession,
    Feedback,
    Invitation,
    ShareLink,
)


def company_roster(session: Session) -> tuple[str, ...]:
    """Every company id these jobs could have work for, in order.

    THIS BELONGS IN app/state/queries.py beside versions_for_company, and it is
    here for the reason app/state/sources.py gives for its own scoped reads: that
    file is owned by another agent this build and a parallel edit would lose their
    work. It is in the handoff.

    IT READS ONE COLUMN AND NO CONTENT. company_id and nothing else, from the
    tables the jobs act on -- so no statement, no filing, no address and no name
    crosses a tenant boundary here, and every read that returns anything goes
    through a per-company call with _require_scope in front of it. It is the same
    census scripts/backup.py takes to verify one chain per company, for the same
    reason: there is no companies table in this schema to ask instead.

    A tenant with no row in any of these tables is invisible to this, and that is
    correct rather than a gap: a tenant with no audit row, no approval run, no
    session, no conversation, no complaint, no invitation and no share link has
    nothing for either job to do.
    """
    found: set[str] = set()
    for model in _ROSTER_TABLES:
        found.update(
            value
            for value in session.execute(select(model.company_id).distinct())
            .scalars()
            .all()
            if value
        )
    return tuple(sorted(found))


class JobRunner:
    """Runs the enabled jobs, one company at a time, under one lock.

    It holds no session. Each (job, company) opens its own through the factory
    and commits or rolls back on its own, so one tenant's failure cannot undo
    another tenant's work and one tenant's slow job cannot hold a transaction
    open across the rest.
    """

    def __init__(
        self,
        jobs: Sequence[Job],
        *,
        companies: Sequence[str] | None = None,
        lock_path: Path | str | None = DERIVE_LOCK,
        log: Callable[[str], None] = print,
        session_factory: Callable[[], object] = state_db.session_scope,
        ledger_size: int = 500,
    ) -> None:
        self._jobs = tuple(jobs)
        self._log = log
        self._sessions = session_factory
        self._ledger: deque[RunRecord] = deque(maxlen=ledger_size)
        self._failures: dict[tuple[str, str], int] = {}
        self._last_run: dict[str, datetime] = {}

        if companies is None:
            self._companies: tuple[str, ...] | None = None
        else:
            # Checked here rather than at three in the morning. A bad id in a
            # setting is a configuration error, and a sweep is the wrong place to
            # discover one.
            self._companies = tuple(_require_scope(company) for company in companies)

        if lock_path is DERIVE_LOCK:
            self._lock_path: Path | None = lock_path_for(state_db.DATABASE_URL)
        elif lock_path is None:
            self._lock_path = None
            self._log(
                f"{LOG_PREFIX} NO LOCK. This runner was built without one, so "
                "nothing stops two sweeps overlapping and double-reminding. Only "
                "a caller that has its own guarantee of one run at a time may do "
                "this."
            )
        else:
            self._lock_path = Path(lock_path)

    # -- reading what happened -------------------------------------------------

    def ledger(self) -> tuple[RunRecord, ...]:
        """The last few hundred runs, newest last. In memory, and only in memory."""
        return tuple(self._ledger)

    def failing(self) -> Mapping[tuple[str, str], int]:
        """Every (job, company) failing now, and for how many runs in a row."""
        return dict(self._failures)

    # -- doing the work --------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> SweepResult:
        """One sweep. Takes the lock, or returns having done nothing and says so."""
        started = now or _utcnow()
        if started.tzinfo is None:
            raise ValueError(
                "now must carry a timezone; a naive moment cannot be compared "
                "with the stored deadlines and would pick the wrong cutoff"
            )
        clock = time.monotonic()

        if self._lock_path is None:
            return self._sweep(started, clock)
        try:
            with exclusive(self._lock_path):
                return self._sweep(started, clock)
        except LockHeld as exc:
            self._log(f"{LOG_PREFIX} {exc}")
            return SweepResult(
                started_at=started,
                seconds=time.monotonic() - clock,
                ran=False,
                refusal=str(exc),
                records=(),
            )

    def _sweep(self, started: datetime, clock: float) -> SweepResult:
        due = [job for job in self._jobs if self._is_due(job, started)]
        records: list[RunRecord] = []
        if due:
            companies = self._roster()
            for job in due:
                self._last_run[job.name] = started
                for company_id in companies:
                    records.append(self._run_one(job, company_id, started))
        return SweepResult(
            started_at=started,
            seconds=time.monotonic() - clock,
            ran=True,
            refusal="",
            records=tuple(records),
        )

    def _is_due(self, job: Job, started: datetime) -> bool:
        """A job runs when its own interval has passed since it last ran HERE.

        The memory is this object's. A fresh process -- which is every run from a
        cron entry -- has none, so every job is due at once and the cron entry
        owns the cadence. That is the right division: an operator who wrote the
        crontab line does not want this second-guessing it.
        """
        last = self._last_run.get(job.name)
        if last is None:
            return True
        return (started - last).total_seconds() >= job.every_seconds

    def _roster(self) -> tuple[str, ...]:
        if self._companies is not None:
            return self._companies
        with self._sessions() as session:
            return company_roster(session)

    def _run_one(self, job: Job, company_id: str, started: datetime) -> RunRecord:
        """One job, one company, one transaction. No job failure escapes it.

        THE SCOPE CHECK IS OUTSIDE THE CATCH, on purpose and alone. Everything
        the job does is caught and recorded as that job failing; an unscoped
        company id is not the job failing, it is this runner about to sweep
        without a tenant, and filing that as "the workflow job had a bad day"
        would hide the one defect that matters here. It goes up, and the loop
        logs it as the sweep itself breaking.
        """
        _require_scope(company_id)
        key = (job.name, company_id)
        clock = time.monotonic()
        try:
            with self._sessions() as session:
                outcome = job.run(session, company_id=company_id, now=started)
                if not isinstance(outcome, JobOutcome):
                    raise TypeError(
                        f"{job.name} returned {type(outcome).__name__} rather than "
                        "a JobOutcome, so nothing here can say what it did"
                    )
                if outcome.changed:
                    record_event(
                        session,
                        company_id=company_id,
                        actor=JOBS_ACTOR,
                        action=ACTION_JOB_RAN,
                        subject_type=SUBJECT_JOB,
                        subject_id=job.name,
                        reason=(
                            f"the {job.name} job ran for this company on the clock "
                            f"and changed something: {outcome.summary}"
                        ),
                        occurred_at=started,
                        actor_kind=ACTOR_SYSTEM,
                    )
        except Exception as exc:
            streak = self._failures.get(key, 0) + 1
            self._failures[key] = streak
            record = RunRecord(
                job=job.name,
                company_id=company_id,
                started_at=started,
                seconds=time.monotonic() - clock,
                changed=False,
                summary="",
                error=f"{type(exc).__name__}: {exc}",
                consecutive_failures=streak,
            )
            self._remember(record)
            if streak == 1:
                self._append(
                    company_id,
                    action=ACTION_JOB_FAILED,
                    job=job,
                    moment=started,
                    reason=(
                        f"the {job.name} job failed for this company on the clock "
                        f"and nothing it would have done was done. The failure was "
                        f"a {type(exc).__name__}; its text is in the container log "
                        "and deliberately not here, because an exception can quote "
                        "a row and this table cannot be redacted. Other companies "
                        "carried on. It is retried every tick, and every attempt "
                        "is logged."
                    ),
                )
            return record

        was_failing = self._failures.pop(key, 0)
        if was_failing:
            self._append(
                company_id,
                action=ACTION_JOB_RECOVERED,
                job=job,
                moment=started,
                reason=(
                    f"the {job.name} job worked again for this company after "
                    f"{was_failing} failed attempts in a row. Whatever those "
                    "attempts would have done was not done at the time; this run "
                    "did the work that was still outstanding."
                ),
            )
        record = RunRecord(
            job=job.name,
            company_id=company_id,
            started_at=started,
            seconds=time.monotonic() - clock,
            changed=outcome.changed,
            summary=outcome.summary,
        )
        self._remember(record)
        return record

    def _remember(self, record: RunRecord) -> None:
        self._ledger.append(record)
        self._log(record.line())

    def _append(
        self, company_id: str, *, action: str, job: Job, moment: datetime, reason: str
    ) -> None:
        """Write one row about the job itself, in a session of its own.

        Its own session because the one that failed has been rolled back, and a
        row appended to a dead transaction is a record of a failure nobody can
        read. And it never raises: a scheduler that dies while recording that
        something died has taken the loop down for the sake of a log line.
        """
        try:
            with self._sessions() as session:
                record_event(
                    session,
                    company_id=company_id,
                    actor=JOBS_ACTOR,
                    action=action,
                    subject_type=SUBJECT_JOB,
                    subject_id=job.name,
                    reason=reason,
                    occurred_at=moment,
                    actor_kind=ACTOR_SYSTEM,
                )
        except Exception as exc:  # pragma: no cover - needs a broken database
            self._log(
                f"{LOG_PREFIX} could not record {action} for {job.name} "
                f"{company_id}: {type(exc).__name__}: {exc}. The run itself is in "
                "this log and nowhere else."
            )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class JobLoop:
    """A thread that calls run_once on an interval. The demo's clock.

    A DAEMON THREAD, on purpose. A sweep in flight when the process is asked to
    stop is cut off, and what it was inside rolls back -- SQLite gives that for
    free, and a half-applied sweep is exactly what the per-company transaction
    boundary exists to make safe. The alternative, a non-daemon thread, holds a
    container open for up to a whole interval on every deploy.

    NOTHING RESTARTS IT. If the process dies, the loop dies with it and the only
    sign is that the log lines stop.
    """

    def __init__(
        self,
        runner: JobRunner,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        log: Callable[[str], None] = print,
    ) -> None:
        if interval_seconds <= 0:
            raise JobConfigError(
                f"the loop needs an interval greater than zero; got "
                f"{interval_seconds!r}. Zero is a spin, not a schedule."
            )
        self._runner = runner
        self._interval = interval_seconds
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            raise JobConfigError("this loop is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="strata-jobs", daemon=True
        )
        self._thread.start()
        self._log(
            f"{LOG_PREFIX} loop started, ticking every {self._interval}s. The "
            "first sweep runs now."
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Ask it to stop and wait a little. Stopping a loop that never ran is fine."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():  # pragma: no cover - needs a sweep longer than timeout
            self._log(
                f"{LOG_PREFIX} the loop did not stop within {timeout}s. It is a "
                "daemon thread and will end with the process; whatever it is "
                "inside will roll back."
            )
        else:
            self._thread = None
            self._log(f"{LOG_PREFIX} loop stopped.")

    def _run(self) -> None:
        """Sweep, then wait. Nothing raised in here may end the loop."""
        while True:
            try:
                self._runner.run_once()
            except Exception as exc:
                # The runner catches its own job failures, so reaching this means
                # the sweep itself broke -- the lock, the roster read, the
                # database. Still not a reason to stop: the next tick may find it
                # fixed, and a loop that exits leaves nothing running and nothing
                # saying so.
                self._log(
                    f"{LOG_PREFIX} THE SWEEP ITSELF FAILED: "
                    f"{type(exc).__name__}: {exc}. The loop is still running and "
                    "will try again."
                )
            if self._stop.wait(self._interval):
                return


def loop_from_env(
    environ: Mapping[str, str] | None = None, *, log: Callable[[str], None] = print
) -> JobLoop | None:
    """Print the banner, and start the loop if the setting asks for it.

    This is the one line app/main.py needs to run the jobs inside the application
    process. Two lines, in fact: this one and something that keeps the returned
    loop alive, because a loop nobody holds is a loop nobody can stop.

    IT NEVER RAISES, AND THAT IS DELIBERATE HERE AND NOWHERE ELSE IN THIS FILE.
    Everything else refuses loudly, because a refusal there means a sweep does
    not run. A refusal HERE would mean the web application does not start, and
    taking a regulated customer's workspace off the air because the scheduler
    could not read its own settings is a worse outcome than having no scheduler.
    So a refusal is logged in full and None comes back.

    None therefore means one of two things -- nothing was asked for, or what was
    asked for was refused -- and the log says which. It is not "running quietly":
    a loop that started says so.
    """
    settings = settings_from_env(environ)
    for line in boot_lines(settings):
        log(line)
    if not settings.enabled:
        return None
    try:
        jobs = build_jobs(settings)
        runner = JobRunner(jobs, companies=settings.companies or None, log=log)
        loop = JobLoop(runner, interval_seconds=settings.interval_seconds, log=log)
        loop.start()
    except JobConfigError as exc:
        log(
            f"{LOG_PREFIX} REFUSED, so NOTHING IS SCHEDULED in this process: "
            f"{exc}"
        )
        return None
    return loop


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
    "Settings",
    "SweepResult",
    "SUBJECT_JOB",
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
