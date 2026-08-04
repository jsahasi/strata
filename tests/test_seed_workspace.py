"""The seeded workspace, against the real corpus in data/.

Four things this suite exists to catch, because each has a cheap wrong
implementation that looks right on a screen.

1. A second seed that writes a second row. `make run` seeds on every start, so
   a loader that duplicated one thread turn would make every count in the
   product wrong by the second morning -- and the screen would not say so.
   Every workspace total is compared across two runs, and so is the audit chain.

2. A synthesis that hides what it could not use. The corpus contains two claims
   that fail verification and one finding nobody has cited. The seeded take and
   the seeded deliverable must both carry a non-zero withheld count, and both
   counts must equal what coverage says right now.

3. Knowledge edited in place. Superseding writes a new row; the old one leaves
   the live list and stays readable by id. If the old body cannot be read after
   superseding, the compounding store is not compounding.

4. A withheld claim's sentence reaching a page under another name. Every text
   column the workspace writes is searched for the two statements the product
   refuses to assert. A finding headline is the obvious place one would leak.

Everything is checked against data/manifest.json and data/company_context.json
rather than against literals typed here, so editing the corpus without editing
the seed fails in this file rather than in a demo.
"""

import json

from app.seed import load
from app.state.audit import event_count, verify_chain
from app.state.claims import escalations_for_company, verified_claims
from app.state.db import init_db, session_scope
from app.state.models import (
    Claim,
    CollectiveTake,
    Deliverable,
    Finding,
    KnowledgeItem,
    Project,
    ProjectChange,
    Question,
    ResearchThread,
    ResearchTurn,
    ScheduledRun,
    Source,
    SteerDirective,
    WorkPlan,
    WorkPlanStep,
)
from app.state.projects import (
    knowledge_for_company,
    knowledge_for_project,
    knowledge_history,
    knowledge_item_for_company,
    project_card,
    project_cards_for_company,
    scheduled_runs_for_project,
    steps_for_plan,
    threads_for_project,
    turns_for_thread,
    work_plans_for_project,
)
from app.state.review import (
    collective_take_for_project,
    coverage_for_project,
    deliverables_for_project,
    findings_for_project,
    open_questions,
    sources_for_project,
    steer_directives_for_project,
)

COMPANY = "MEP"
OTHER_COMPANY = "AUB"
DOCKET = "MPUC-2026-0142"
DOCKET_PROJECT = f"PRJ-{DOCKET}"
LESSON = "KN-LESSON-COST-ALLOCATION"

# Every table the workspace writes, scoped by company_id. Listed once so a new
# table added to the seed without a count here is a visible omission.
_TENANT_TABLES = (
    Project,
    ProjectChange,
    ResearchThread,
    ResearchTurn,
    WorkPlan,
    WorkPlanStep,
    ScheduledRun,
    KnowledgeItem,
    Source,
    Finding,
    Question,
    CollectiveTake,
    Deliverable,
    SteerDirective,
)


def _manifest(data_dir):
    return json.loads((data_dir / "manifest.json").read_bytes().decode("utf-8"))


def _context(data_dir):
    return json.loads((data_dir / "company_context.json").read_bytes().decode("utf-8"))


def _seed():
    with session_scope() as session:
        return load(session)


def _row_ids(session, model) -> list:
    return sorted(str(row.id) for row in session.query(model).all())


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_seeding_twice_yields_identical_workspace_counts():
    init_db()
    first = _seed()
    second = _seed()

    assert first.workspace == second.workspace
    # The whole report, so a duplicated row anywhere moves a number here.
    assert first == second


def test_the_second_seed_duplicates_no_workspace_row():
    init_db()
    _seed()
    with session_scope() as session:
        before = {model.__name__: _row_ids(session, model) for model in _TENANT_TABLES}

    _seed()
    with session_scope() as session:
        after = {model.__name__: _row_ids(session, model) for model in _TENANT_TABLES}

    assert after == before
    for name, ids in after.items():
        assert ids, f"{name} was not seeded at all"
        assert len(set(ids)) == len(ids), f"{name} has a duplicate id"


