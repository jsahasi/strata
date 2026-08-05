"""The thing that runs periodic work, and everything it refuses to do.

Nothing in this product has ever run on a clock. Three jobs were written and
called by nobody: the workflow clock in app/state/workflow.py, the retention
purges in app/state/retention.py, and a source fetch that is still not built.
These tests are written before app/jobs/runner.py exists, because every claim
below is a claim about what the runner MUST NOT do, and a test written after the
code tends to describe the code.

Five claims, and each has its own section.

**Per company, one transaction each.** A sweep that walks every tenant in one
transaction is a cross-tenant write waiting to happen: one company's failure
rolls back another's committed work, and one bad company_id reaches rows that
are not its own. Every job runs against exactly one company id, and that id goes
through the same _require_scope every tenant read goes through.

**One run at a time, and the second caller is told.** Two overlapping sweeps
double-remind and double-bypass. The lock is SQLite's own, on a file beside the
database, and the second caller does not wait: it returns having run nothing and
says why. Waiting would queue a sweep behind a sweep and run it a moment later,
which is the same double-remind arriving late.

**A failing job does not kill the loop and is never silent.** The failure is
recorded, the next company still runs, and the run after this one can still see
that it failed -- because a job that has been broken for six hours and says so
once is a job nobody knows is broken.

**Retention is a dry run unless something explicitly arms it, and says which.**
Deleting a customer's rows on a timer nobody configured is the worst thing this
could do. The flag is a real bool, never a truthy string, and the mode is printed
at every start whichever way it lands.

**The source fetch is named and refused.** It is not built. Asking for it gets a
sentence saying so, not a job that quietly does nothing -- a scheduler that
appears to fetch and does not is the failure this whole repository keeps finding.

Time is a parameter everywhere. One test starts a thread, and it is the only one,
because a suite that sleeps is a suite people stop running.
"""

import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.state.audit import ACTOR_SYSTEM, verify_chain
from app.state.db import init_db, session_scope
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    FEEDBACK_KIND_FEEDBACK,
    RATING_DOWN,
    TIMEOUT_BYPASS,
    AuditEvent,
    Feedback,
)
from app.state.workflow import (
    ACTION_STEP_REMINDED,
    open_step_run,
    start_run,
)

from app.state import db as state_db

from app.jobs import runner as runner_module
from app.jobs.runner import (
    ACTION_JOB_FAILED,
    ACTION_JOB_RAN,
    ACTION_JOB_RECOVERED,
    DEFAULT_INTERVAL_SECONDS,
    ENV_COMPANIES,
    ENV_ENABLED,
    ENV_INTERVAL,
    ENV_JOBS,
    ENV_RETENTION_DELETE,
    JOB_RETENTION,
    JOB_SOURCES,
    JOB_WORKFLOW,
    JOBS_ACTOR,
    Job,
    JobConfigError,
    JobLoop,
    JobOutcome,
    JobRunner,
    LockHeld,
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

from tests.test_routing import ACTOR, COMPANY, RIVAL, _person, _world
from tests.test_workflow import T0, _hours, _live_route, _step


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _lock(tmp_path: Path) -> Path:
    """A lock file of this test's own, so tests never contend with each other."""
    return tmp_path / "jobs.lock"


def _counting_job(name: str = "counter", *, every_seconds: int = 1) -> tuple[Job, list]:
    """A job that records every (company, now) it was handed and changes nothing."""
    seen: list[tuple[str, datetime]] = []

    def run(session, *, company_id: str, now: datetime) -> JobOutcome:
        seen.append((company_id, now))
        return JobOutcome(changed=False, summary="nothing to do")

    job = Job(
        name=name,
        every_seconds=every_seconds,
        run=run,
        description="a job that counts what it was asked to do",
    )
    return job, seen


def _angry_job(name: str = "angry", *, fails_for=(COMPANY,)) -> Job:
    """A job that raises for the named companies and works for the rest."""

    def run(session, *, company_id: str, now: datetime) -> JobOutcome:
        if company_id in fails_for:
            raise RuntimeError(f"the {name} job cannot read {company_id}")
        return JobOutcome(changed=True, summary=f"did something for {company_id}")

    return Job(
        name=name,
        every_seconds=1,
        run=run,
        description="a job that fails for one company",
    )


def _rows(session, action, company_id=COMPANY):
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .filter(AuditEvent.action == action)
        .order_by(AuditEvent.seq)
        .all()
    )


