"""Tenant isolation: the guard on the way in, and the guard on the way out.

TWO HALVES, AND THE SECOND ONE IS NEWER. The first half is the scope guard --
_require_scope, which refuses a company_id that could match more than the caller
meant. The second is row_for_company, the chokepoint for fetching one row by its
primary key and handing it back only to the company that owns it.

WHY THESE TESTS WERE REWRITTEN. The wildcard test below used to read:

    try:
        result = versions_for_company(session, value)
    except ValueError:
        continue
    assert result == [], f"{value!r} behaved as a wildcard"

which passes whether or not the guard exists. SQLAlchemy's `==` is an equality
comparison, so a company_id of "%" matches no row and returns [] with the guard
deleted -- and the test calls that a pass. A test that green-lights the absence
of the thing it is named after is worse than no test, because it is counted.
Every assertion here now demands the raise, so deleting a line of _require_scope
turns this file red.
"""

import pytest

from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.models import Base, DocumentVersion, Passage
from app.state.queries import (
    CrossTenantRow,
    _require_scope,
    row_for_company,
    versions_for_company,
)


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


# ---------------------------------------------------------------------------
# The guard on the way in
# ---------------------------------------------------------------------------


#: Every value _require_scope must refuse, and the failure each one names. The
#: two wildcards are the interesting ones: they are harmless against an equality
#: comparison and become "every tenant" the moment any caller reaches for LIKE,
#: a raw string filter or a search box that builds a pattern. The guard refuses
#: them now rather than after somebody writes that query.
REFUSED_SCOPES = (
    None,
    "",
    "   ",
    "\t\n",
    "%",
    "MEP%",
    "%MEP",
    "_",
    "ME_",
    " MEP",
    "MEP ",
    123,
    b"MEP",
    ["MEP"],
    True,
)


@pytest.mark.parametrize("value", REFUSED_SCOPES)
def test_the_scope_guard_refuses_every_value_that_could_match_more_than_one_tenant(
    value,
):
    """The guard raises. It does not return, and it does not answer [].

    Asserting on the raise rather than on an empty list is the whole point of
    this rewrite. An empty list is what a deleted guard also produces.
    """
    with pytest.raises(ValueError):
        _require_scope(value)


def test_the_scope_guard_accepts_the_ids_the_product_actually_uses():
    """The control. Without it, a guard that refused everything would pass above."""
    for value in ("MEP", "RIVAL", "NOT-A-TENANT", "acme-energy", "C1"):
        assert _require_scope(value) == value


@pytest.mark.parametrize("value", REFUSED_SCOPES)
def test_a_read_with_a_wildcard_scope_raises_rather_than_answering(value):
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        with pytest.raises(ValueError):
            versions_for_company(session, value)


# ---------------------------------------------------------------------------
# The guard on the way out
#
# The defect these were written against: three call sites fetched a row by
# primary key with session.get and then remembered, or did not remember, to
# compare company_id afterwards. app/state/routing.py::ensure_obligation did
# not. Ids are unique across tenants, so the row it handed back could be
# somebody else's.
# ---------------------------------------------------------------------------


def test_a_scoped_get_hands_back_the_row_when_it_belongs_to_the_asker():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        row = row_for_company(session, "MEP", DocumentVersion, "mep-v1")
        assert row is not None
        assert row.id == "mep-v1"
        assert row.company_id == "MEP"


def test_a_scoped_get_refuses_another_companys_row_rather_than_returning_it():
    """Absence is denial. The row exists; it is not this company's; nothing is
    handed back and the caller is told."""
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        with pytest.raises(CrossTenantRow):
            row_for_company(session, "MEP", DocumentVersion, "rival-v1")


def test_the_refusal_says_nothing_about_who_does_hold_the_row():
    """A message naming RIVAL would confirm that RIVAL holds that id, which is
    the fact the scope exists to withhold."""
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        with pytest.raises(CrossTenantRow) as caught:
            row_for_company(session, "MEP", DocumentVersion, "rival-v1")
        assert "RIVAL" not in str(caught.value)
        assert "Springfield" not in str(caught.value)


def test_a_scoped_get_returns_none_for_an_id_nobody_holds():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        assert row_for_company(session, "MEP", DocumentVersion, "no-such-v") is None
        assert row_for_company(session, "MEP", DocumentVersion, "") is None
        assert row_for_company(session, "MEP", DocumentVersion, None) is None


def test_a_scoped_get_is_refused_without_a_company():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        for value in ("", None, "%", "   "):
            with pytest.raises(ValueError):
                row_for_company(session, value, DocumentVersion, "mep-v1")


def test_a_model_that_cannot_say_whose_row_it_is_raises_rather_than_passing():
    """Passage carries no company_id; its tenancy lives on the version above it.

    Handing it to the chokepoint is a mistake at the call site, and it fails
    loudly here rather than answering None for ever. A scoped read that silently
    returns nothing is the failure this whole file is about, arrived at from the
    other side -- the caller reads "no such row" and never learns the check
    could not run.
    """
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        passage = session.query(Passage).first()
        assert passage is not None, "the fixture must produce a passage to ask about"
        with pytest.raises(TypeError):
            row_for_company(session, "MEP", Passage, passage.id)


#: Tables that hold no company_id, and how tenancy reaches each one instead.
#: Named here rather than counted, so a new table added without a company_id has
#: to be argued for in this list rather than slipping in as a number that moved.
#:
#:   permissions, role_permissions -- vocabulary, not tenant data. models.py
#:     makes the argument at the head of the identity block.
#:   passages -- scoped by the document version that owns it; the join in
#:     queries.passages_for_company is the enforcement.
#:   share_opens -- an access record on a share, scoped by the share.
#:   workflow_steps, workflow_edges -- parts of one route, scoped by the
#:     workflow that owns them.
TENANCY_BY_PARENT = {
    "permissions",
    "role_permissions",
    "passages",
    "share_opens",
    "workflow_steps",
    "workflow_edges",
}


def test_every_other_table_carries_the_company_id_the_chokepoint_reads():
    """A standing check over the schema rather than over the call sites we have.

    row_for_company reads company_id off the row. A tenant table added without
    one would reach it and raise on a reviewer's machine; this says so at the
    schema instead, and forces the new table into the list above with a reason.
    """
    missing = sorted(
        table.name
        for table in Base.metadata.sorted_tables
        if table.name not in TENANCY_BY_PARENT and "company_id" not in table.c
    )
    assert missing == [], f"tenant tables with no company_id: {missing}"


def test_the_parent_scoped_list_names_no_table_that_has_since_grown_a_company_id():
    """The other direction. A stale exemption is an unaudited hole."""
    tables = {table.name: table for table in Base.metadata.sorted_tables}
    stale = sorted(
        name
        for name in TENANCY_BY_PARENT
        if name in tables and "company_id" in tables[name].c
    )
    assert stale == [], f"these no longer need the exemption: {stale}"
