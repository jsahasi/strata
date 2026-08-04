"""The seed, against the real corpus in data/.

Two things are asserted here that no screenshot can show. First, that running
the loader twice leaves the database exactly as it was -- `make run` seeds on
every start. Second, that exactly two claims fail verification and for the two
reasons the corpus was built to produce, so the demo of ADR-003 is a property of
the data rather than a screen someone drew.
"""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.seed import (
    CLAIM_AMBIGUOUS,
    CLAIM_MISQUOTE,
    load,
    statement_for,
)
from app.state.audit import event_count, verify_chain
from app.state.claims import (
    REASON_CODE_AMBIGUOUS_OCCURRENCE,
    REASON_CODE_QUOTE_MISMATCH,
    changes_for_proceeding,
    claims_for_change,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent
from app.state.queries import versions_for_company
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
)

COMPANY = "MEP"
PROCEEDING = "MPUC-2026-0142"

# The passage-level diff over the three files. More than the five changes the
# manifest labels, because the manifest labels the changes worth an analyst's
# attention while the diff reports every paragraph that differs -- the caption,
# the status line, the renumbered headings. Pinned as a golden number: it is the
# first thing to move if segmentation or alignment changes, and a silent change
# there would move every citation offset in the product.
EXPECTED_CHANGES = 27


def _manifest(data_dir):
    return json.loads((data_dir / "manifest.json").read_bytes().decode("utf-8"))


def _seed():
    with session_scope() as session:
        return load(session)


def _all_claims(session):
    claims = []
    for change in changes_for_proceeding(session, COMPANY, PROCEEDING):
        claims += claims_for_change(session, COMPANY, change.id)
    return claims


def _split(session):
    """Every claim in the proceeding, split by whether it may be asserted."""
    verified, withheld = [], []
    for change in changes_for_proceeding(session, COMPANY, PROCEEDING):
        good, held = verified_claims(session, COMPANY, change.id)
        verified += good
        withheld += held
    return verified, withheld


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_seeding_twice_produces_the_same_counts():
    init_db()
    first = _seed()
    second = _seed()

    assert first == second
    assert first.company_id == COMPANY
    assert first.proceeding_id == PROCEEDING
    assert first.versions == 3
    assert first.changes == EXPECTED_CHANGES


def test_seeding_twice_writes_no_second_audit_entry():
    init_db()
    _seed()
    with session_scope() as session:
        events = event_count(session, COMPANY)

    _seed()
    with session_scope() as session:
        assert event_count(session, COMPANY) == events
        assert verify_chain(session, COMPANY) is True


def test_the_second_run_does_not_duplicate_a_single_row():
    init_db()
    _seed()
    with session_scope() as session:
        versions = [v.id for v in versions_for_company(session, COMPANY)]
        changes = [c.id for c in changes_for_proceeding(session, COMPANY, PROCEEDING)]
        claims = [c.id for c in _all_claims(session)]
        escalations = [e.id for e in escalations_for_company(session, COMPANY)]

    _seed()
    with session_scope() as session:
        assert [v.id for v in versions_for_company(session, COMPANY)] == versions
        assert [
            c.id for c in changes_for_proceeding(session, COMPANY, PROCEEDING)
        ] == changes
        assert [c.id for c in _all_claims(session)] == claims
        assert [e.id for e in escalations_for_company(session, COMPANY)] == escalations
        assert len(set(claims)) == len(claims)


# --------------------------------------------------------------------------
# The corpus produces the changes the manifest says it does
# --------------------------------------------------------------------------


def test_the_three_versions_load_with_their_stated_status(data_dir):
    init_db()
    _seed()
    manifest = _manifest(data_dir)
    with session_scope() as session:
        stored = {v.id: v for v in versions_for_company(session, COMPANY)}

    assert set(stored) == {"v1", "v2", "v3"}
    for version in manifest["versions"]:
        assert stored[version["id"]].status == version["status"]
        assert stored[version["id"]].label == version["label"]
    # ADR-005 again: only the final order is final.
    assert [v for v in stored.values() if v.status == "FINAL"] == [stored["v3"]]


def test_every_labelled_change_in_the_manifest_is_found_by_the_diff(data_dir):
    init_db()
    _seed()
    manifest = _manifest(data_dir)

    with session_scope() as session:
        rows = changes_for_proceeding(session, COMPANY, PROCEEDING)
        assert len(rows) == EXPECTED_CHANGES

        for labelled in manifest["changes"]:
            after = labelled["after"]
            covering = [
                row
                for row in rows
                if row.to_version_id == after["version"]
                and row.after_start is not None
                and row.after_start < after["end"]
                and after["start"] < row.after_end
            ]
            assert covering, f"{labelled['id']} was not reported by the diff"