def _two_tenants(session):
    """Audit rows in two companies, so the roster has two names to find."""
    _world(session)
    _person(session, RIVAL, "raj")


def _old_complaint(session):
    """A complaint past its year, which the retention schedule would purge."""
    owner, _change, _escalation = _world(session)
    session.add(
        Feedback(
            id="FB-old",
            company_id=COMPANY,
            user_id=owner.id,
            kind=FEEDBACK_KIND_FEEDBACK,
            rating=RATING_DOWN,
            comment="the citation did not hold",
            created_at=T0 - timedelta(days=400),
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Per company, through the scope check
# ---------------------------------------------------------------------------


def test_every_job_is_handed_one_company_at_a_time():
    init_db()
    with session_scope() as session:
        _two_tenants(session)

    job, seen = _counting_job()
    runner = JobRunner((job,), lock_path=None)
    runner.run_once(now=T0)

    assert sorted(company for company, _ in seen) == [COMPANY, RIVAL]
    assert all(isinstance(company, str) and company for company, _ in seen)


def test_a_roster_given_by_hand_is_the_only_one_used():
    """A sweep restricted to one tenant must not touch another's rows."""
    init_db()
    with session_scope() as session:
        _two_tenants(session)

    job, seen = _counting_job()
    runner = JobRunner((job,), companies=(RIVAL,), lock_path=None)
    runner.run_once(now=T0)

    assert [company for company, _ in seen] == [RIVAL]


def test_an_empty_company_id_is_refused_when_the_runner_is_built():
    """Not at three in the morning, when the sweep would otherwise run unscoped."""
    job, _seen = _counting_job()
    with pytest.raises(ValueError):
        JobRunner((job,), companies=("",), lock_path=None)
    with pytest.raises(ValueError):
        JobRunner((job,), companies=(None,), lock_path=None)


def test_the_roster_is_discovered_from_the_tables_the_jobs_act_on():
    init_db()
    with session_scope() as session:
        _two_tenants(session)
        roster = company_roster(session)
    assert roster == (COMPANY, RIVAL)


def test_one_company_failing_leaves_the_other_committed():
    """The transaction boundary is per company, and this is what it buys."""
    init_db()
    with session_scope() as session:
        _two_tenants(session)

    runner = JobRunner((_angry_job(fails_for=(COMPANY,)),), lock_path=None)
    result = runner.run_once(now=T0)

    by_company = {record.company_id: record for record in result.records}
    assert by_company[COMPANY].error
    assert by_company[RIVAL].error == ""
    assert by_company[RIVAL].changed is True

    with session_scope() as session:
        # The company that worked has its row. The company that failed has a
        # failure row and no claim that anything ran.
        assert len(_rows(session, ACTION_JOB_RAN, RIVAL)) == 1
        assert len(_rows(session, ACTION_JOB_RAN, COMPANY)) == 0
        assert len(_rows(session, ACTION_JOB_FAILED, COMPANY)) == 1


# ---------------------------------------------------------------------------
# One run at a time
# ---------------------------------------------------------------------------


def test_a_second_run_does_not_wait_and_does_not_run(tmp_path):
    init_db()
    with session_scope() as session:
        _world(session)

    job, seen = _counting_job()
    runner = JobRunner((job,), lock_path=_lock(tmp_path))

    with exclusive(_lock(tmp_path)):
        result = runner.run_once(now=T0)

    assert result.ran is False
    assert result.records == ()
    assert "another run" in result.refusal
    assert seen == [], "the second caller ran nothing at all"


def test_the_lock_is_released_and_the_next_run_gets_it(tmp_path):
    init_db()
    with session_scope() as session:
        _world(session)

    job, seen = _counting_job()
    runner = JobRunner((job,), lock_path=_lock(tmp_path))
    assert runner.run_once(now=T0).ran is True
    assert runner.run_once(now=T0 + timedelta(seconds=30)).ran is True
    assert len(seen) == 2


def test_the_lock_refuses_a_second_holder_in_this_very_process(tmp_path):
    """Two threads in one process are the overlap most likely to happen here."""
    with exclusive(_lock(tmp_path)):
        with pytest.raises(LockHeld):
            with exclusive(_lock(tmp_path)):
                pass


def test_the_lock_sits_beside_the_database_and_never_inside_it():
    """Locking the database itself would block every write the product makes."""
    path = lock_path_for("sqlite:////data/strata.db")
    assert str(path) != "/data/strata.db"
    assert str(path).startswith("/data/strata.db")


def test_a_database_that_is_not_sqlite_is_refused_rather_than_run_unlocked():
    with pytest.raises(JobConfigError):
        lock_path_for("postgresql://localhost/strata")
    with pytest.raises(JobConfigError):
        lock_path_for("sqlite://")


# ---------------------------------------------------------------------------
# A failing job
# ---------------------------------------------------------------------------


def test_a_failure_is_recorded_once_per_streak_and_counted_every_time():
    init_db()
    with session_scope() as session:
        _world(session)

    runner = JobRunner((_angry_job(fails_for=(COMPANY,)),), companies=(COMPANY,), lock_path=None)
    first = runner.run_once(now=T0)
    second = runner.run_once(now=T0 + _hours(1))

    assert first.records[0].consecutive_failures == 1
    assert second.records[0].consecutive_failures == 2, (
        "the next run has to see that the last one failed"
    )
    with session_scope() as session:
        # One row for the streak, not one per tick: a job broken for six hours
        # would otherwise write three hundred and sixty identical rows.
        assert len(_rows(session, ACTION_JOB_FAILED)) == 1


def test_the_failure_reaches_the_log_every_single_time():
    init_db()
    with session_scope() as session:
        _world(session)

    lines: list[str] = []
    runner = JobRunner(
        (_angry_job(fails_for=(COMPANY,)),),
        companies=(COMPANY,),
        lock_path=None,
        log=lines.append,
    )
    runner.run_once(now=T0)
    runner.run_once(now=T0 + _hours(1))

    failed = [line for line in lines if "FAILED" in line]
    assert len(failed) == 2, "silence on the second tick is how this stays broken"
    assert "cannot read" in failed[-1]


def test_recovery_is_recorded_and_names_how_long_it_was_broken():
    init_db()
    with session_scope() as session:
        _world(session)

    broken = {"yes": True}

    def run(session, *, company_id, now):
        if broken["yes"]:
            raise RuntimeError("still broken")
        return JobOutcome(changed=False, summary="fine now")

    job = Job(name="flaky", every_seconds=1, run=run, description="breaks, then works")
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None)
    runner.run_once(now=T0)
    runner.run_once(now=T0 + _hours(1))
    broken["yes"] = False
    result = runner.run_once(now=T0 + _hours(2))

    assert result.records[0].consecutive_failures == 0
    with session_scope() as session:
        rows = _rows(session, ACTION_JOB_RECOVERED)
        assert len(rows) == 1
        assert "2" in rows[0].reason, "the count of failed attempts belongs in the row"
        assert verify_chain(session, COMPANY)


def test_a_job_that_fails_writes_no_claim_that_it_ran():
    """Absence is denial. A tick that could not run must not read as one that did."""
    init_db()
    with session_scope() as session:
        _world(session)

    runner = JobRunner((_angry_job(fails_for=(COMPANY,)),), companies=(COMPANY,), lock_path=None)
    runner.run_once(now=T0)
    with session_scope() as session:
        assert _rows(session, ACTION_JOB_RAN) == []


# ---------------------------------------------------------------------------
# Every run is recorded
# ---------------------------------------------------------------------------


def test_the_ledger_says_what_ran_for_whom_and_how_long():
    init_db()
    with session_scope() as session:
        _two_tenants(session)

    job, _seen = _counting_job()
    runner = JobRunner((job,), lock_path=None)
    result = runner.run_once(now=T0)

    assert len(result.records) == 2
    for record in result.records:
        assert record.job == "counter"
        assert record.company_id in (COMPANY, RIVAL)
        assert record.started_at == T0
        assert record.seconds >= 0
        assert record.summary
        assert record.job in record.line() and record.company_id in record.line()
    assert runner.ledger() == result.records


def test_a_tick_that_changed_nothing_writes_no_audit_row_and_is_still_in_the_log():
    """The chain records changes. The container log records every tick."""
    init_db()
    with session_scope() as session:
        _world(session)

    lines: list[str] = []
    job, _seen = _counting_job()
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None, log=lines.append)
    runner.run_once(now=T0)

    with session_scope() as session:
        assert _rows(session, ACTION_JOB_RAN) == []
    assert any("counter" in line and COMPANY in line for line in lines)


