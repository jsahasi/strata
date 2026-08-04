"""Tenant scoping on every read, and the guarantee the product is built on:
a claim whose citation does not verify cannot carry its own text.
"""

from dataclasses import fields

import pytest

from app.ingestion.ingest import ingest_version
from app.state.claims import (
    REASON_CODE_AMBIGUOUS_OCCURRENCE,
    REASON_CODE_LOW_CONFIDENCE,
    REASON_CODE_OUT_OF_RANGE,
    REASON_CODE_QUOTE_MISMATCH,
    REASON_CODE_SOURCE_UNREADABLE,
    VerifiedClaim,
    WithheldClaim,
    change_for_company,
    changes_for_proceeding,
    claims_for_change,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import init_db, session_scope
from app.state.models import Change, Claim, DocumentVersion, Escalation, Proceeding

# The sentence the whole demo turns on. Kept inline so the suite needs no file.
DEFINITION = (
    '"Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW)."
)

SOURCE = "SECTION 2. DEFINITIONS\n\n2.1 " + DEFINITION + "\n"

QUOTE_START = SOURCE.index(DEFINITION)
QUOTE_END = QUOTE_START + len(DEFINITION)

# Same length as the truth, so replacing it moves no offsets. That matters:
# the read-time test must change what the source says, not where it says it.
CORRUPTED = DEFINITION.replace("20 megawatts", "10 megawatts")


def _seed_company(
    session,
    company_id: str,
    *,
    source_text: str = SOURCE,
    quote: str = DEFINITION,
    confidence_bp: int = 9200,
) -> None:
    """One company with a full graph: version, proceeding, change, claim, escalation."""
    prefix = company_id.lower()
    ingest_version(
        session,
        version_id=f"{prefix}-v1",
        company_id=company_id,
        docket=f"{company_id}-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text=source_text,
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
    session.add(
        Claim(
            id=f"{prefix}-claim",
            company_id=company_id,
            change_id=f"{prefix}-chg",
            statement=f"{company_id} threshold sits at 20 MW.",
            citation_version_id=f"{prefix}-v1",
            citation_start=QUOTE_START,
            citation_end=QUOTE_END,
            citation_quote=quote,
            cited_occurrence=None,
            confidence_bp=confidence_bp,
        )
    )
    session.add(
        Escalation(
            id=f"{prefix}-esc",
            company_id=company_id,
            claim_id=f"{prefix}-claim",
            reason_code=REASON_CODE_QUOTE_MISMATCH,
            reason_text="quoted text does not match the source",
            detail=f"{company_id} confidential detail",
        )
    )
    session.flush()


def _seed_two_companies(session) -> None:
    _seed_company(session, "MEP")
    _seed_company(session, "RIVAL")


# --------------------------------------------------------------------------
# Tenant scoping, on every function
# --------------------------------------------------------------------------


def test_each_company_sees_only_its_own_proceedings():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert [p.id for p in proceedings_for_company(session, "MEP")] == ["mep-proc"]
        assert [p.id for p in proceedings_for_company(session, "RIVAL")] == [
            "rival-proc"
        ]
        assert proceedings_for_company(session, "NOT-A-TENANT") == []


def test_changes_do_not_cross_a_tenant_boundary():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert [c.id for c in changes_for_proceeding(session, "MEP", "mep-proc")] == [
            "mep-chg"
        ]
        # Knowing another company's proceeding id must not be enough.
        assert changes_for_proceeding(session, "MEP", "rival-proc") == []
        assert changes_for_proceeding(session, "RIVAL", "mep-proc") == []


def test_a_change_lookup_across_tenants_returns_none_not_the_row():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        assert change_for_company(session, "MEP", "mep-chg").id == "mep-chg"
        assert change_for_company(session, "MEP", "rival-chg") is None
        assert change_for_company(session, "RIVAL", "mep-chg") is None
        assert change_for_company(session, "MEP", "no-such-change") is None


def test_claims_are_scoped_by_company():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        mine = claims_for_change(session, "MEP", "mep-chg")
        assert [c.id for c in mine] == ["mep-claim"]
        assert not any("RIVAL" in c.statement for c in mine)

        assert claims_for_change(session, "MEP", "rival-chg") == []
        assert claims_for_change(session, "RIVAL", "mep-chg") == []


def test_escalations_are_scoped_by_company():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        mine = escalations_for_company(session, "MEP")
        assert [e.id for e in mine] == ["mep-esc"]
        assert not any("RIVAL" in e.detail for e in mine)
        assert escalations_for_company(session, "NOT-A-TENANT") == []


def test_resolved_escalations_can_be_filtered_out_without_being_deleted():
    from datetime import datetime, timezone

    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        escalation = escalations_for_company(session, "MEP")[0]
        escalation.resolved_at = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        escalation.resolved_by = "J. Okonkwo"
        session.flush()

        assert escalations_for_company(session, "MEP", unresolved_only=True) == []
        # Still there for audit. Resolving is a stamp, not a delete.
        assert len(escalations_for_company(session, "MEP")) == 1


def test_verified_claims_are_scoped_by_company():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert [c.claim_id for c in verified] == ["mep-claim"]
        assert not any("RIVAL" in c.statement for c in verified)
        assert withheld == []

        # Another company's change id yields nothing at all, not its claims.
        assert verified_claims(session, "MEP", "rival-chg") == ([], [])


def test_no_read_treats_a_missing_company_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        calls = (
            lambda scope: proceedings_for_company(session, scope),
            lambda scope: changes_for_proceeding(session, scope, "mep-proc"),
            lambda scope: change_for_company(session, scope, "mep-chg"),
            lambda scope: claims_for_change(session, scope, "mep-chg"),
            lambda scope: escalations_for_company(session, scope),
            lambda scope: verified_claims(session, scope, "mep-chg"),
        )
        for call in calls:
            for scope in ("", None, "%"):
                try:
                    result = call(scope)
                except ValueError:
                    continue
                assert result in ([], None, ([], [])), (
                    f"{scope!r} behaved as a wildcard"
                )


# --------------------------------------------------------------------------
# The guarantee: a withheld claim cannot carry what it would have said
# --------------------------------------------------------------------------


def test_a_corrupted_quote_lands_in_the_withheld_list():
    # Correct offsets, invented words -- the failure a model produces fluently.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", quote=CORRUPTED)

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert verified == []
        assert len(withheld) == 1
        assert withheld[0].reason_code == REASON_CODE_QUOTE_MISMATCH
        # The mismatch is shown: what was quoted, against what the source says.
        assert "10 megawatts" in withheld[0].citation_quote
        assert "20 megawatts" in withheld[0].source_excerpt


def test_a_withheld_claim_has_no_statement_field_at_all():
    # Structural, not cosmetic. A template cannot render what does not exist,
    # so no stylesheet change and no stray {{ claim.statement }} can leak an
    # assertion whose citation failed.
    assert "statement" not in {f.name for f in fields(WithheldClaim)}
    assert "statement" in {f.name for f in fields(VerifiedClaim)}

    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", quote=CORRUPTED)
        _verified, withheld = verified_claims(session, "MEP", "mep-chg")
        held = withheld[0]

        assert not hasattr(held, "statement")
        with pytest.raises(AttributeError):
            held.statement

        # slots=True, so there is no instance dict and nothing can be attached
        # later either -- not by a view function, not by a helpful refactor.
        assert "statement" not in WithheldClaim.__slots__
        with pytest.raises(AttributeError):
            held.__dict__
        with pytest.raises((AttributeError, TypeError)):
            held.statement = "the threshold was lowered to 10 MW"
        assert not hasattr(held, "statement")


def test_the_withheld_claims_text_appears_nowhere_in_the_object():
    # The assertion must be absent from every rendering of the object, repr
    # included -- a debug dump into a template is still a leak.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", quote=CORRUPTED)
        stored = claims_for_change(session, "MEP", "mep-chg")[0]
        _verified, withheld = verified_claims(session, "MEP", "mep-chg")

        assert stored.statement == "MEP threshold sits at 20 MW."
        assert stored.statement not in repr(withheld[0])
        assert "threshold sits at" not in repr(withheld[0])


# --------------------------------------------------------------------------
# Verification runs at read time, not at write time
# --------------------------------------------------------------------------


def test_editing_the_source_flips_a_verified_claim_to_withheld():
    # The reason there is no `verified` column. A stored verdict is a promise
    # about bytes that may since have changed.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP")

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert [c.claim_id for c in verified] == ["mep-claim"]
        assert withheld == []

        version = session.get(DocumentVersion, "mep-v1")
        version.source_text = SOURCE.replace("20 megawatts", "30 megawatts")
        session.flush()

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert verified == []
        assert [c.claim_id for c in withheld] == ["mep-claim"]
        assert withheld[0].reason_code == REASON_CODE_QUOTE_MISMATCH
        assert "30 megawatts" in withheld[0].source_excerpt


def test_offsets_past_the_end_of_the_source_withhold_rather_than_raise():
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP")
        claim = claims_for_change(session, "MEP", "mep-chg")[0]
        claim.citation_end = len(SOURCE) + 500
        session.flush()

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert verified == []
        assert withheld[0].reason_code == REASON_CODE_OUT_OF_RANGE
        # No real bytes exist at those offsets, so none are shown.
        assert withheld[0].source_excerpt == ""


def test_a_quote_appearing_twice_must_name_its_occurrence():
    doubled = SOURCE + "\n3.1 " + DEFINITION + "\n"
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", source_text=doubled)

        _verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert withheld[0].reason_code == REASON_CODE_AMBIGUOUS_OCCURRENCE

        claim = claims_for_change(session, "MEP", "mep-chg")[0]
        claim.cited_occurrence = 0
        session.flush()

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert [c.claim_id for c in verified] == ["mep-claim"]
        assert verified[0].cited_occurrence == 0
        assert withheld == []


def test_a_citation_into_another_companys_version_is_withheld_not_read():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        claim = claims_for_change(session, "MEP", "mep-chg")[0]
        claim.citation_version_id = "rival-v1"
        session.flush()

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert verified == []
        assert withheld[0].reason_code == REASON_CODE_SOURCE_UNREADABLE
        assert withheld[0].source_excerpt == ""


def test_low_confidence_withholds_once_a_threshold_is_configured():
    # The threshold ships at 0 -- no evidence yet for where it belongs -- so the
    # mechanism is tested by passing one rather than by shipping a made-up line.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", confidence_bp=4000)

        verified, withheld = verified_claims(session, "MEP", "mep-chg")
        assert [c.claim_id for c in verified] == ["mep-claim"]

        verified, withheld = verified_claims(
            session, "MEP", "mep-chg", min_confidence_bp=8500
        )
        assert verified == []
        assert withheld[0].reason_code == REASON_CODE_LOW_CONFIDENCE
        assert not hasattr(withheld[0], "statement")


def test_a_bad_citation_beats_low_confidence_to_the_reason():
    # The analyst needs to know the quote was wrong, not that a number was low.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP", quote=CORRUPTED, confidence_bp=100)

        _verified, withheld = verified_claims(
            session, "MEP", "mep-chg", min_confidence_bp=9000
        )
        assert withheld[0].reason_code == REASON_CODE_QUOTE_MISMATCH


def test_confidence_is_stored_as_an_integer_in_basis_points():
    # A float would render differently across platforms and break any hash
    # taken over the row.
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP")
        claim = claims_for_change(session, "MEP", "mep-chg")[0]
        assert isinstance(claim.confidence_bp, int)
        assert claim.confidence_bp == 9200


def test_verified_claims_reads_the_source_and_does_not_write():
    init_db()
    with session_scope() as session:
        _seed_company(session, "MEP")
        before = len(escalations_for_company(session, "MEP"))

        verified, _withheld = verified_claims(session, "MEP", "mep-chg")
        # actual_text is the source's bytes, not the quote echoed back.
        assert verified[0].actual_text == SOURCE[QUOTE_START:QUOTE_END]
        assert len(escalations_for_company(session, "MEP")) == before