def test_the_second_seed_appends_no_audit_entry_and_the_chain_still_verifies():
    init_db()
    _seed()
    with session_scope() as session:
        events = event_count(session, COMPANY)
        assert verify_chain(session, COMPANY) is True

    _seed()
    with session_scope() as session:
        assert event_count(session, COMPANY) == events
        assert verify_chain(session, COMPANY) is True


def test_the_second_seed_does_not_supersede_the_knowledge_item_again():
    # supersede_knowledge() writes a new row and an audit entry. Called twice it
    # would fork the history, which is the failure the guard in the seed exists
    # to prevent and the one a count alone would not name.
    init_db()
    _seed()
    with session_scope() as session:
        chain = [item.id for item in knowledge_history(session, COMPANY, LESSON)]
        assert len(chain) == 2

    _seed()
    with session_scope() as session:
        assert [i.id for i in knowledge_history(session, COMPANY, LESSON)] == chain


# --------------------------------------------------------------------------
# The projects
# --------------------------------------------------------------------------


def test_the_docket_project_and_every_project_in_the_company_context_exist(data_dir):
    init_db()
    _seed()
    context = _context(data_dir)

    with session_scope() as session:
        cards = {card.project_id: card for card in project_cards_for_company(
            session, COMPANY
        )}

    expected = {DOCKET_PROJECT} | {p["id"] for p in context["projects"]}
    assert set(cards) == expected
    assert len(cards) >= 3, "the card grid needs something to show"
    assert cards[DOCKET_PROJECT].docket_ref == DOCKET
    # An internal programme tracks no docket of its own.
    for project in context["projects"]:
        assert cards[project["id"]].docket_ref is None


def test_every_change_the_diff_found_attaches_to_the_docket_project():
    init_db()
    report = _seed()

    with session_scope() as session:
        card = project_card(session, COMPANY, DOCKET_PROJECT)

    assert card is not None
    assert card.change_count == report.changes
    # Two claims fail verification and they anchor on two different changes, so
    # two attached changes are still waiting on a person.
    assert card.unreviewed_count == 2
    assert card.open_thread_count == 1


def test_an_internal_project_only_gets_the_changes_its_obligations_reach(data_dir):
    # The join is the one company_context.json already describes: a change binds
    # a project when the obligation it maps to is one the project holds. Checked
    # against the files rather than against a list typed in the seed.
    init_db()
    _seed()
    manifest, context = _manifest(data_dir), _context(data_dir)

    with session_scope() as session:
        cards = {c.project_id: c for c in project_cards_for_company(session, COMPANY)}

    for project in context["projects"]:
        held = set(project["related_obligation_ids"])
        expected = sum(
            1
            for change in manifest["changes"]
            if held.intersection(
                [change["maps_to_obligation_id"]]
                + change.get("also_related_obligation_ids", [])
            )
        )
        assert cards[project["id"]].change_count == expected


def test_the_card_grid_shows_a_project_that_is_watched_rather_than_worked():
    init_db()
    _seed()
    with session_scope() as session:
        statuses = {c.project_id: c.status for c in project_cards_for_company(
            session, COMPANY
        )}

    assert set(statuses.values()) >= {"active", "monitoring"}


# --------------------------------------------------------------------------
# Threads, plans, runs
# --------------------------------------------------------------------------