def test_the_audit_row_names_a_machine_and_never_a_person():
    init_db()
    with session_scope() as session:
        _world(session)

    runner = JobRunner((_angry_job(fails_for=()),), companies=(COMPANY,), lock_path=None)
    runner.run_once(now=T0)

    with session_scope() as session:
        row = _rows(session, ACTION_JOB_RAN)[0]
        assert row.actor == JOBS_ACTOR
        assert row.actor_kind == ACTOR_SYSTEM
        assert row.actor_user_id is None
        assert verify_chain(session, COMPANY)


# ---------------------------------------------------------------------------
# The workflow clock, driven by the runner rather than by a request
# ---------------------------------------------------------------------------


def test_the_workflow_clock_reminds_through_the_runner():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        run_id = start.run.id

    settings = settings_from_env({ENV_ENABLED: "true"})
    runner = JobRunner(build_jobs(settings), companies=(COMPANY,), lock_path=None)
    result = runner.run_once(now=T0 + _hours(25))

    record = result.records[0]
    assert record.job == JOB_WORKFLOW
    assert record.changed is True
    assert "1" in record.summary

    with session_scope() as session:
        step_run = open_step_run(session, COMPANY, run_id)
        assert step_run.reminder_count == 1
        assert len(_rows(session, ACTION_STEP_REMINDED)) == 1
        assert len(_rows(session, ACTION_JOB_RAN)) == 1


