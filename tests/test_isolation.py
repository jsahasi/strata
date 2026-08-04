from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.queries import versions_for_company


def _seed_two_companies(session):
    ingest_version(
        session,
        version_id="mep-v1",
        company_id="MEP",
        docket="MPUC-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text="MEP confidential load forecast for Monrovia.",
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


def test_a_company_read_returns_none_of_another_companys_rows():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()

        mep = versions_for_company(session, "MEP")
        assert [v.id for v in mep] == ["mep-v1"]
        assert all(v.company_id == "MEP" for v in mep)
        assert not any("RIVAL" in v.source_text for v in mep)

        rival = versions_for_company(session, "RIVAL")
        assert [v.id for v in rival] == ["rival-v1"]
        assert not any("MEP" in v.source_text for v in rival)


def test_an_unknown_company_sees_nothing_rather_than_everything():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        assert versions_for_company(session, "NOT-A-TENANT") == []


def test_an_empty_company_id_is_refused_not_treated_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        for value in ("", None, "%"):
            try:
                result = versions_for_company(session, value)
            except ValueError:
                continue
            assert result == [], f"{value!r} behaved as a wildcard"