def test_one_thread_is_answered_on_a_verified_claim_and_one_is_still_open():
    init_db()
    _seed()

    with session_scope() as session:
        threads = threads_for_project(session, COMPANY, DOCKET_PROJECT)
        statuses = {thread.status for thread in threads}
        cited: list[tuple[str, str]] = []
        for thread in threads:
            for turn in turns_for_thread(session, COMPANY, thread.id):
                if turn.claim_id is not None:
                    cited.append((turn.claim_id, turn.author_kind))

        assert "open" in statuses, "no thread is still waiting for an answer"
        assert "answered" in statuses, "nothing was ever answered"
        assert cited, "no turn cites a claim at all"

        # The cited claim has to be one the product will actually assert, and
        # verified right now rather than by a stored verdict.
        escalated = {
            row.claim_id
            for row in escalations_for_company(session, COMPANY, unresolved_only=True)
        }
        for claim_id, author_kind in cited:
            assert author_kind == "system"
            assert claim_id not in escalated
            claim = session.get(Claim, claim_id)
            assert claim is not None and claim.company_id == COMPANY
            verified, _withheld = verified_claims(session, COMPANY, claim.change_id)
            assert claim_id in {row.claim_id for row in verified}


def test_the_open_thread_has_no_answer_written_into_it():
    init_db()
    _seed()
    with session_scope() as session:
        threads = threads_for_project(session, COMPANY, DOCKET_PROJECT)
        still_open = [t for t in threads if t.status == "open"]
        assert still_open
        for thread in still_open:
            kinds = {
                turn.author_kind
                for turn in turns_for_thread(session, COMPANY, thread.id)
            }
            assert kinds == {"analyst"}, "an open thread carrying a system answer"


def test_the_plan_has_a_blocked_step_and_a_step_tied_to_a_change():
    init_db()
    _seed()

    with session_scope() as session:
        plans = work_plans_for_project(session, COMPANY, DOCKET_PROJECT)
        assert len(plans) == 1
        steps = steps_for_plan(session, COMPANY, plans[0].id)

        states = [step.state for step in steps]
        ordinals = [step.ordinal for step in steps]
        assert ordinals == sorted(ordinals)
        assert "blocked" in states
        assert len(set(states)) >= 3, "a plan where every step is todo shows nothing"
        assert any(step.change_id for step in steps)
        # And one that answers no single change, which is why the column is
        # nullable.
        assert any(step.change_id is None for step in steps)


def test_the_run_schedule_shows_four_states_that_must_not_render_the_same():
    init_db()
    _seed()

    with session_scope() as session:
        runs = {}
        for card in project_cards_for_company(session, COMPANY):
            runs[card.project_id] = scheduled_runs_for_project(
                session, COMPANY, card.project_id
            )

        fired = [r for rows in runs.values() for r in rows if r.last_run_at is not None]
        never = [r for rows in runs.values() for r in rows if r.last_run_at is None]
        off = [r for rows in runs.values() for r in rows if not r.enabled]

        assert fired, "no run has ever fired"
        # A run reports even when it found nothing, or a stopped scheduler looks
        # exactly like a quiet week.
        assert all(run.last_result for run in fired)
        assert never, "no run is waiting for its first fire"
        assert off, "no run is switched off"
        assert any(not rows for rows in runs.values()), "no project without a schedule"

        cadences = {r.cadence for rows in runs.values() for r in rows}
        assert {"daily", "on-filing"} <= cadences


def test_the_enabled_run_is_scheduled_ahead_of_the_run_that_last_fired():
    init_db()
    _seed()
    with session_scope() as session:
        runs = scheduled_runs_for_project(session, COMPANY, DOCKET_PROJECT)
        assert runs
        for run in runs:
            if run.last_run_at is not None:
                assert run.next_run_at > run.last_run_at


# --------------------------------------------------------------------------
# Knowledge compounds, and keeps its history
# --------------------------------------------------------------------------