def test_two_sweeps_at_the_same_moment_do_not_double_remind():
    """Two runners is the shape that threatens: two processes, two fresh clocks.

    One runner will not run a job twice inside its own interval, so the overlap
    worth testing is the one the lock exists for -- and the job is idempotent
    underneath it, which is why an overlap is a wasted sweep and not a second
    reminder.
    """
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        run_id = start.run.id

    settings = settings_from_env({ENV_ENABLED: "true"})
    when = T0 + _hours(25)
    first = JobRunner(build_jobs(settings), companies=(COMPANY,), lock_path=None)
    second = JobRunner(build_jobs(settings), companies=(COMPANY,), lock_path=None)
    first.run_once(now=when)
    result = second.run_once(now=when)

    assert result.records[0].changed is False
    with session_scope() as session:
        assert open_step_run(session, COMPANY, run_id).reminder_count == 1
        assert len(_rows(session, ACTION_STEP_REMINDED)) == 1


def test_the_clock_never_reaches_a_company_the_sweep_was_not_given():
    """The bypass that must not happen: another tenant's step skipped by a sweep."""
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(
            session,
            steps=[
                _step(
                    "STP-1",
                    assignee_rule=ASSIGNEE_OBLIGATION_OWNER,
                    approval_hours=1,
                    on_timeout=TIMEOUT_BYPASS,
                )
            ],
            edges=(),
        )
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        run_id = start.run.id
        _person(session, RIVAL, "raj")

    settings = settings_from_env({ENV_ENABLED: "true"})
    runner = JobRunner(build_jobs(settings), companies=(RIVAL,), lock_path=None)
    runner.run_once(now=T0 + _hours(48))

    with session_scope() as session:
        assert open_step_run(session, COMPANY, run_id).outcome is None, (
            "a sweep for another tenant bypassed this company's step"
        )


# ---------------------------------------------------------------------------
# Retention: dry unless armed, and it says which
# ---------------------------------------------------------------------------


def test_retention_is_dry_by_default_and_the_flag_is_a_real_bool():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION})
    assert settings.retention_delete is False
    assert isinstance(settings.retention_delete, bool)


