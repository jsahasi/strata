"""The review centre: tenant scoping on every read, and the rule the synthesis
rests on -- a take states what it excluded, or it does not get written.

Two things these tests deliberately do not assume. They do not assume a stored
verdict: the coverage tests edit the source under a finding and expect the
count to move on the next read. And they do not assume good faith in the
composer: the guard that refuses a silent take is called directly, with counts
a future edit might pass, because a check that can only be exercised through
the happy path is a check nobody has tested.
"""

from datetime import datetime, timezone

import pytest

from app.ingestion.ingest import ingest_version
from app.state.audit import event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.models import (
    AuditEvent,
    Change,
    Claim,
    CollectiveTake,
    Deliverable,
    DocumentVersion,
    Finding,
    Proceeding,
    Project,
    Question,
    Source,
    SteerDirective,
)
from app.state.review import (
    ACTION_STEER_ISSUED,
    ACTION_STEER_REVOKED,
    ACTION_TAKE_COMPOSED,
    Coverage,
    _refuse_a_silent_take,
    collective_take_for_project,
    compose_take,
    coverage_for_project,
    deliverables_for_project,
    findings_for_project,
    issue_steer,
    open_questions,
    questions_for_project,
    revoke_steer,
    sources_for_project,
    steer_directives_for_project,
)

# The sentence the demo turns on. Inline, so the suite needs no file.
DEFINITION = (
    '"Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW)."
)

SOURCE = "SECTION 2. DEFINITIONS\n\n2.1 " + DEFINITION + "\n"

QUOTE_START = SOURCE.index(DEFINITION)
QUOTE_END = QUOTE_START + len(DEFINITION)

# Same length as the truth, so swapping it moves no offsets. That matters: the
# read-time test has to change what the source says, not where it says it.
CORRUPTED = DEFINITION.replace("20 megawatts", "10 megawatts")

# A third wording, for the test that edits the source underneath a finding.
# It has to match neither quote. Rewriting the source to the misquoted text
# would only swap which claim verifies, and the counts would not move -- which
# is what the first draft of that test asserted, wrongly.
MOVED = DEFINITION.replace("20 megawatts", "30 megawatts")
MOVED_SOURCE = SOURCE.replace(DEFINITION, MOVED)

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)