def test_the_superseded_item_leaves_the_live_list_and_stays_readable(data_dir):
    init_db()
    _seed()
    context = _context(data_dir)
    was = next(o for o in context["obligations"] if o["id"] == "OBL-005")

    with session_scope() as session:
        live = {item.id for item in knowledge_for_project(
            session, COMPANY, DOCKET_PROJECT
        )}
        assert LESSON not in live

        stored = knowledge_item_for_company(session, COMPANY, LESSON)
        assert stored is not None
        assert stored.superseded_by is not None
        assert stored.superseded_by in live
        # The old body was never touched. It is the company's belief in the
        # spring, and it is still the record of it.
        assert stored.body == was["internal_wording"]

        chain = knowledge_history(session, COMPANY, LESSON)
        assert [item.id for item in chain] == [LESSON, stored.superseded_by]
        assert chain[1].body != chain[0].body
        assert chain[1].confirmed_by


def test_company_wide_knowledge_is_not_filed_under_one_project():
    init_db()
    _seed()
    with session_scope() as session:
        everything = knowledge_for_company(session, COMPANY)
        company_wide = [item for item in everything if item.project_id is None]
        assert company_wide, "nothing the company knows outside one project"

        scoped = knowledge_for_project(session, COMPANY, DOCKET_PROJECT)
        assert not set(i.id for i in company_wide) & set(i.id for i in scoped)


# --------------------------------------------------------------------------
# The review centre, and the count the whole product turns on
# --------------------------------------------------------------------------


def test_the_collective_take_states_a_non_zero_withheld_count():
    init_db()
    _seed()

    with session_scope() as session:
        take = collective_take_for_project(session, COMPANY, DOCKET_PROJECT)
        coverage = coverage_for_project(session, COMPANY, DOCKET_PROJECT)

        assert take is not None
        assert take.findings_withheld > 0, "a take that hides its exclusions"
        assert take.findings_included == coverage.findings_verified
        assert take.findings_withheld == coverage.findings_withheld
        assert take.superseded_by is None
        assert take.body


def test_the_withheld_findings_are_the_corpus_failures_plus_the_uncited_one():
    init_db()
    _seed()

    with session_scope() as session:
        escalated = {
            row.claim_id
            for row in escalations_for_company(session, COMPANY, unresolved_only=True)
        }
        findings = findings_for_project(session, COMPANY, DOCKET_PROJECT)
        coverage = coverage_for_project(session, COMPANY, DOCKET_PROJECT)

    assert len(escalated) == 2
    withheld = [f for f in findings if f.claim_id in escalated or f.claim_id is None]
    assert len(withheld) == coverage.findings_withheld
    assert coverage.findings_verified + coverage.findings_withheld == len(findings)
    # A finding raised by a person and never cited counts against coverage, the
    # same rule the model's output is held to.
    assert any(f.claim_id is None for f in findings)


def test_the_deliverable_carries_the_same_coverage_as_the_take():
    init_db()
    _seed()

    with session_scope() as session:
        deliverables = deliverables_for_project(session, COMPANY, DOCKET_PROJECT)
        take = collective_take_for_project(session, COMPANY, DOCKET_PROJECT)

    assert len(deliverables) == 1
    memo = deliverables[0]
    assert memo.state == "in_review"
    # Nobody has signed it. Empty is not approved.
    assert memo.approved_by is None
    assert (memo.findings_included, memo.findings_withheld) == (
        take.findings_included,
        take.findings_withheld,
    )
    assert memo.findings_withheld > 0


def test_coverage_counts_both_source_kinds_and_a_blocking_question(data_dir):
    init_db()
    _seed()
    manifest, context = _manifest(data_dir), _context(data_dir)

    with session_scope() as session:
        coverage = coverage_for_project(session, COMPANY, DOCKET_PROJECT)
        sources = sources_for_project(session, COMPANY, DOCKET_PROJECT)
        unanswered = open_questions(session, COMPANY, DOCKET_PROJECT)

    assert coverage.sources_external == len(manifest["versions"])
    assert coverage.sources_internal == len(context["documents"])
    # Every source kind is one of the two named ones, so the totals add up.
    assert coverage.sources_internal + coverage.sources_external == len(sources)
    assert coverage.blocking_questions == 1
    assert coverage.open_questions == len(unanswered)
    # Blocking first, because that is the order the work is done in.
    assert unanswered[0].blocking is True