def test_the_restructure_and_the_new_final_section_both_escalate_on_confidence(
    data_dir,
):
    # ADR-004's disclosed limit, visible in the data. Section 6 becoming 5.4 and
    # a section with no draft-stage counterpart are exactly what sequence
    # alignment gets wrong, so both must come back with low confidence rather
    # than as settled matches.
    init_db()
    _seed()
    manifest = _manifest(data_dir)

    with session_scope() as session:
        rows = changes_for_proceeding(session, COMPANY, PROCEEDING)
        for label in ("CHG-4", "CHG-5"):
            labelled = next(c for c in manifest["changes"] if c["id"] == label)
            after = labelled["after"]
            covering = [
                row
                for row in rows
                if row.to_version_id == after["version"]
                and row.after_start is not None
                and row.after_start < after["end"]
                and after["start"] < row.after_end
            ]
            assert min(row.alignment_confidence for row in covering) <= 0.5


def test_the_proceeding_comes_from_the_manifest(data_dir):
    init_db()
    _seed()
    manifest = _manifest(data_dir)
    with session_scope() as session:
        proceeding = proceedings_for_company(session, COMPANY)[0]

    assert proceeding.id == PROCEEDING
    assert proceeding.docket == PROCEEDING
    assert proceeding.subject == manifest["docket"]["subject"]
    assert proceeding.commission == "Meridian Public Utilities Commission (MPUC)"


# --------------------------------------------------------------------------
# Exactly two claims fail, and for the two reasons the corpus was built for
# --------------------------------------------------------------------------


def test_exactly_two_claims_fail_verification():
    init_db()
    report = _seed()

    with session_scope() as session:
        verified, withheld = _split(session)

    assert len(withheld) == 2
    assert sorted(held.claim_id for held in withheld) == sorted(
        [CLAIM_AMBIGUOUS, CLAIM_MISQUOTE]
    )
    assert len(verified) == report.claims - 2
    assert verified, "a demo where nothing verifies proves nothing either"


def test_the_two_failures_are_a_misquote_and_an_unnumbered_occurrence():
    init_db()
    _seed()

    with session_scope() as session:
        _verified, withheld = _split(session)
        by_id = {held.claim_id: held for held in withheld}

    assert by_id[CLAIM_MISQUOTE].reason_code == REASON_CODE_QUOTE_MISMATCH
    # The quote was altered where the source was not: same offsets, same length.
    assert "10 megawatts" in by_id[CLAIM_MISQUOTE].citation_quote
    assert "20 megawatts" in by_id[CLAIM_MISQUOTE].source_excerpt

    assert by_id[CLAIM_AMBIGUOUS].reason_code == REASON_CODE_AMBIGUOUS_OCCURRENCE
    assert by_id[CLAIM_AMBIGUOUS].cited_occurrence is None
    # The words really are there. Text equality alone would have passed it.
    ambiguous = by_id[CLAIM_AMBIGUOUS]
    assert ambiguous.citation_quote == ambiguous.source_excerpt


def test_a_withheld_claim_carries_no_statement_all_the_way_from_the_seed():
    # ADR-003, end to end: the sentence is stored, and the read path cannot
    # hand it to anything that renders.
    init_db()
    _seed()

    with session_scope() as session:
        stored = {claim.id: claim.statement for claim in _all_claims(session)}
        _verified, withheld = _split(session)

    assert "10 megawatts" in stored[CLAIM_MISQUOTE]
    for held in withheld:
        assert not hasattr(held, "statement")
        assert stored[held.claim_id] not in repr(held)


def test_every_passing_claim_verifies_against_the_stored_source():
    init_db()
    _seed()

    with session_scope() as session:
        sources = {v.id: v.source_text for v in versions_for_company(session, COMPANY)}
        verified, _withheld = _split(session)

        assert len(verified) == 5
        for claim in verified:
            source = sources[claim.citation_version_id]
            assert (
                source[claim.citation_start:claim.citation_end] == claim.citation_quote
            )
            # actual_text is read from the source, not echoed from the claim.
            assert claim.actual_text == source[claim.citation_start:claim.citation_end]
            assert claim.statement


def test_each_labelled_change_is_cited_at_the_manifests_own_offsets(data_dir):
    init_db()
    _seed()
    manifest = _manifest(data_dir)

    with session_scope() as session:
        claims = {claim.id: claim for claim in _all_claims(session)}

    for labelled in manifest["changes"]:
        claim = claims[f"CLM-{labelled['id']}"]
        after = labelled["after"]
        assert claim.citation_version_id == after["version"]
        assert claim.citation_start == after["start"]
        assert claim.citation_end == after["end"]
        assert claim.citation_quote == after["exact_text"]


