"""The chokepoint: ordering, idempotency, and what it refuses to do.

The interesting tests here are the refusals. A loader that quietly does the
wrong thing on the second run is the defect this module exists to prevent, and
it is invisible in a screenshot.
"""

import pytest

from app.pipeline import ACTOR, CorpusChanged, change_id, ingest_and_diff
from app.state.audit import event_count, verify_chain
from app.state.claims import changes_for_proceeding
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent, Proceeding

V1 = (
    "SECTION 2. DEFINITIONS\n\n"
    '2.1 "Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW).\n\n"
    "5.2 Allocation. The Utility shall allocate 100% of Network Upgrade costs.\n\n"
    "7.1 Updated Load Forecasts. Submit no later than March 1, 2027.\n"
)

V2 = (
    "SECTION 2. DEFINITIONS\n\n"
    '2.1 "Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW).\n\n"
    "5.2 Allocation. The Utility shall allocate 50% of Network Upgrade costs.\n\n"
    "7.1 Updated Load Forecasts. Submit no later than June 1, 2027.\n"
)

# One paragraph struck and nothing put in its place.
V2_SHORTER = (
    "SECTION 2. DEFINITIONS\n\n"
    '2.1 "Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW).\n\n"
    "5.2 Allocation. The Utility shall allocate 100% of Network Upgrade costs.\n"
)


def _proceeding(session, company_id: str = "MEP", proceeding_id: str = "DKT-1"):
    session.add(
        Proceeding(
            id=proceeding_id,
            company_id=company_id,
            docket=proceeding_id,
            commission="Meridian Public Utilities Commission",
            subject="Large load interconnection",
        )
    )
    session.flush()
    return proceeding_id


def _load(session, version_id, text, *, previous=None, status="DRAFT",
          company_id="MEP", proceeding_id="DKT-1"):
    return ingest_and_diff(
        session,
        company_id=company_id,
        proceeding_id=proceeding_id,
        version_id=version_id,
        label=f"Version {version_id}",
        status=status,
        source_text=text,
        previous_version_id=previous,
    )


# --------------------------------------------------------------------------
# The happy path, and what lands in the row
# --------------------------------------------------------------------------


def test_the_first_version_has_nothing_to_diff_against():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        assert _load(session, "v1", V1) == []
        # Ingested all the same. An empty change list is not a skipped version.
        assert event_count(session, "MEP") == 1


def test_a_second_version_produces_persisted_changes():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")

        assert rows, "two different versions must produce at least one change"
        assert [row.id for row in rows] == [
            change_id("v1", "v2", n) for n in range(len(rows))
        ]
        for row in rows:
            assert row.company_id == "MEP"
            assert row.proceeding_id == "DKT-1"
            assert row.from_version_id == "v1"
            assert row.to_version_id == "v2"
            assert row.change_type in ("added", "removed", "modified")


def test_a_change_carries_the_status_of_the_version_it_came_from():
    # ADR-005: copied at write time, never inferred at read time. A change found
    # in a final order stays final even if the version row is later corrected.
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1, status="DRAFT")
        rows = _load(session, "v2", V2, previous="v1", status="FINAL")
        assert {row.status for row in rows} == {"FINAL"}


def test_materiality_stays_null_because_no_model_call_exists():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")
        assert all(row.materiality is None for row in rows)


def test_a_modified_change_carries_offsets_into_both_versions():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")

        modified = [row for row in rows if row.change_type == "modified"]
        assert modified
        for row in modified:
            assert V1[row.before_start:row.before_end] in V1
            assert V2[row.after_start:row.after_end] in V2
        # The allocation edit is found where the text really differs.
        assert any(
            "100%" in V1[row.before_start:row.before_end]
            and "50%" in V2[row.after_start:row.after_end]
            for row in modified
        )


def test_a_removal_has_no_after_side_and_cites_what_was_struck():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2_SHORTER, previous="v1")

        removed = [row for row in rows if row.change_type == "removed"]
        assert removed, "a struck paragraph must be reported, never dropped"
        row = removed[0]
        assert row.after_start is None and row.after_end is None
        assert "March 1, 2027" in V1[row.before_start:row.before_end]

        event = (
            session.query(AuditEvent)
            .filter(AuditEvent.subject_id == row.id)
            .one()
        )
        assert event.citation.startswith("v1:")


def test_a_change_records_the_section_it_sits_under():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")
        assert {row.section for row in rows} & {"5.2", "7.1"}


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------