def test_only_the_word_true_arms_the_destructive_run():
    for value in ("true", "TRUE", " True "):
        settings = settings_from_env(
            {ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION, ENV_RETENTION_DELETE: value}
        )
        assert settings.retention_delete is True, value

    for value in ("1", "yes", "on", "y", "", "false", "no"):
        settings = settings_from_env(
            {ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION, ENV_RETENTION_DELETE: value}
        )
        assert settings.retention_delete is False, value


def test_a_value_nobody_can_read_lands_dry_and_says_it_did_not_understand():
    settings = settings_from_env(
        {ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION, ENV_RETENTION_DELETE: "1"}
    )
    assert settings.retention_delete is False
    assert any("1" in note and ENV_RETENTION_DELETE in note for note in settings.notes)
    assert any("DRY RUN" in line for line in boot_lines(settings))


def test_the_mode_is_printed_at_every_start_whichever_way_it_landed():
    dry = boot_lines(settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION}))
    assert any("retention" in line and "DRY RUN" in line for line in dry)

    armed = boot_lines(
        settings_from_env(
            {ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION, ENV_RETENTION_DELETE: "true"}
        )
    )
    assert any("retention" in line and "DELETES ROWS" in line for line in armed)
    assert not any("DRY RUN" in line for line in armed)


def test_a_dry_retention_run_deletes_nothing_and_says_what_it_would_have():
    init_db()
    with session_scope() as session:
        _old_complaint(session)

    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION})
    runner = JobRunner(build_jobs(settings), companies=(COMPANY,), lock_path=None)
    record = runner.run_once(now=T0).records[0]

    assert record.job == JOB_RETENTION
    assert record.changed is False, "a dry run changed nothing, whatever it matched"
    assert "would" in record.summary.lower()
    with session_scope() as session:
        assert session.get(Feedback, "FB-old") is not None


def test_an_armed_retention_run_deletes_and_the_chain_says_so():
    init_db()
    with session_scope() as session:
        _old_complaint(session)

    settings = settings_from_env(
        {ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION, ENV_RETENTION_DELETE: "true"}
    )
    runner = JobRunner(build_jobs(settings), companies=(COMPANY,), lock_path=None)
    record = runner.run_once(now=T0).records[0]

    assert record.changed is True
    with session_scope() as session:
        assert session.get(Feedback, "FB-old") is None
        assert len(_rows(session, ACTION_JOB_RAN)) == 1
        assert verify_chain(session, COMPANY)


def test_retention_runs_on_a_slower_clock_than_the_approval_sweep():
    settings = settings_from_env(
        {ENV_ENABLED: "true", ENV_JOBS: f"{JOB_WORKFLOW},{JOB_RETENTION}"}
    )
    jobs = {job.name: job for job in build_jobs(settings)}
    assert jobs[JOB_WORKFLOW].every_seconds < jobs[JOB_RETENTION].every_seconds
    assert jobs[JOB_RETENTION].destructive is True
    assert jobs[JOB_WORKFLOW].destructive is False


def test_a_job_is_skipped_until_its_own_interval_has_passed():
    init_db()
    with session_scope() as session:
        _world(session)

    fast, fast_seen = _counting_job("fast", every_seconds=1)
    slow, slow_seen = _counting_job("slow", every_seconds=3600)
    runner = JobRunner((fast, slow), companies=(COMPANY,), lock_path=None)

    runner.run_once(now=T0)
    runner.run_once(now=T0 + timedelta(seconds=2))
    assert len(fast_seen) == 2
    assert len(slow_seen) == 1, "the slow job is not due yet"

    runner.run_once(now=T0 + timedelta(seconds=3700))
    assert len(slow_seen) == 2


# ---------------------------------------------------------------------------
# What is enabled, and what is refused
# ---------------------------------------------------------------------------


def test_nothing_is_scheduled_unless_the_setting_asks():
    settings = settings_from_env({})
    assert settings.enabled is False
    assert settings.job_names == ()
    lines = boot_lines(settings)
    assert any("nothing is scheduled" in line.lower() for line in lines)
    assert any(ENV_ENABLED in line for line in lines)