def _seed(session, company_id: str, project_id: str) -> None:
    """One company, one project, and a full review centre hanging off it.

    Three findings on purpose: one whose claim verifies, one whose claim
    misquotes the source, and one a person raised with no claim at all. That is
    the whole coverage story in three rows -- one included, two withheld, for
    two different reasons.
    """
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
        Project(
            id=project_id,
            company_id=company_id,
            name=f"{company_id} large load interconnection",
            jurisdiction="Meridian",
            status="active",
            owner=f"analyst@{prefix}.example",
        )
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
    session.add(
        Change(
            id=f"{prefix}-chg",
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
    for suffix, quote in (("ok", DEFINITION), ("bad", CORRUPTED)):
        session.add(
            Claim(
                id=f"{prefix}-claim-{suffix}",
                company_id=company_id,
                change_id=f"{prefix}-chg",
                statement=f"{company_id} threshold sits at 20 MW.",
                citation_version_id=f"{prefix}-v1",
                citation_start=QUOTE_START,
                citation_end=QUOTE_END,
                citation_quote=quote,
                cited_occurrence=None,
                confidence_bp=9200,
            )
        )

    session.add(
        Source(
            id=f"{prefix}-src-internal",
            company_id=company_id,
            project_id=project_id,
            kind="internal",
            label=f"{company_id} obligation register",
            locator=f"file:///{prefix}/obligations.json",
            version_id=None,
            retrieved_at=T0,
            trusted=True,
        )
    )
    session.add(
        Source(
            id=f"{prefix}-src-external",
            company_id=company_id,
            project_id=project_id,
            kind="external",
            label="Docket 2026-0142, NOPR",
            locator=f"{company_id}-2026-0142",
            version_id=f"{prefix}-v1",
            retrieved_at=T0,
            trusted=False,
        )
    )

    session.add(
        Finding(
            id=f"{prefix}-find-ok",
            company_id=company_id,
            project_id=project_id,
            change_id=f"{prefix}-chg",
            claim_id=f"{prefix}-claim-ok",
            headline="The 20 MW threshold is unchanged in this draft.",
            detail=f"{company_id} confidential detail",
            raised_by="system",
            raised_at=T0,
            status="open",
        )
    )
    session.add(
        Finding(
            id=f"{prefix}-find-bad",
            company_id=company_id,
            project_id=project_id,
            change_id=f"{prefix}-chg",
            claim_id=f"{prefix}-claim-bad",
            headline="The threshold moved to 10 MW.",
            detail="",
            raised_by="system",
            raised_at=T1,
            status="open",
        )
    )
    session.add(
        Finding(
            id=f"{prefix}-find-human",
            company_id=company_id,
            project_id=project_id,
            change_id=None,
            claim_id=None,
            headline="Counsel thinks the collateral clause is the real exposure.",
            detail="",
            raised_by=f"analyst@{prefix}.example",
            raised_at=T2,
            status="open",
        )
    )

    session.add(
        Question(
            id=f"{prefix}-q-open",
            company_id=company_id,
            project_id=project_id,
            body="Does the study deadline run from filing or from acceptance?",
            asked_by=f"analyst@{prefix}.example",
            asked_at=T0,
            blocking=False,
        )
    )
    session.add(
        Question(
            id=f"{prefix}-q-blocking",
            company_id=company_id,
            project_id=project_id,
            body="Is the collateral requirement retroactive?",
            asked_by=f"analyst@{prefix}.example",
            asked_at=T1,
            blocking=True,
        )
    )
    session.add(
        Question(
            id=f"{prefix}-q-answered",
            company_id=company_id,
            project_id=project_id,
            body="Which commission issued this?",
            asked_by=f"analyst@{prefix}.example",
            asked_at=T2,
            answered_at=T2,
            answer="Meridian.",
            answered_by=f"counsel@{prefix}.example",
            blocking=True,
        )
    )

    session.add(
        Deliverable(
            id=f"{prefix}-del-1",
            company_id=company_id,
            project_id=project_id,
            title=f"{company_id} comment memo",
            kind="memo",
            body="",
            state="draft",
            created_at=T0,
            approved_by=None,
            findings_included=1,
            findings_withheld=2,
        )
    )
    session.flush()


def _seed_two_companies(session) -> None:
    _seed(session, "MEP", "MEP-PRJ-1")
    _seed(session, "RIVAL", "RIVAL-PRJ-1")


# --------------------------------------------------------------------------
# Tenant scoping, on every read
# --------------------------------------------------------------------------

_PROJECT_READS = (
    sources_for_project,
    findings_for_project,
    questions_for_project,
    open_questions,
    steer_directives_for_project,
    deliverables_for_project,
    collective_take_for_project,
    coverage_for_project,
)


def test_every_read_refuses_a_call_missing_either_scope():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        for read in _PROJECT_READS:
            with pytest.raises(ValueError, match="company_id is required"):
                read(session, "", "MEP-PRJ-1")
            with pytest.raises(ValueError, match="project_id is required"):
                read(session, "MEP", "")


def test_sources_do_not_cross_a_tenant_boundary():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert [s.id for s in sources_for_project(session, "MEP", "MEP-PRJ-1")] == [
            "mep-src-external",
            "mep-src-internal",
        ]
        # Knowing another company's project id must not be enough.
        assert sources_for_project(session, "MEP", "RIVAL-PRJ-1") == []
        assert sources_for_project(session, "RIVAL", "MEP-PRJ-1") == []


def test_findings_do_not_cross_a_tenant_boundary():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        mine = findings_for_project(session, "MEP", "MEP-PRJ-1")
        assert [f.id for f in mine] == [
            "mep-find-bad",
            "mep-find-human",
            "mep-find-ok",
        ]
        assert findings_for_project(session, "MEP", "RIVAL-PRJ-1") == []
        assert findings_for_project(session, "RIVAL", "MEP-PRJ-1") == []


def test_questions_and_deliverables_and_takes_are_scoped():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        compose_take(session, "RIVAL", "RIVAL-PRJ-1", "Rival view.", "rival@example")

        assert len(questions_for_project(session, "MEP", "MEP-PRJ-1")) == 3
        assert questions_for_project(session, "MEP", "RIVAL-PRJ-1") == []

        mine = deliverables_for_project(session, "MEP", "MEP-PRJ-1")
        assert [d.id for d in mine] == ["mep-del-1"]
        assert deliverables_for_project(session, "MEP", "RIVAL-PRJ-1") == []

        # A take exists, and it belongs to exactly one tenant.
        assert collective_take_for_project(session, "RIVAL", "RIVAL-PRJ-1") is not None
        assert collective_take_for_project(session, "MEP", "RIVAL-PRJ-1") is None
        assert collective_take_for_project(session, "MEP", "MEP-PRJ-1") is None


def test_open_questions_puts_blocking_first_and_drops_the_answered_one():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert [q.id for q in open_questions(session, "MEP", "MEP-PRJ-1")] == [
            "mep-q-blocking",
            "mep-q-open",
        ]
        # The answered one is blocking too, and answering it stopped it blocking.
        assert [q.id for q in questions_for_project(session, "MEP", "MEP-PRJ-1")] == [
            "mep-q-open",
            "mep-q-blocking",
            "mep-q-answered",
        ]


# --------------------------------------------------------------------------
# Coverage -- counted from the rows, every time
# --------------------------------------------------------------------------


def test_coverage_counts_match_the_rows():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        coverage = coverage_for_project(session, "MEP", "MEP-PRJ-1")
        assert coverage == Coverage(
            findings_total=3,
            findings_verified=1,
            findings_withheld=2,
            claims_verified=1,
            claims_withheld=1,
            open_questions=2,
            blocking_questions=1,
            sources_internal=1,
            sources_external=1,
        )


def test_verified_plus_withheld_always_equals_the_total():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        for company_id, project_id in (
            ("MEP", "MEP-PRJ-1"),
            ("RIVAL", "RIVAL-PRJ-1"),
        ):
            coverage = coverage_for_project(session, company_id, project_id)
            assert (
                coverage.findings_verified + coverage.findings_withheld
                == coverage.findings_total
            )


def test_coverage_for_another_companys_project_counts_nothing():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        coverage = coverage_for_project(session, "MEP", "RIVAL-PRJ-1")
        assert coverage == Coverage(
            findings_total=0,
            findings_verified=0,
            findings_withheld=0,
            claims_verified=0,
            claims_withheld=0,
            open_questions=0,
            blocking_questions=0,
            sources_internal=0,
            sources_external=0,
        )


def test_a_finding_falls_out_of_coverage_when_its_source_changes():
    """The verdict is recomputed, never remembered.

    Nothing about the finding or its claim is touched here. The bytes under the
    citation move, and the next read says so.
    """
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        assert coverage_for_project(session, "MEP", "MEP-PRJ-1").findings_verified == 1

        version = session.get(DocumentVersion, "mep-v1")
        version.source_text = MOVED_SOURCE
        session.flush()

        after = coverage_for_project(session, "MEP", "MEP-PRJ-1")
        assert after.findings_verified == 0
        assert after.findings_withheld == 3
        assert (after.claims_verified, after.claims_withheld) == (0, 2)
        # The other company's source did not move, so its count did not either.
        theirs = coverage_for_project(session, "RIVAL", "RIVAL-PRJ-1")
        assert theirs.findings_verified == 1


def test_a_finding_pointing_at_another_companys_claim_is_not_verified():
    """A claim id from another tenant buys nothing -- not even a count."""
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.add(
            Finding(
                id="mep-find-borrowed",
                company_id="MEP",
                project_id="MEP-PRJ-1",
                change_id=None,
                claim_id="rival-claim-ok",
                headline="Borrowed from the neighbour.",
                detail="",
                raised_by="analyst@mep.example",
                raised_at=T2,
                status="open",
            )
        )
        session.flush()

        coverage = coverage_for_project(session, "MEP", "MEP-PRJ-1")
        assert coverage.findings_total == 4
        assert coverage.findings_verified == 1
        assert coverage.findings_withheld == 3
        # The neighbour's change was never opened, so its claims were not counted.
        assert coverage.claims_verified == 1
        assert coverage.claims_withheld == 1


# --------------------------------------------------------------------------
# Steer -- a directive is a row and an audit event, or it is neither
# --------------------------------------------------------------------------


def test_issuing_a_steer_writes_an_audit_event_and_the_chain_still_verifies():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        before = event_count(session, "MEP")

        directive = issue_steer(
            session,
            "MEP",
            "MEP-PRJ-1",
            "Track collateral and cost allocation only.",
            "analyst@mep.example",
        )

        assert event_count(session, "MEP") == before + 1
        event = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == "MEP")
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert event.action == ACTION_STEER_ISSUED
        assert event.subject_type == "steer_directive"
        assert event.subject_id == directive.id
        assert "collateral" in event.reason
        assert event.actor == "analyst@mep.example"

        assert verify_chain(session, "MEP") is True
        # A steer for one tenant leaves the other's chain alone.
        assert event_count(session, "RIVAL") == 0


def test_a_steer_with_no_issuer_or_no_instruction_is_refused():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        with pytest.raises(ValueError, match="instruction"):
            issue_steer(session, "MEP", "MEP-PRJ-1", "   ", "analyst@mep.example")
        with pytest.raises(ValueError, match="issuer"):
            issue_steer(session, "MEP", "MEP-PRJ-1", "Track collateral.", "")

        assert steer_directives_for_project(session, "MEP", "MEP-PRJ-1") == []
        assert event_count(session, "MEP") == 0


def test_a_revoked_steer_leaves_the_active_list_and_stays_readable():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        kept = issue_steer(
            session, "MEP", "MEP-PRJ-1", "Track collateral.", "analyst@mep.example"
        )
        dropped = issue_steer(
            session, "MEP", "MEP-PRJ-1", "Ignore rate design.", "analyst@mep.example"
        )
        before = steer_directives_for_project(session, "MEP", "MEP-PRJ-1")
        assert [d.id for d in before] == [kept.id, dropped.id]

        revoke_steer(session, "MEP", dropped.id, "lead@mep.example")

        active = steer_directives_for_project(session, "MEP", "MEP-PRJ-1")
        assert [d.id for d in active] == [kept.id]

        everything = steer_directives_for_project(
            session, "MEP", "MEP-PRJ-1", include_revoked=True
        )
        assert [d.id for d in everything] == [kept.id, dropped.id]

        readable = session.get(SteerDirective, dropped.id)
        assert readable.instruction == "Ignore rate design."
        assert readable.revoked_at is not None

        last = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == "MEP")
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert last.action == ACTION_STEER_REVOKED
        assert last.actor == "lead@mep.example"
        assert verify_chain(session, "MEP") is True


