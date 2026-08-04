"""The project workspace: tenant scoping, audit, and counts that cannot go stale.

Four things this suite is built to catch, because each one has a cheap wrong
implementation that passes a casual read:

1. A project id from another tenant that resolves to a row. Every read and
   every write here takes an id from somewhere; the tests hand each one an id
   it must refuse.
2. A project opened without a trace. Creating one appends to the hash chain,
   and the chain has to still verify afterwards.
3. Knowledge edited in place. Superseding writes a new row; the old one stays
   readable and drops out of the live list. If a test can read the old body
   after superseding, the compounding store is doing its job.
4. A tile that disagrees with its project. Every count on ProjectCard is
   checked against the rows it claims to count.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.ingestion.ingest import ingest_version
from app.state.audit import event_count, verify_chain
from app.state.claims import REASON_CODE_QUOTE_MISMATCH
from app.state.db import init_db, session_scope
from app.state.models import Change, Claim, Escalation, Proceeding
from app.state.projects import (
    ProjectCard,
    add_knowledge,
    add_step,
    add_turn,
    attach_change,
    attachments_for_project,
    changes_for_project,
    create_project,
    create_work_plan,
    due_runs,
    knowledge_for_company,
    knowledge_for_project,
    knowledge_history,
    knowledge_item_for_company,
    open_thread,
    project_card,
    project_cards_for_company,
    project_for_company,
    projects_for_change,
    projects_for_company,
    record_run,
    schedule_run,
    scheduled_runs_for_project,
    set_step_state,
    set_thread_status,
    steps_for_plan,
    supersede_knowledge,
    threads_for_project,
    turns_for_thread,
    work_plans_for_project,
)

DEFINITION = (
    '"Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW)."
)
SOURCE = "SECTION 2. DEFINITIONS\n\n2.1 " + DEFINITION + "\n"
QUOTE_START = SOURCE.index(DEFINITION)
QUOTE_END = QUOTE_START + len(DEFINITION)

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def _at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


def _seed_corpus(session, company_id: str) -> None:
    """One company with two changes: the first escalated, the second resolved."""
    prefix = company_id.lower()
    ingest_version(
        session,
        version_id=f"{prefix}-v1",
        company_id=company_id,
        docket=f"{company_id}-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text=SOURCE,
    )
    session.add(
        Proceeding(
            id=f"{prefix}-proc",
            company_id=company_id,
            docket=f"{company_id}-2026-0142",
            commission="Public Utilities Commission",
            subject=f"{company_id} large load interconnection",
        )
    )
    for n in (1, 2):
        session.add(
            Change(
                id=f"{prefix}-chg-{n}",
                company_id=company_id,
                proceeding_id=f"{prefix}-proc",
                from_version_id=f"{prefix}-v1",
                to_version_id=f"{prefix}-v1",
                change_type="modified",
                before_start=QUOTE_START,
                before_end=QUOTE_END,
                after_start=QUOTE_START,
                after_end=QUOTE_END,
                section="2.1",
                alignment_confidence=0.94,
                materiality=None,
                status="DRAFT",
            )
        )
        session.add(
            Claim(
                id=f"{prefix}-claim-{n}",
                company_id=company_id,
                change_id=f"{prefix}-chg-{n}",
                statement=f"{company_id} threshold sits at 20 MW.",
                citation_version_id=f"{prefix}-v1",
                citation_start=QUOTE_START,
                citation_end=QUOTE_END,
                citation_quote=DEFINITION,
                cited_occurrence=None,
                confidence_bp=9200,
            )
        )
    # One open escalation, one already resolved. unreviewed_count must see the
    # difference; a naive count of escalations would not.
    session.add(
        Escalation(
            id=f"{prefix}-esc-1",
            company_id=company_id,
            claim_id=f"{prefix}-claim-1",
            reason_code=REASON_CODE_QUOTE_MISMATCH,
            reason_text="quoted text does not match the source",
            detail=f"{company_id} confidential detail",
        )
    )
    session.add(
        Escalation(
            id=f"{prefix}-esc-2",
            company_id=company_id,
            claim_id=f"{prefix}-claim-2",
            reason_code=REASON_CODE_QUOTE_MISMATCH,
            reason_text="quoted text does not match the source",
            detail=f"{company_id} confidential detail",
            resolved_at=_at(1),
            resolved_by="J. Okonkwo",
        )
    )
    session.flush()


def _seed_project(session, company_id: str, suffix: str = "a"):
    prefix = company_id.lower()
    return create_project(
        session,
        company_id,
        name=f"{company_id} large load interconnection {suffix}",
        jurisdiction="Monrovia",
        owner="J. Okonkwo",
        docket_ref=f"{company_id}-2026-0142",
        summary=f"{company_id} internal summary",
        project_id=f"{prefix}-proj-{suffix}",
        created_at=_at(0),
    )


def _seed_two_companies(session) -> None:
    for company_id in ("MEP", "RIVAL"):
        _seed_corpus(session, company_id)
        _seed_project(session, company_id, "a")


# --------------------------------------------------------------------------
# Tenant scoping
# --------------------------------------------------------------------------


def test_each_company_sees_only_its_own_projects():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        mine = projects_for_company(session, "MEP")
        assert [p.id for p in mine] == ["mep-proj-a"]
        assert not any("RIVAL" in p.summary for p in mine)

        theirs = projects_for_company(session, "RIVAL")
        assert [p.id for p in theirs] == ["rival-proj-a"]
        assert projects_for_company(session, "NOT-A-TENANT") == []


def test_a_project_lookup_across_tenants_returns_none_not_the_row():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert project_for_company(session, "MEP", "mep-proj-a").id == "mep-proj-a"
        assert project_for_company(session, "MEP", "rival-proj-a") is None
        assert project_for_company(session, "RIVAL", "mep-proj-a") is None
        assert project_for_company(session, "MEP", "no-such-project") is None


def test_every_child_read_refuses_another_tenants_parent_id():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        attach_change(session, "RIVAL", "rival-proj-a", "rival-chg-1", "R. Vale")
        open_thread(
            session,
            "RIVAL",
            "rival-proj-a",
            question="Does the RIVAL threshold bind us?",
            opened_by="R. Vale",
            thread_id="rival-thr",
            opened_at=_at(2),
        )
        create_work_plan(
            session, "RIVAL", "rival-proj-a", title="RIVAL plan", plan_id="rival-plan"
        )
        add_step(
            session,
            "RIVAL",
            "rival-plan",
            description="RIVAL step",
            owner="R. Vale",
            step_id="rival-step",
        )
        schedule_run(
            session,
            "RIVAL",
            "rival-proj-a",
            cadence="daily",
            next_run_at=_at(3),
            run_id="rival-run",
        )
        add_knowledge(
            session,
            "RIVAL",
            kind="definition",
            body="RIVAL definition",
            project_id="rival-proj-a",
            item_id="rival-kn",
        )

        # Knowing RIVAL's ids is not enough to read a single row of it.
        assert changes_for_project(session, "MEP", "rival-proj-a") == []
        assert attachments_for_project(session, "MEP", "rival-proj-a") == []
        assert threads_for_project(session, "MEP", "rival-proj-a") == []
        assert turns_for_thread(session, "MEP", "rival-thr") == []
        assert work_plans_for_project(session, "MEP", "rival-proj-a") == []
        assert steps_for_plan(session, "MEP", "rival-plan") == []
        assert scheduled_runs_for_project(session, "MEP", "rival-proj-a") == []
        assert knowledge_for_project(session, "MEP", "rival-proj-a") == []
        assert knowledge_item_for_company(session, "MEP", "rival-kn") is None
        assert project_card(session, "MEP", "rival-proj-a") is None
        assert projects_for_change(session, "MEP", "rival-chg-1") == []


def test_every_write_refuses_another_tenants_id():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        open_thread(
            session,
            "RIVAL",
            "rival-proj-a",
            question="RIVAL question",
            opened_by="R. Vale",
            thread_id="rival-thr",
            opened_at=_at(2),
        )
        create_work_plan(
            session, "RIVAL", "rival-proj-a", title="RIVAL plan", plan_id="rival-plan"
        )
        add_step(
            session,
            "RIVAL",
            "rival-plan",
            description="RIVAL step",
            owner="R. Vale",
            step_id="rival-step",
        )
        schedule_run(
            session,
            "RIVAL",
            "rival-proj-a",
            cadence="daily",
            next_run_at=_at(3),
            run_id="rival-run",
        )
        add_knowledge(
            session,
            "RIVAL",
            kind="definition",
            body="RIVAL definition",
            project_id="rival-proj-a",
            item_id="rival-kn",
        )

        writes = (
            lambda: attach_change(session, "MEP", "rival-proj-a", "mep-chg-1", "J. O."),
            lambda: attach_change(session, "MEP", "mep-proj-a", "rival-chg-1", "J. O."),
            lambda: open_thread(
                session, "MEP", "rival-proj-a", question="q", opened_by="J. O."
            ),
            lambda: add_turn(
                session, "MEP", "rival-thr", author_kind="analyst", body="note"
            ),
            lambda: set_thread_status(session, "MEP", "rival-thr", "answered"),
            lambda: create_work_plan(session, "MEP", "rival-proj-a", title="t"),
            lambda: add_step(
                session, "MEP", "rival-plan", description="d", owner="J. O."
            ),
            lambda: set_step_state(session, "MEP", "rival-step", "done", "J. O."),
            lambda: schedule_run(
                session, "MEP", "rival-proj-a", cadence="daily", next_run_at=_at(3)
            ),
            lambda: record_run(
                session,
                "MEP",
                "rival-run",
                ran_at=_at(4),
                result="no change",
                next_run_at=_at(28),
            ),
            lambda: add_knowledge(
                session, "MEP", kind="lesson", body="b", project_id="rival-proj-a"
            ),
            lambda: supersede_knowledge(session, "MEP", "rival-kn", "new", "J. O."),
        )
        for write in writes:
            with pytest.raises(ValueError):
                write()

        # And nothing of RIVAL's moved.
        assert knowledge_item_for_company(session, "RIVAL", "rival-kn").body == (
            "RIVAL definition"
        )
        assert changes_for_project(session, "RIVAL", "rival-proj-a") == []


def test_no_call_treats_a_missing_company_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q",
            opened_by="J. Okonkwo",
            thread_id="mep-thr",
            opened_at=_at(2),
        )

        reads = (
            lambda scope: projects_for_company(session, scope),
            lambda scope: project_for_company(session, scope, "mep-proj-a"),
            lambda scope: changes_for_project(session, scope, "mep-proj-a"),
            lambda scope: attachments_for_project(session, scope, "mep-proj-a"),
            lambda scope: projects_for_change(session, scope, "mep-chg-1"),
            lambda scope: threads_for_project(session, scope, "mep-proj-a"),
            lambda scope: turns_for_thread(session, scope, "mep-thr"),
            lambda scope: work_plans_for_project(session, scope, "mep-proj-a"),
            lambda scope: steps_for_plan(session, scope, "mep-plan"),
            lambda scope: scheduled_runs_for_project(session, scope, "mep-proj-a"),
            lambda scope: due_runs(session, scope, _at(9)),
            lambda scope: knowledge_for_project(session, scope, "mep-proj-a"),
            lambda scope: knowledge_for_company(session, scope),
            lambda scope: knowledge_item_for_company(session, scope, "mep-kn"),
            lambda scope: knowledge_history(session, scope, "mep-kn"),
            lambda scope: project_card(session, scope, "mep-proj-a"),
            lambda scope: project_cards_for_company(session, scope),
        )
        for read in reads:
            for scope in ("", None, "%"):
                try:
                    result = read(scope)
                except ValueError:
                    continue
                assert result in ([], None), f"{scope!r} behaved as a wildcard"

        writes = (
            lambda scope: create_project(
                session, scope, name="n", jurisdiction="j", owner="o"
            ),
            lambda scope: attach_change(
                session, scope, "mep-proj-a", "mep-chg-1", "J. O."
            ),
            lambda scope: open_thread(
                session, scope, "mep-proj-a", question="q", opened_by="J. O."
            ),
            lambda scope: add_turn(
                session, scope, "mep-thr", author_kind="analyst", body="b"
            ),
            lambda scope: create_work_plan(session, scope, "mep-proj-a", title="t"),
        )
        for write in writes:
            for scope in ("", None):
                with pytest.raises(ValueError):
                    write(scope)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_creating_a_project_writes_an_audit_event_and_the_chain_still_verifies():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        assert event_count(session, "MEP") == 0

        project = _seed_project(session, "MEP")

        assert event_count(session, "MEP") == 1
        assert verify_chain(session, "MEP") is True

        from app.state.models import AuditEvent

        entry = session.query(AuditEvent).filter_by(company_id="MEP").one()
        assert entry.action == "project.created"
        assert entry.subject_type == "project"
        assert entry.subject_id == project.id
        assert entry.actor == "J. Okonkwo"
        assert entry.occurred_at.tzinfo is not None


def test_one_companys_events_do_not_enter_anothers_chain():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")

        assert event_count(session, "MEP") == 2
        assert event_count(session, "RIVAL") == 1
        assert verify_chain(session, "MEP") is True
        assert verify_chain(session, "RIVAL") is True


def test_attaching_and_moving_a_step_both_land_in_the_chain():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        create_work_plan(
            session, "MEP", "mep-proj-a", title="Comment by 12 Sept", plan_id="mep-plan"
        )
        step = add_step(
            session,
            "MEP",
            "mep-plan",
            description="Draft the comment",
            owner="J. Okonkwo",
            step_id="mep-step-1",
        )
        assert step.state == "todo"

        moved = set_step_state(session, "MEP", "mep-step-1", "doing", "T. Adeyemi")
        assert moved.state == "doing"

        from app.state.models import AuditEvent

        actions = [
            e.action
            for e in session.query(AuditEvent)
            .filter_by(company_id="MEP")
            .order_by(AuditEvent.seq)
            .all()
        ]
        assert actions == ["project.created", "work_plan_step.state_changed"]
        assert verify_chain(session, "MEP") is True


def test_opening_a_thread_stays_out_of_the_chain():
    # Enquiry is not commitment. The rule is in the module docstring; this is
    # the test that keeps the next writer from quietly changing it.
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        before = event_count(session, "MEP")

        thread = open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="Does 20 MW bind existing customers?",
            opened_by="J. Okonkwo",
            opened_at=_at(2),
        )
        add_turn(
            session,
            "MEP",
            thread.id,
            author_kind="analyst",
            body="Checked the 2024 order.",
            created_at=_at(3),
        )
        assert event_count(session, "MEP") == before


# --------------------------------------------------------------------------
# A change attaches to more than one project
# --------------------------------------------------------------------------


def test_a_change_attaches_to_two_projects_and_appears_in_both():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP", "a")
        _seed_project(session, "MEP", "b")

        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        attach_change(session, "MEP", "mep-proj-b", "mep-chg-1", "T. Adeyemi")

        assert [c.id for c in changes_for_project(session, "MEP", "mep-proj-a")] == [
            "mep-chg-1"
        ]
        assert [c.id for c in changes_for_project(session, "MEP", "mep-proj-b")] == [
            "mep-chg-1"
        ]
        assert [p.id for p in projects_for_change(session, "MEP", "mep-chg-1")] == [
            "mep-proj-a",
            "mep-proj-b",
        ]

        # Who attached it is recorded per project, not shared.
        by_project = {
            row.project_id: row.attached_by
            for project_id in ("mep-proj-a", "mep-proj-b")
            for row in attachments_for_project(session, "MEP", project_id)
        }
        assert by_project == {"mep-proj-a": "J. Okonkwo", "mep-proj-b": "T. Adeyemi"}


def test_attaching_twice_returns_the_first_row_and_writes_no_second_event():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")

        first = attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        after_first = event_count(session, "MEP")
        again = attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "T. Adeyemi")

        assert again.id == first.id
        assert again.attached_by == "J. Okonkwo"
        assert len(attachments_for_project(session, "MEP", "mep-proj-a")) == 1
        assert event_count(session, "MEP") == after_first


# --------------------------------------------------------------------------
# Threads and turns
# --------------------------------------------------------------------------


def test_a_turn_moves_the_threads_last_activity_and_keeps_its_order():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        thread = open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="Does 20 MW bind existing customers?",
            opened_by="J. Okonkwo",
            thread_id="mep-thr",
            opened_at=_at(2),
        )
        assert thread.status == "open"
        assert thread.last_activity_at == _at(2)

        add_turn(
            session,
            "MEP",
            "mep-thr",
            author_kind="system",
            body="Two versions differ at 2.1.",
            created_at=_at(3),
        )
        add_turn(
            session,
            "MEP",
            "mep-thr",
            author_kind="analyst",
            body="Agreed, raising with counsel.",
            claim_id="mep-claim-1",
            created_at=_at(5),
        )

        turns = turns_for_thread(session, "MEP", "mep-thr")
        assert [t.author_kind for t in turns] == ["system", "analyst"]
        assert turns[1].claim_id == "mep-claim-1"
        assert turns[0].claim_id is None
        assert thread.last_activity_at == _at(5)


def test_a_turn_cannot_cite_another_companys_claim():
    # A cited claim renders beside the turn. An unchecked id here is a way to
    # pull another tenant's words onto this tenant's page.
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q",
            opened_by="J. Okonkwo",
            thread_id="mep-thr",
            opened_at=_at(2),
        )
        with pytest.raises(ValueError):
            add_turn(
                session,
                "MEP",
                "mep-thr",
                author_kind="analyst",
                body="note",
                claim_id="rival-claim-1",
            )
        assert turns_for_thread(session, "MEP", "mep-thr") == []


def test_an_unknown_author_kind_or_status_is_refused_at_the_write():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q",
            opened_by="J. Okonkwo",
            thread_id="mep-thr",
            opened_at=_at(2),
        )
        with pytest.raises(ValueError):
            add_turn(session, "MEP", "mep-thr", author_kind="robot", body="b")
        with pytest.raises(ValueError):
            set_thread_status(session, "MEP", "mep-thr", "OPEN")
        with pytest.raises(ValueError):
            create_project(
                session, "MEP", name="n", jurisdiction="j", owner="o", status="live"
            )


def test_open_thread_count_falls_when_a_thread_is_answered():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q1",
            opened_by="J. Okonkwo",
            thread_id="mep-thr-1",
            opened_at=_at(2),
        )
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q2",
            opened_by="J. Okonkwo",
            thread_id="mep-thr-2",
            opened_at=_at(3),
        )
        assert project_card(session, "MEP", "mep-proj-a").open_thread_count == 2

        set_thread_status(session, "MEP", "mep-thr-1", "answered")
        assert project_card(session, "MEP", "mep-proj-a").open_thread_count == 1
        # Answered, not deleted. The question and its turns stay.
        assert len(threads_for_project(session, "MEP", "mep-proj-a")) == 2


# --------------------------------------------------------------------------
# Work plans
# --------------------------------------------------------------------------


def test_steps_come_back_in_the_order_the_analyst_set():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        create_work_plan(session, "MEP", "mep-proj-a", title="Comment", plan_id="p1")

        add_step(session, "MEP", "p1", description="Read the order", owner="J. O.")
        add_step(session, "MEP", "p1", description="Draft comment", owner="T. A.")
        add_step(session, "MEP", "p1", description="File it", owner="J. O.")

        steps = steps_for_plan(session, "MEP", "p1")
        assert [s.ordinal for s in steps] == [1, 2, 3]
        assert [s.description for s in steps] == [
            "Read the order",
            "Draft comment",
            "File it",
        ]
        assert all(s.state == "todo" for s in steps)


def test_a_step_cannot_be_tied_to_another_companys_change():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        create_work_plan(session, "MEP", "mep-proj-a", title="Comment", plan_id="p1")
        with pytest.raises(ValueError):
            add_step(
                session,
                "MEP",
                "p1",
                description="d",
                owner="J. O.",
                change_id="rival-chg-1",
            )
        assert steps_for_plan(session, "MEP", "p1") == []


def test_an_unknown_step_state_is_refused():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        create_work_plan(session, "MEP", "mep-proj-a", title="Comment", plan_id="p1")
        add_step(session, "MEP", "p1", description="d", owner="J. O.", step_id="s1")
        with pytest.raises(ValueError):
            set_step_state(session, "MEP", "s1", "finished", "J. O.")
        assert steps_for_plan(session, "MEP", "p1")[0].state == "todo"


# --------------------------------------------------------------------------
# Scheduled re-runs
# --------------------------------------------------------------------------


def test_due_runs_returns_only_this_companys_enabled_and_due_runs():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="daily",
            next_run_at=_at(4),
            run_id="mep-run-due",
        )
        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="weekly",
            next_run_at=_at(48),
            run_id="mep-run-later",
        )
        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="on-filing",
            next_run_at=_at(1),
            run_id="mep-run-off",
            enabled=False,
        )
        schedule_run(
            session,
            "RIVAL",
            "rival-proj-a",
            cadence="daily",
            next_run_at=_at(1),
            run_id="rival-run-due",
        )

        assert [r.id for r in due_runs(session, "MEP", _at(6))] == ["mep-run-due"]
        assert [r.id for r in due_runs(session, "RIVAL", _at(6))] == ["rival-run-due"]
        assert due_runs(session, "MEP", _at(0)) == []

        # A naive clock is refused rather than compared.
        with pytest.raises(ValueError):
            due_runs(session, "MEP", datetime(2026, 8, 1, 12, 0))


def test_a_run_that_has_never_fired_says_so_and_records_a_result_when_it_does():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        run = schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="daily",
            next_run_at=_at(4),
            run_id="mep-run",
        )
        assert run.last_run_at is None
        assert run.last_result is None

        record_run(
            session,
            "MEP",
            "mep-run",
            ran_at=_at(4),
            result="no new version found",
            next_run_at=_at(28),
        )
        after = scheduled_runs_for_project(session, "MEP", "mep-proj-a")[0]
        assert after.last_run_at == _at(4)
        assert after.last_result == "no new version found"
        assert after.next_run_at == _at(28)
        assert due_runs(session, "MEP", _at(6)) == []

        # A silent run is refused: "found nothing" has to be written down.
        with pytest.raises(ValueError):
            record_run(
                session,
                "MEP",
                "mep-run",
                ran_at=_at(28),
                result="",
                next_run_at=_at(52),
            )


# --------------------------------------------------------------------------
# Knowledge: superseded, never edited
# --------------------------------------------------------------------------


def test_superseding_leaves_the_original_readable_but_out_of_the_live_list():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")

        original = add_knowledge(
            session,
            "MEP",
            kind="mapping",
            body="Our load-study duty maps to the 20 MW threshold.",
            project_id="mep-proj-a",
            source_claim_id="mep-claim-1",
            item_id="mep-kn-1",
            created_at=_at(2),
        )
        assert [i.id for i in knowledge_for_project(session, "MEP", "mep-proj-a")] == [
            "mep-kn-1"
        ]

        replacement = supersede_knowledge(
            session,
            "MEP",
            "mep-kn-1",
            "Our load-study duty maps to the 15 MW threshold after the final order.",
            "T. Adeyemi",
        )

        live = knowledge_for_project(session, "MEP", "mep-proj-a")
        assert [i.id for i in live] == [replacement.id]
        assert "15 MW" in live[0].body
        assert live[0].confirmed_by == "T. Adeyemi"
        # Carried over rather than reinvented.
        assert live[0].kind == original.kind
        assert live[0].source_claim_id == "mep-claim-1"

        # The old belief is still there, word for word, and still points forward.
        old = knowledge_item_for_company(session, "MEP", "mep-kn-1")
        assert old.body == "Our load-study duty maps to the 20 MW threshold."
        assert old.superseded_by == replacement.id
        assert [i.id for i in knowledge_history(session, "MEP", "mep-kn-1")] == [
            "mep-kn-1",
            replacement.id,
        ]
        assert verify_chain(session, "MEP") is True


def test_an_item_cannot_be_superseded_twice():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        add_knowledge(
            session,
            "MEP",
            kind="lesson",
            body="first",
            project_id="mep-proj-a",
            item_id="mep-kn-1",
            created_at=_at(2),
        )
        second = supersede_knowledge(session, "MEP", "mep-kn-1", "second", "J. O.")

        with pytest.raises(ValueError):
            supersede_knowledge(session, "MEP", "mep-kn-1", "third", "J. O.")

        third = supersede_knowledge(session, "MEP", second.id, "third", "J. O.")
        assert [i.body for i in knowledge_history(session, "MEP", "mep-kn-1")] == [
            "first",
            "second",
            "third",
        ]
        assert [i.id for i in knowledge_for_project(session, "MEP", "mep-proj-a")] == [
            third.id
        ]


def test_company_wide_knowledge_is_not_project_knowledge():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")
        add_knowledge(
            session,
            "MEP",
            kind="definition",
            body="We word our own threshold as Requested Capacity.",
            project_id=None,
            item_id="mep-kn-wide",
            created_at=_at(2),
        )
        add_knowledge(
            session,
            "MEP",
            kind="precedent",
            body="The 2024 order read 20 MW as nameplate.",
            project_id="mep-proj-a",
            item_id="mep-kn-proj",
            created_at=_at(3),
        )

        assert [i.id for i in knowledge_for_project(session, "MEP", "mep-proj-a")] == [
            "mep-kn-proj"
        ]
        assert [i.id for i in knowledge_for_company(session, "MEP")] == [
            "mep-kn-wide",
            "mep-kn-proj",
        ]


# --------------------------------------------------------------------------
# The card is computed, never stored
# --------------------------------------------------------------------------


def test_project_card_counts_match_the_underlying_rows():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")

        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        attach_change(session, "MEP", "mep-proj-a", "mep-chg-2", "J. Okonkwo")
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q1",
            opened_by="J. Okonkwo",
            thread_id="mep-thr-1",
            opened_at=_at(2),
        )
        open_thread(
            session,
            "MEP",
            "mep-proj-a",
            question="q2",
            opened_by="J. Okonkwo",
            thread_id="mep-thr-2",
            opened_at=_at(3),
        )
        set_thread_status(session, "MEP", "mep-thr-2", "parked")
        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="weekly",
            next_run_at=_at(48),
            run_id="mep-run-late",
        )
        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="daily",
            next_run_at=_at(12),
            run_id="mep-run-soon",
        )
        schedule_run(
            session,
            "MEP",
            "mep-proj-a",
            cadence="daily",
            next_run_at=_at(1),
            run_id="mep-run-off",
            enabled=False,
        )

        card = project_card(session, "MEP", "mep-proj-a")
        assert isinstance(card, ProjectCard)
        assert card.project_id == "mep-proj-a"
        assert card.name == "MEP large load interconnection a"
        assert card.jurisdiction == "Monrovia"
        assert card.docket_ref == "MEP-2026-0142"
        assert card.status == "active"

        attached = changes_for_project(session, "MEP", "mep-proj-a")
        assert card.change_count == len(attached)
        assert card.change_count == 2
        # mep-chg-1 carries an open escalation; mep-chg-2's was resolved.
        assert card.unreviewed_count == 1
        assert card.open_thread_count == 1
        # The disabled run never fires, so it is not what happens next.
        assert card.next_run_at == _at(12)
        assert card.last_activity_at.tzinfo is not None

        assert [c.project_id for c in project_cards_for_company(session, "MEP")] == [
            "mep-proj-a"
        ]


def test_the_card_follows_the_rows_rather_than_a_stored_number():
    init_db()
    with session_scope() as session:
        _seed_corpus(session, "MEP")
        _seed_project(session, "MEP")

        empty = project_card(session, "MEP", "mep-proj-a")
        assert (empty.change_count, empty.unreviewed_count) == (0, 0)
        assert empty.next_run_at is None
        assert empty.last_activity_at == _at(0)

        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        after = project_card(session, "MEP", "mep-proj-a")
        assert (after.change_count, after.unreviewed_count) == (1, 1)
        assert after.last_activity_at > empty.last_activity_at

        # Resolve the escalation and the count falls on the next read. No
        # recompute step, no cache to invalidate.
        escalation = session.get(Escalation, "mep-esc-1")
        escalation.resolved_at = _at(6)
        escalation.resolved_by = "T. Adeyemi"
        session.flush()
        assert project_card(session, "MEP", "mep-proj-a").unreviewed_count == 0


def test_a_card_never_counts_another_tenants_rows():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        attach_change(session, "MEP", "mep-proj-a", "mep-chg-1", "J. Okonkwo")
        attach_change(session, "RIVAL", "rival-proj-a", "rival-chg-1", "R. Vale")
        attach_change(session, "RIVAL", "rival-proj-a", "rival-chg-2", "R. Vale")

        assert project_card(session, "MEP", "mep-proj-a").change_count == 1
        assert project_card(session, "RIVAL", "rival-proj-a").change_count == 2
        assert project_card(session, "MEP", "rival-proj-a") is None