# --------------------------------------------------------------------------
# Escalations
# --------------------------------------------------------------------------


def test_an_escalation_is_raised_for_each_failure_in_the_verifiers_own_words():
    init_db()
    report = _seed()

    with session_scope() as session:
        escalations = {e.claim_id: e for e in escalations_for_company(session, COMPANY)}

    assert set(escalations) == {CLAIM_MISQUOTE, CLAIM_AMBIGUOUS}
    assert report.escalations == 2
    assert escalations[CLAIM_MISQUOTE].reason_text == REASON_QUOTE_MISMATCH
    assert escalations[CLAIM_AMBIGUOUS].reason_text == REASON_AMBIGUOUS_OCCURRENCE
    # The detail shows the mismatch itself: what was quoted against what is there.
    assert "10 megawatts" in escalations[CLAIM_MISQUOTE].detail
    assert "20 megawatts" in escalations[CLAIM_MISQUOTE].detail


def test_escalations_start_unresolved_and_are_never_reopened():
    init_db()
    _seed()

    with session_scope() as session:
        assert len(escalations_for_company(session, COMPANY, unresolved_only=True)) == 2
        escalation = escalations_for_company(session, COMPANY)[0]
        escalation.resolved_at = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
        escalation.resolved_by = "D. Okoro"

    # A second seed must not undo somebody's afternoon.
    _seed()
    with session_scope() as session:
        assert len(escalations_for_company(session, COMPANY, unresolved_only=True)) == 1
        resolved = [
            e for e in escalations_for_company(session, COMPANY) if e.resolved_at
        ]
        assert [e.resolved_by for e in resolved] == ["D. Okoro"]


def test_withholding_a_claim_is_written_to_the_audit_log():
    init_db()
    _seed()

    with session_scope() as session:
        events = (
            session.query(AuditEvent)
            .filter(AuditEvent.action == "withhold_claim")
            .all()
        )
        assert {event.subject_id for event in events} == {
            CLAIM_MISQUOTE,
            CLAIM_AMBIGUOUS,
        }
        assert verify_chain(session, COMPANY) is True


# --------------------------------------------------------------------------
# The sentences are built by rule, and the rules are the same every run
# --------------------------------------------------------------------------


def test_the_deadline_sentence_is_the_one_the_corpus_was_built_to_produce(data_dir):
    manifest = _manifest(data_dir)
    labels = {v["id"]: v["label"] for v in manifest["versions"]}
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-3")

    assert statement_for(change, labels) == (
        "The compliance date for the updated load forecast moved from "
        "March 1, 2027 to June 1, 2027."
    )


def test_each_labelled_change_gets_a_sentence_naming_what_moved(data_dir):
    manifest = _manifest(data_dir)
    labels = {v["id"]: v["label"] for v in manifest["versions"]}
    built = {c["id"]: statement_for(c, labels) for c in manifest["changes"]}

    assert built["CHG-1"] == (
        "The share in Section 5.2-5.3 moved from 100% to not less than fifty "
        "percent (50%)."
    )
    assert built["CHG-2"] == (
        "The wording of Section 2.1 changed and the 20 megawatts (MW) threshold "
        "did not."
    )
    assert built["CHG-4"] == (
        "Section 6.5 appears for the first time in the Final Order."
    )
    assert built["CHG-5"] == (
        "The Collateral and Financial Assurance section moved from Section 6 to "
        "Section 5.4."
    )
    assert len(set(built.values())) == len(built)


def test_the_sentences_are_the_same_on_every_call(data_dir):
    manifest = _manifest(data_dir)
    labels = {v["id"]: v["label"] for v in manifest["versions"]}
    once = [statement_for(c, labels) for c in manifest["changes"]]
    twice = [statement_for(c, labels) for c in manifest["changes"]]
    assert once == twice


# --------------------------------------------------------------------------
# The health check reports what is there, not what was configured
# --------------------------------------------------------------------------


def test_healthz_counts_the_corpus_it_can_actually_read():
    init_db()
    _seed()
    body = TestClient(app).get("/healthz").json()

    assert body["corpus_loaded"] is True
    assert body["versions"] == 3
    assert body["changes"] == EXPECTED_CHANGES
    assert body["claims"] == 7
    assert body["escalations"] == 2
    assert "detail" not in body


def test_healthz_says_plainly_when_the_corpus_is_not_loaded():
    init_db()
    body = TestClient(app).get("/healthz").json()

    assert body["status"] == "ok"
    assert body["corpus_loaded"] is False
    assert body["versions"] == 0
    # And names the command that fixes it, rather than leaving a reviewer to
    # wonder whether an empty proceeding is the product working.
    assert "app.seed" in body["detail"]