def test_the_boot_lines_name_every_job_and_whether_it_is_on():
    settings = settings_from_env({ENV_ENABLED: "true"})
    lines = boot_lines(settings)
    text = "\n".join(lines)
    for name in (JOB_WORKFLOW, JOB_RETENTION, JOB_SOURCES):
        assert name in text
    assert f"{DEFAULT_INTERVAL_SECONDS}" in text


def test_the_default_when_the_loop_is_on_is_the_clock_and_not_the_purges():
    settings = settings_from_env({ENV_ENABLED: "true"})
    assert settings.job_names == (JOB_WORKFLOW,)
    assert [job.name for job in build_jobs(settings)] == [JOB_WORKFLOW]


def test_a_job_name_nobody_recognises_is_refused_rather_than_ignored():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: "workfow"})
    assert settings.refusal
    with pytest.raises(JobConfigError) as caught:
        build_jobs(settings)
    assert "workfow" in str(caught.value)
    assert JOB_WORKFLOW in str(caught.value), "say which names exist"


def test_the_source_fetch_is_named_refused_and_not_quietly_dropped():
    """It is not built. Asking for it is an error, not a job that does nothing."""
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_SOURCES})
    with pytest.raises(JobConfigError) as caught:
        build_jobs(settings)
    assert "not built" in str(caught.value).lower()
    assert "register" in str(caught.value).lower(), "say how it arrives when it exists"


def test_a_registered_job_takes_the_seam_without_anything_importing_it():
    fetch, seen = _counting_job(JOB_SOURCES)
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_SOURCES})
    jobs = build_jobs(settings, extra={JOB_SOURCES: fetch})
    assert [job.name for job in jobs] == [JOB_SOURCES]


def test_the_interval_falls_back_loudly_when_the_number_is_not_one():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_INTERVAL: "soon"})
    assert settings.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert any("soon" in note for note in settings.notes)

    settings = settings_from_env({ENV_ENABLED: "true", ENV_INTERVAL: "0"})
    assert settings.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert any(ENV_INTERVAL in note for note in settings.notes)


def test_an_explicit_roster_comes_from_the_setting():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_COMPANIES: "MEP, RIVAL"})
    assert settings.companies == ("MEP", "RIVAL")
    assert settings_from_env({ENV_ENABLED: "true"}).companies == ()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_the_loop_ticks_stops_when_asked_and_survives_a_failing_job():
    init_db()
    with session_scope() as session:
        _world(session)

    ticks: list[int] = []

    def run(session, *, company_id, now):
        ticks.append(1)
        raise RuntimeError("every tick fails")

    job = Job(name="always-fails", every_seconds=0, run=run, description="fails")
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None, log=lambda _line: None)
    loop = JobLoop(runner, interval_seconds=0.01, log=lambda _line: None)
    loop.start()
    try:
        deadline = time.monotonic() + 5
        while len(ticks) < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        loop.stop()

    assert len(ticks) >= 3, "a failing job stopped the loop"
    assert loop.alive is False


def test_a_stopped_loop_is_not_alive_before_it_starts():
    job, _seen = _counting_job()
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None)
    loop = JobLoop(runner, interval_seconds=60, log=lambda _line: None)
    assert loop.alive is False
    loop.stop()  # stopping something never started is not an error
    assert loop.alive is False


# ---------------------------------------------------------------------------
# What the container log has to show a reviewer
# ---------------------------------------------------------------------------


def test_the_entrypoint_starts_the_loop_only_when_the_setting_asks(repo_root):
    text = (repo_root / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    assert ENV_ENABLED in text
    assert "run_jobs.py --banner" in text
    assert "run_jobs.py --loop" in text
    # The start is inside a conditional on the setting, not at the top level.
    start = text.index("run_jobs.py --loop")
    guard = text.index(ENV_ENABLED)
    assert guard < start, "the loop must be started under the setting, not before it"


def test_the_script_exists_and_says_what_its_exit_codes_mean(repo_root):
    text = (repo_root / "scripts" / "run_jobs.py").read_text(encoding="utf-8")
    for code in ("0", "1", "2", "3"):
        assert f"{code} " in text
    assert "--loop" in text and "--once" in text and "--banner" in text


def _script(repo_root, *args, **env):
    """Run scripts/run_jobs.py the way a cron entry would: a fresh process."""
    import os
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "scripts/run_jobs.py", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **env},
    )