def test_another_companys_steer_cannot_be_revoked():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        theirs = issue_steer(
            session, "RIVAL", "RIVAL-PRJ-1", "Track everything.", "rival@example"
        )

        with pytest.raises(ValueError, match="no such steer directive"):
            revoke_steer(session, "MEP", theirs.id, "analyst@mep.example")

        assert session.get(SteerDirective, theirs.id).revoked_at is None


# --------------------------------------------------------------------------
# The collective take -- it says what it left out, or it is not written
# --------------------------------------------------------------------------


def test_a_composed_take_carries_a_non_zero_withheld_count():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        take = compose_take(
            session,
            "MEP",
            "MEP-PRJ-1",
            "The threshold holds at 20 MW in this draft.",
            "analyst@mep.example",
        )

        coverage = coverage_for_project(session, "MEP", "MEP-PRJ-1")
        assert take.findings_included == coverage.findings_verified == 1
        assert take.findings_withheld == coverage.findings_withheld == 2
        assert take.composed_by == "analyst@mep.example"

        stored = collective_take_for_project(session, "MEP", "MEP-PRJ-1")
        assert stored.id == take.id
        assert stored.findings_withheld == 2


def test_composing_a_take_is_audited_with_what_it_excluded():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        take = compose_take(
            session, "MEP", "MEP-PRJ-1", "Holding at 20 MW.", "analyst@mep.example"
        )

        event = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == "MEP")
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert event.action == ACTION_TAKE_COMPOSED
        assert event.subject_id == take.id
        assert "2 withheld" in event.reason
        assert verify_chain(session, "MEP") is True


