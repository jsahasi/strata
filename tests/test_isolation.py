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

WHAT THIS FILE STILL CANNOT DO, AND WHERE THAT IS DONE INSTEAD. It names four
functions by hand. An audit deleted eighteen tenant filters from app/ one at a
time and this file, with tests/test_passage_isolation.py beside it, caught
exactly those four -- because a list a person maintains cannot catch the function
that person forgot. tests/test_tenancy_derived.py is the answer to that: it
derives the question from inspect.signature, from the AST and from app.routes
rather than from a list. The last section below carries the three defects that
got past this file, so the behaviour is written down where somebody reading about
tenancy will find it, and the derived guards catch the class.
"""

import pytest

from app.ingestion.ingest import ingest_version
from app.state import permissions as perms
from app.state.audit import chain_head, event_count, record_event, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import ensure_system_roles
from app.state.models import Base, DocumentVersion, Passage, Role
from app.state.queries import (
    CrossTenantRow,
    _require_scope,
    passage_counts_by_version,
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


# ---------------------------------------------------------------------------
# THE THREE THAT GOT THROUGH
#
# The tests above describe the guard. These three describe defects that reached
# the repository past it, and each one is here because the class guard in
# tests/test_tenancy_derived.py would catch it structurally and a reader
# deserves to see the behaviour named as well. A rule with no example beside it
# is a rule people argue with.
# ---------------------------------------------------------------------------


#: The values a guard built out of `if not company_id:` lets straight through.
#: Every one of them matches no stored row, so the read comes back empty and the
#: caller is told the tenant is clean.
SCOPES_A_TRUTHY_CHECK_ACCEPTS = ("   ", "\t\n", " MEP", "MEP ", "MEP%", "%", "_")


@pytest.mark.parametrize("value", SCOPES_A_TRUTHY_CHECK_ACCEPTS)
def test_the_audit_log_refuses_a_scope_it_cannot_parse_rather_than_verifying_it(value):
    """The sharpest of the three, because of which module it was in.

    app/state/audit.py had no reference to _require_scope at all -- the only
    module under app/state with none -- and wrote `if not company_id:` at four
    sites instead. So:

        verify_chain(session, "   ")         -> True
        versions_for_company(session, "   ") -> ValueError

    Two answers to one question about scope, and THE AUDIT LOG GAVE THE
    REASSURING ONE. True there means "every row in this company's chain
    recomputes to the hash it carries". Over a scope that matched nothing it
    meant "there were no rows", and the two sentences read the same to whoever
    asked. "No events matched a scope I could not parse" is not "the chain
    verifies", and the module whose whole job is to be trustworthy is the last
    place to blur them.
    """
    init_db()
    with session_scope() as session:
        for read in (verify_chain, chain_head, event_count):
            with pytest.raises(ValueError):
                read(session, value)
        with pytest.raises(ValueError):
            record_event(
                session,
                company_id=value,
                actor="system:test",
                action="user.created",
                subject_type="user",
                subject_id="USR-1",
                reason="an unparsable scope must not reach the chain",
            )


def test_a_passage_count_carries_no_other_tenants_versions():
    """The one deletion the whole suite could not feel.

    Delete the tenant filter from queries.passage_counts_by_version and 1,984
    tests stayed byte for byte identical. Its only caller reads the dict with
    .get(version_id, 0) against its own version list, so another tenant's keys
    fall on the floor and nothing goes red -- until the day a second caller
    iterates the dict, and then the filter is the only thing between one tenant
    and another's page counts, with nothing having noticed it go.

    tests/test_tenancy_derived.py holds the class guard, which reads the source
    and fails whether or not any caller looks. This is the behaviour it protects,
    written down where somebody reading about tenancy will find it.
    """
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()

        mep = passage_counts_by_version(session, "MEP")
        assert mep, "the owning company must see its own counts"
        assert set(mep) == {"mep-v1"}

        rival = passage_counts_by_version(session, "RIVAL")
        assert set(rival) == {"rival-v1"}
        assert "mep-v1" not in rival


def test_a_role_refusal_never_names_another_tenants_role():
    """The refusal ran before the tenant check, so it could print their word.

    permissions._editable_role fetched a role by primary key, refused a system
    role by interpolating its name, and only then compared company_id. Role names
    are not a fixed vocabulary -- a company composes them and calls them what it
    likes -- so posting another tenant's role id at the form returned a sentence
    carrying THEIR name for it. The tenant question is settled first now, by
    row_for_company, and the only name the function can still print belongs to a
    system role, which every company sees anyway.
    """
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        # Written as a row rather than through create_role, which is behind
        # user.manage and would need a whole RIVAL admin to exist first. What is
        # under test is the refusal, and the refusal only needs the role.
        theirs = Role(
            # An id that says nothing about who holds it, so the assertion below
            # tests the message rather than the fixture.
            id="ROL-9001",
            company_id="RIVAL",
            name="Rate Case Counsel",
            description="a role only RIVAL knows about",
        )
        session.add(theirs)
        session.flush()

        with pytest.raises(ValueError) as refused:
            perms._editable_role(session, "MEP", role_id=theirs.id)

        message = str(refused.value)
        assert "Rate Case Counsel" not in message
        assert "RIVAL" not in message
        assert "composed by this company" in message