def test_the_script_says_nothing_is_scheduled_and_exits_clean(repo_root, tmp_path):
    done = _script(
        repo_root,
        "--once",
        STRATA_DATABASE_URL=f"sqlite:///{tmp_path / 'strata.db'}",
        STRATA_JOBS_ENABLED="",
    )
    assert done.returncode == 0, done.stderr
    assert "nothing is scheduled" in done.stdout.lower()


def test_the_script_refuses_a_job_name_this_build_does_not_have(repo_root, tmp_path):
    done = _script(
        repo_root,
        "--banner",
        STRATA_DATABASE_URL=f"sqlite:///{tmp_path / 'strata.db'}",
        STRATA_JOBS_ENABLED="true",
        STRATA_JOBS="retenton",
    )
    assert done.returncode == 2, done.stdout
    assert "retenton" in done.stdout


def test_a_second_process_is_told_the_lock_is_held_and_exits_three(repo_root, tmp_path):
    """The claim the whole lock exists for, proved across two real processes."""
    database = tmp_path / "strata.db"
    with exclusive(Path(str(database) + ".jobs.lock")):
        done = _script(
            repo_root,
            "--once",
            STRATA_DATABASE_URL=f"sqlite:///{database}",
            STRATA_JOBS_ENABLED="true",
        )
    assert done.returncode == 3, f"{done.stdout}\n{done.stderr}"
    assert "another run" in done.stdout


# ---------------------------------------------------------------------------
# The edges, each of which is a refusal
# ---------------------------------------------------------------------------


def test_a_job_that_reports_nothing_has_not_reported():
    with pytest.raises(ValueError):
        JobOutcome(changed=False, summary="")
    with pytest.raises(ValueError):
        JobOutcome(changed=1, summary="a truthy one is not a bool")


def test_a_job_that_hands_back_something_else_is_a_failure_not_a_success():
    init_db()
    with session_scope() as session:
        _world(session)

    def run(session, *, company_id, now):
        return "done"

    job = Job(name="liar", every_seconds=1, run=run, description="returns a string")
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None, log=lambda _l: None)
    record = runner.run_once(now=T0).records[0]
    assert "TypeError" in record.error
    with session_scope() as session:
        assert _rows(session, ACTION_JOB_RAN) == []


def test_a_moment_with_no_timezone_is_refused():
    job, _seen = _counting_job()
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None)
    with pytest.raises(ValueError):
        runner.run_once(now=datetime(2026, 8, 4, 9, 0))


def test_an_in_memory_database_has_nowhere_to_put_a_lock():
    with pytest.raises(JobConfigError):
        lock_path_for("sqlite:///:memory:")


def test_the_runner_finds_its_own_lock_beside_the_database_by_default():
    init_db()
    with session_scope() as session:
        _world(session)

    job, seen = _counting_job()
    runner = JobRunner((job,), companies=(COMPANY,))
    assert runner.run_once(now=T0).ran is True
    assert len(seen) == 1
    assert Path(lock_path_for(state_db.DATABASE_URL)).exists()


def test_the_retention_job_refuses_a_flag_that_is_not_a_bool():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_RETENTION})
    with pytest.raises(JobConfigError):
        build_jobs(replace(settings, retention_delete=1))


def test_an_interval_that_is_a_number_is_used():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_INTERVAL: "900"})
    assert settings.interval_seconds == 900
    assert settings.notes == ()


def test_a_switch_set_to_something_other_than_true_says_so():
    settings = settings_from_env({ENV_ENABLED: "yes"})
    assert settings.enabled is False
    assert any("yes" in note for note in settings.notes)


def test_a_refusal_is_printed_at_boot_rather_than_raised_there():
    """A banner that raises prints nothing, at the one moment somebody is reading."""
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: "workfow,retention"})
    lines = boot_lines(settings)
    assert any("REFUSED" in line for line in lines)
    assert any("workfow" in line for line in lines)


