import pytest

from app.diff.engine import passage_refs
from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.queries import passages_for_company


def _seed_two_companies(session):
    ingest_version(
        session,
        version_id="mep-v1",
        company_id="MEP",
        docket="MPUC-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text="MEP confidential forecast.\n\nSECTION 1. PURPOSE\n\nMEP load is 400 MW.",
    )
    ingest_version(
        session,
        version_id="rival-v1",
        company_id="RIVAL",
        docket="OTHER-2026-0001",
        label="NOPR",
        status="DRAFT",
        source_text="RIVAL confidential forecast.\n\nSECTION 1. PURPOSE\n\nRIVAL load is 900 MW.",
    )
    session.flush()


def test_passages_are_scoped_by_the_owning_versions_company():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        mep = passages_for_company(session, "MEP", "mep-v1")
        assert mep, "the owning company must see its own passages"
        assert not any("RIVAL" in p.text for p in mep)

        rival = passages_for_company(session, "RIVAL", "rival-v1")
        assert rival
        assert not any("MEP" in p.text for p in rival)


def test_asking_for_another_companys_version_returns_nothing():
    # The defect this test exists for: passage reads carried no company scope,
    # so knowing a version id was enough to read another tenant's source text.
    # Version ids are short and guessable, which made this reachable.
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        assert passages_for_company(session, "MEP", "rival-v1") == []
        assert passages_for_company(session, "RIVAL", "mep-v1") == []


def test_an_unscoped_passage_read_is_refused_not_answered():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        for value in ("", None, "%"):
            try:
                result = passages_for_company(session, value, "mep-v1")
            except ValueError:
                continue
            assert result == [], f"{value!r} behaved as a wildcard"


def test_the_diff_entry_point_cannot_be_called_without_a_company():
    # passage_refs feeds the diff engine. If it can read unscoped, then so can
    # every change the product computes, whatever queries.py promises.
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)

        with pytest.raises(TypeError):
            passage_refs(session, "mep-v1")  # company_id omitted

        refs = passage_refs(session, "mep-v1", company_id="MEP")
        assert refs
        assert all(r.version_id == "mep-v1" for r in refs)
        assert passage_refs(session, "rival-v1", company_id="MEP") == []


def test_offsets_survive_the_scoped_read():
    # Scoping must not disturb the property everything else rests on.
    init_db()
    text = "MEP confidential forecast.\n\nSECTION 1. PURPOSE\n\nMEP load is 400 MW."
    with session_scope() as session:
        ingest_version(
            session,
            version_id="mep-v1",
            company_id="MEP",
            docket="D",
            label="NOPR",
            status="DRAFT",
            source_text=text,
        )
        session.flush()
        for ref in passage_refs(session, "mep-v1", company_id="MEP"):
            assert text[ref.char_start:ref.char_end] == ref.text