def test_one_steer_is_applied_with_an_effect_and_one_is_still_waiting():
    init_db()
    _seed()

    with session_scope() as session:
        directives = steer_directives_for_project(
            session, COMPANY, DOCKET_PROJECT, include_revoked=True
        )

    assert len(directives) == 2
    applied = [d for d in directives if d.applied_at is not None]
    waiting = [d for d in directives if d.applied_at is None]
    assert len(applied) == 1 and len(waiting) == 1
    assert applied[0].effect
    # A blank effect means nobody has written one down, never that the
    # directive did nothing.
    assert waiting[0].effect is None
    for directive in directives:
        assert directive.issued_by
        assert directive.revoked_at is None


# --------------------------------------------------------------------------
# Nothing withheld reaches a page, in any field
# --------------------------------------------------------------------------


def test_no_withheld_claim_sentence_appears_in_any_workspace_text():
    # ADR-003 applied to the workspace. WithheldClaim has no statement field, so
    # a template cannot render one -- but a seed that copied the sentence into a
    # finding headline would put the assertion on the page under another name,
    # and no type would stop it.
    init_db()
    _seed()

    with session_scope() as session:
        escalated = {
            row.claim_id for row in escalations_for_company(session, COMPANY)
        }
        assert escalated
        refused = [
            session.get(Claim, claim_id).statement for claim_id in sorted(escalated)
        ]

        written: list[str] = []
        for finding in session.query(Finding).all():
            written += [finding.headline, finding.detail]
        for turn in session.query(ResearchTurn).all():
            written.append(turn.body)
        for thread in session.query(ResearchThread).all():
            written.append(thread.question)
        for item in session.query(KnowledgeItem).all():
            written.append(item.body)
        for question in session.query(Question).all():
            written += [question.body, question.answer or ""]
        for take in session.query(CollectiveTake).all():
            written.append(take.body)
        for memo in session.query(Deliverable).all():
            written += [memo.title, memo.body]
        for directive in session.query(SteerDirective).all():
            written += [directive.instruction, directive.effect or ""]
        for step in session.query(WorkPlanStep).all():
            written.append(step.description)

    assert written
    for statement in refused:
        assert statement
        for text in written:
            assert statement not in text, f"a withheld sentence reached {text!r}"


# --------------------------------------------------------------------------
# Every seeded row belongs to one company
# --------------------------------------------------------------------------


def test_every_seeded_workspace_row_is_scoped_to_one_company():
    init_db()
    _seed()

    with session_scope() as session:
        for model in _TENANT_TABLES:
            companies = {row.company_id for row in session.query(model).all()}
            assert companies == {COMPANY}, f"{model.__name__} escaped its tenant"


def test_another_company_reads_none_of_it():
    # The seed writes one company's work. A neighbour asking for the same
    # project id must get nothing back, not a row with somebody else's docket
    # on it.
    init_db()
    _seed()

    with session_scope() as session:
        assert project_cards_for_company(session, OTHER_COMPANY) == []
        assert project_card(session, OTHER_COMPANY, DOCKET_PROJECT) is None
        assert findings_for_project(session, OTHER_COMPANY, DOCKET_PROJECT) == []
        assert sources_for_project(session, OTHER_COMPANY, DOCKET_PROJECT) == []
        assert threads_for_project(session, OTHER_COMPANY, DOCKET_PROJECT) == []
        assert (
            collective_take_for_project(session, OTHER_COMPANY, DOCKET_PROJECT) is None
        )
        assert deliverables_for_project(session, OTHER_COMPANY, DOCKET_PROJECT) == []
        assert knowledge_for_company(session, OTHER_COMPANY) == []
        assert knowledge_item_for_company(session, OTHER_COMPANY, LESSON) is None

        empty = coverage_for_project(session, OTHER_COMPANY, DOCKET_PROJECT)
        assert empty.findings_total == 0
        assert empty.findings_withheld == 0