def test_a_refused_configuration_reads_as_off_from_the_very_first_line():
    """A reader who skims the first line must not come away believing it runs."""
    lines = boot_lines(
        settings_from_env({ENV_ENABLED: "true", ENV_JOBS: "workfow,retention"})
    )
    assert "OFF" in lines[0]
    assert not any("ON." in line for line in lines)
    # The job that WAS spelled correctly must not read as enabled either.
    assert any(f"{JOB_RETENTION}: not enabled" in line for line in lines)
    assert not any("ENABLED" in line for line in lines)


def test_the_boot_lines_say_the_source_fetch_is_refused_when_it_is_asked_for():
    settings = settings_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_SOURCES})
    lines = boot_lines(settings)
    assert any(
        JOB_SOURCES in line and "REFUSED" in line and "not built" in line.lower()
        for line in lines
    )


def test_a_loop_with_no_interval_is_a_spin_and_is_refused():
    job, _seen = _counting_job()
    runner = JobRunner((job,), companies=(COMPANY,), lock_path=None, log=lambda _l: None)
    for interval in (0, -1):
        with pytest.raises(JobConfigError):
            JobLoop(runner, interval_seconds=interval)


# ---------------------------------------------------------------------------
# The seam, and starting from the environment
# ---------------------------------------------------------------------------


def test_a_job_registers_itself_and_is_refused_a_second_time():
    fetch, _seen = _counting_job("a-registered-job")
    register(fetch)
    try:
        assert registered()["a-registered-job"] is fetch
        with pytest.raises(JobConfigError):
            register(fetch)
        with pytest.raises(JobConfigError):
            register("not a job")
    finally:
        runner_module._REGISTERED.pop("a-registered-job", None)


def test_nothing_starts_from_an_environment_that_did_not_ask():
    lines: list[str] = []
    assert loop_from_env({}, log=lines.append) is None
    assert any("nothing is scheduled" in line.lower() for line in lines)


def test_a_loop_started_from_the_environment_says_so_and_stops():
    init_db()
    with session_scope() as session:
        _world(session)

    lines: list[str] = []
    loop = loop_from_env(
        {ENV_ENABLED: "true", ENV_COMPANIES: COMPANY, ENV_INTERVAL: "3600"},
        log=lines.append,
    )
    assert loop is not None
    try:
        assert loop.alive is True
        assert any("loop started" in line for line in lines)
        with pytest.raises(JobConfigError):
            loop.start()
    finally:
        loop.stop()
    assert loop.alive is False


def test_a_configuration_refused_at_boot_starts_nothing_and_never_raises():
    """The web application must not fail to start because a job name is misspelt."""
    lines: list[str] = []
    assert loop_from_env({ENV_ENABLED: "true", ENV_JOBS: JOB_SOURCES}, log=lines.append) is None
    assert any("REFUSED" in line for line in lines)


def test_the_loop_survives_a_sweep_that_breaks_before_any_job_runs():
    """Not a job failing -- the sweep itself, which the runner cannot catch."""

    class Broken(JobRunner):
        calls = 0

        def run_once(self, *, now=None):
            Broken.calls += 1
            raise RuntimeError("the roster read failed")

    job, _seen = _counting_job()
    runner = Broken((job,), companies=(COMPANY,), lock_path=None, log=lambda _l: None)
    lines: list[str] = []
    loop = JobLoop(runner, interval_seconds=0.01, log=lines.append)
    loop.start()
    try:
        deadline = time.monotonic() + 5
        while Broken.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        loop.stop()

    assert Broken.calls >= 2, "the loop stopped when the sweep itself broke"
    assert any("THE SWEEP ITSELF FAILED" in line for line in lines)


def test_a_sweep_reports_its_own_failures_and_the_runner_remembers_them():
    init_db()
    with session_scope() as session:
        _two_tenants(session)

    runner = JobRunner(
        (_angry_job(fails_for=(COMPANY,)),), lock_path=None, log=lambda _l: None
    )
    result = runner.run_once(now=T0)
    assert [record.company_id for record in result.failures] == [COMPANY]
    assert runner.failing() == {("angry", COMPANY): 1}