def test_every_version_and_every_change_is_audited():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")

        # Two versions plus one entry per change.
        assert event_count(session, "MEP") == 2 + len(rows)
        assert verify_chain(session, "MEP") is True
        assert {
            event.actor for event in session.query(AuditEvent).all()
        } == {ACTOR}


def test_the_audit_entry_for_a_change_says_where_it_happened():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        rows = _load(session, "v2", V2, previous="v1")

        row = rows[0]
        event = (
            session.query(AuditEvent)
            .filter(AuditEvent.subject_id == row.id)
            .one()
        )
        version_id, start, end = event.citation.split(":")
        assert version_id == "v2"
        assert V2[int(start):int(end)] == V2[row.after_start:row.after_end]


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_loading_the_same_version_twice_writes_nothing_the_second_time():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        first = _load(session, "v2", V2, previous="v1")
        events = event_count(session, "MEP")

        again = _load(session, "v2", V2, previous="v1")

        assert [row.id for row in again] == [row.id for row in first]
        assert len(changes_for_proceeding(session, "MEP", "DKT-1")) == len(first)
        assert event_count(session, "MEP") == events


def test_idempotency_holds_across_separate_sessions():
    # The real shape of the risk: `make run` seeds in a new process every time.
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)
        _load(session, "v2", V2, previous="v1")

    with session_scope() as session:
        before = len(changes_for_proceeding(session, "MEP", "DKT-1"))
        events = event_count(session, "MEP")

    with session_scope() as session:
        _load(session, "v1", V1)
        _load(session, "v2", V2, previous="v1")

    with session_scope() as session:
        assert len(changes_for_proceeding(session, "MEP", "DKT-1")) == before
        assert event_count(session, "MEP") == events
        assert verify_chain(session, "MEP") is True


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_the_same_version_id_with_different_bytes_is_refused():
    # best-practices.html §27. Every offset already stored addresses the old
    # text; a partial rebuild answers every question plausibly and wrongly.
    init_db()
    with session_scope() as session:
        _proceeding(session)
        _load(session, "v1", V1)

        with pytest.raises(CorpusChanged) as raised:
            _load(session, "v1", V1.replace("20 megawatts", "30 megawatts"))

        message = str(raised.value)
        assert "v1" in message
        assert "reload" in message or "Delete" in message


def test_a_version_cannot_be_loaded_into_a_proceeding_that_does_not_exist():
    init_db()
    with session_scope() as session:
        with pytest.raises(ValueError, match="no proceeding"):
            _load(session, "v1", V1)


def test_a_proceeding_belonging_to_another_company_is_not_visible():
    init_db()
    with session_scope() as session:
        _proceeding(session, company_id="RIVAL", proceeding_id="DKT-1")
        with pytest.raises(ValueError, match="no proceeding"):
            _load(session, "v1", V1, company_id="MEP")


def test_an_unscoped_load_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        _proceeding(session)
        with pytest.raises(ValueError, match="company_id is required"):
            _load(session, "v1", V1, company_id="")


def test_diffing_against_a_version_that_is_not_stored_is_refused():
    # Absence is denial. Silently returning no changes would read as "nothing
    # moved between these two versions", which is a different and false claim.
    init_db()
    with session_scope() as session:
        _proceeding(session)
        with pytest.raises(ValueError, match="previous version"):
            _load(session, "v2", V2, previous="v1")


def test_changes_written_by_the_pipeline_stay_inside_their_tenant():
    init_db()
    with session_scope() as session:
        _proceeding(session, company_id="MEP", proceeding_id="MEP-DKT")
        _proceeding(session, company_id="RIVAL", proceeding_id="RIVAL-DKT")

        _load(session, "mep-v1", V1, company_id="MEP", proceeding_id="MEP-DKT")
        mine = _load(
            session,
            "mep-v2",
            V2,
            previous="mep-v1",
            company_id="MEP",
            proceeding_id="MEP-DKT",
        )
        _load(session, "rival-v1", V1, company_id="RIVAL", proceeding_id="RIVAL-DKT")
        _load(
            session,
            "rival-v2",
            V2,
            previous="rival-v1",
            company_id="RIVAL",
            proceeding_id="RIVAL-DKT",
        )

        assert changes_for_proceeding(session, "MEP", "RIVAL-DKT") == []
        ours = changes_for_proceeding(session, "MEP", "MEP-DKT")
        assert [row.id for row in ours] == [row.id for row in mine]
        # Separate chains, so neither company's log reveals the other's volume.
        assert event_count(session, "MEP") == 2 + len(mine)
        assert verify_chain(session, "RIVAL") is True
