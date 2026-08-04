import json
from pathlib import Path

from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.verification.verifier import (
    REASON_EMPTY_SPAN,
    REASON_OUT_OF_RANGE,
    REASON_QUOTE_MISMATCH,
    REASON_VERSION_UNREADABLE,
    Citation,
    verify_citation,
    verify_citation_for_version,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def _v2_text() -> str:
    return (DATA / "v2_revised_proposed_rule.txt").read_bytes().decode("utf-8")


def _manifest():
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def test_verifies_a_true_citation_from_the_manifest():
    text = _v1_text()
    # CHG-2 before: the Large Load Customer definition in v1.
    citation = Citation(
        version_id="v1",
        char_start=1970,
        char_end=2066,
        quoted_text='"Large Load Customer" means a Customer whose Requested Load equals or exceeds 20 megawatts (MW).',
    )
    result = verify_citation(citation, text)
    assert result.verified is True
    assert result.reason is None


def test_rejects_a_fabricated_quote_at_real_offsets():
    # The single most persuasive test in the submission: correct offsets,
    # invented words. A model that misquotes fluently is caught here.
    text = _v1_text()
    citation = Citation(
        version_id="v1",
        char_start=1970,
        char_end=2066,
        quoted_text='"Large Load Customer" means a Customer whose Requested Load equals or exceeds 10 megawatts (MW).',
    )
    result = verify_citation(citation, text)
    assert result.verified is False
    assert result.reason == REASON_QUOTE_MISMATCH
    assert "20 megawatts" in result.actual_text


def test_rejects_offsets_past_the_end_of_the_source():
    text = _v1_text()
    citation = Citation(
        version_id="v1",
        char_start=len(text) - 5,
        char_end=len(text) + 500,
        quoted_text="anything",
    )
    result = verify_citation(citation, text)
    assert result.verified is False
    assert result.reason == REASON_OUT_OF_RANGE


def test_rejects_negative_and_inverted_offsets():
    text = _v1_text()
    assert verify_citation(Citation("v1", -1, 20, "x"), text).reason == REASON_OUT_OF_RANGE
    assert verify_citation(Citation("v1", 200, 100, "x"), text).reason == REASON_OUT_OF_RANGE


def test_rejects_an_empty_span():
    text = _v1_text()
    result = verify_citation(Citation("v1", 1970, 1970, ""), text)
    assert result.verified is False
    assert result.reason == REASON_EMPTY_SPAN


def test_accepts_a_quote_differing_only_in_normalizable_ways():
    text = _v1_text()
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-2")
    exact = change["before"]["exact_text"]
    noisy = "  " + exact.replace('"', "“", 1).replace('"', "”", 1) + "\n"
    result = verify_citation(
        Citation("v1", change["before"]["start"], change["before"]["end"], noisy), text
    )
    assert result.verified is True


def test_every_manifest_change_verifies_at_its_recorded_offsets():
    manifest = _manifest()
    texts = {
        "v1": (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8"),
        "v2": (DATA / "v2_revised_proposed_rule.txt").read_bytes().decode("utf-8"),
        "v3": (DATA / "v3_final_order.txt").read_bytes().decode("utf-8"),
    }
    checked = 0
    for change in manifest["changes"]:
        for side in ("before", "after"):
            entry = change[side]
            if not entry.get("version"):
                continue
            result = verify_citation(
                Citation(
                    entry["version"], entry["start"], entry["end"], entry["exact_text"]
                ),
                texts[entry["version"]],
            )
            assert result.verified is True, f"{change['id']} {side} failed to verify"
            checked += 1
    assert checked >= 8


# --- the citation must be verified against the version it names --------------
#
# Citation.version_id was carried everywhere and checked nowhere. verify_citation
# takes a string and so cannot check it; that is a real hole, not a theoretical
# one, because the same words sit at different offsets in every version of a
# proceeding. These tests cover the entry point that closes it.

BOILERPLATE = (
    "The Utility shall maintain records sufficient to demonstrate compliance "
    "with this Order for a period of not less than five (5) years."
)
# The Section 4.4 occurrence, per data/manifest.json. Same sentence, 200
# characters further into v2 than into v1, because v2 inserted text above it.
V1_OCCURRENCE_0 = (4930, 5063)
V2_OCCURRENCE_0 = (5130, 5263)


def _seed_a_proceeding(session) -> None:
    for version_id, status, text in (
        ("v1", "DRAFT", _v1_text()),
        ("v2", "DRAFT", _v2_text()),
    ):
        ingest_version(
            session,
            version_id=version_id,
            company_id="MEP",
            docket="MPUC-2026-0142",
            label=version_id,
            status=status,
            source_text=text,
        )
    ingest_version(
        session,
        version_id="rival-v1",
        company_id="RIVAL",
        docket="OTHER-2026-0001",
        label="NOPR",
        status="DRAFT",
        source_text="RIVAL confidential load forecast for Springfield.",
    )
    session.flush()


def test_the_two_argument_form_cannot_catch_a_mispaired_version():
    # Stated as a test rather than left implicit, because it is the reason the
    # version-aware entry point exists. A citation naming v2, handed v1's text,
    # verifies -- the words are there, at those offsets, in that document. The
    # function has no way to know it is the wrong document.
    start, end = V1_OCCURRENCE_0
    misfiled = Citation("v2", start, end, BOILERPLATE)
    result = verify_citation(misfiled, _v1_text(), expected_occurrence=0)
    assert result.verified is True


def test_the_version_aware_form_rejects_that_same_citation():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V1_OCCURRENCE_0
        misfiled = Citation("v2", start, end, BOILERPLATE)
        result = verify_citation_for_version(
            session, misfiled, "MEP", expected_occurrence=0
        )
        assert result.verified is False
        assert result.reason == REASON_QUOTE_MISMATCH
        # It read v2, which is the whole point: those offsets land mid-sentence
        # in Section 4.2 of v2, nowhere near the recordkeeping requirement.
        assert result.actual_text == _v2_text()[start:end]
        assert BOILERPLATE not in result.actual_text


def test_the_version_aware_form_verifies_the_citation_when_it_names_its_own_version():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V2_OCCURRENCE_0
        correct = Citation("v2", start, end, BOILERPLATE)
        result = verify_citation_for_version(
            session, correct, "MEP", expected_occurrence=0
        )
        assert result.verified is True
        assert result.reason is None


def test_the_version_aware_form_still_applies_the_occurrence_rule():
    # Loading the right version must not weaken the checks that already ran on
    # it. The Section 4.4 occurrence claimed as the Section 7.3 one is still
    # refused, and an unstated occurrence is still refused.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V2_OCCURRENCE_0
        citation = Citation("v2", start, end, BOILERPLATE)
        assert not verify_citation_for_version(session, citation, "MEP").verified
        assert not verify_citation_for_version(
            session, citation, "MEP", expected_occurrence=2
        ).verified


def test_a_version_this_company_cannot_read_is_refused_not_answered():
    # A citation naming another tenant's version, and one naming a version that
    # does not exist, must be indistinguishable from outside. Telling them apart
    # tells a caller which version ids exist.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        for version_id in ("rival-v1", "v9-does-not-exist"):
            result = verify_citation_for_version(
                session, Citation(version_id, 0, 20, "RIVAL confidential"), "MEP"
            )
            assert result.verified is False
            assert result.reason == REASON_VERSION_UNREADABLE
            assert result.actual_text is None


def test_the_other_tenant_is_refused_even_when_the_quote_is_correct():
    # The quote below really is the first 18 characters of rival-v1. Reading it
    # back would confirm another tenant's source text to a caller who guessed a
    # version id, which is the failure the chokepoint exists to prevent.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        result = verify_citation_for_version(
            session, Citation("rival-v1", 0, 18, "RIVAL confidential"), "MEP"
        )
        assert result.verified is False
        assert result.reason == REASON_VERSION_UNREADABLE

        # And the same citation, read by the tenant that owns it, verifies --
        # so the refusal above is scope, not a broken lookup.
        allowed = verify_citation_for_version(
            session, Citation("rival-v1", 0, 18, "RIVAL confidential"), "RIVAL"
        )
        assert allowed.verified is True


def test_an_unscoped_read_is_refused_rather_than_treated_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        for company_id in ("", None):
            try:
                verify_citation_for_version(
                    session, Citation("v1", *V1_OCCURRENCE_0, BOILERPLATE), company_id
                )
            except ValueError:
                continue
            raise AssertionError(f"company_id {company_id!r} should have been refused")