def test_a_take_that_hides_its_exclusions_is_refused():
    """The guard is called directly, with the counts a careless edit would pass.

    compose_take() cannot produce this, because it passes coverage's own
    numbers. That is exactly why the check is tested here rather than through
    it: the day someone hands it a hand-rolled zero, this is what stops the
    take being written.
    """
    coverage = Coverage(
        findings_total=6,
        findings_verified=4,
        findings_withheld=2,
        claims_verified=4,
        claims_withheld=3,
        open_questions=1,
        blocking_questions=1,
        sources_internal=2,
        sources_external=2,
    )

    with pytest.raises(ValueError, match="must state what it excluded"):
        _refuse_a_silent_take(coverage, findings_included=4, findings_withheld=0)

    with pytest.raises(ValueError, match="must carry the coverage"):
        _refuse_a_silent_take(coverage, findings_included=6, findings_withheld=2)

    # The honest counts pass, and nothing else does.
    _refuse_a_silent_take(coverage, findings_included=4, findings_withheld=2)


def test_a_take_with_no_body_or_no_composer_is_refused():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        with pytest.raises(ValueError, match="needs a body"):
            compose_take(session, "MEP", "MEP-PRJ-1", "   ", "analyst@mep.example")
        with pytest.raises(ValueError, match="needs a composer"):
            compose_take(session, "MEP", "MEP-PRJ-1", "Holding at 20 MW.", "")

        assert collective_take_for_project(session, "MEP", "MEP-PRJ-1") is None
        assert event_count(session, "MEP") == 0


def test_composing_again_supersedes_the_earlier_take_without_deleting_it():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        first = compose_take(
            session, "MEP", "MEP-PRJ-1", "Holding at 20 MW.", "analyst@mep.example"
        )
        second = compose_take(
            session,
            "MEP",
            "MEP-PRJ-1",
            "Revised: collateral is the exposure.",
            "lead@mep.example",
        )

        assert collective_take_for_project(session, "MEP", "MEP-PRJ-1").id == second.id
        assert session.get(CollectiveTake, first.id).superseded_by == second.id
        assert session.get(CollectiveTake, first.id).body == "Holding at 20 MW."
        assert session.query(CollectiveTake).count() == 2
