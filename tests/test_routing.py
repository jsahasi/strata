"""Routing an escalation to the person who owns the duty, and refusing to guess.

The product's argument, applied to routing rather than to citations: a claim
whose citation does not verify refuses to assert itself, and an escalation whose
owner cannot be resolved refuses to be assigned. A wrong assignment is worse
than no assignment, because a wrong one looks handled -- it leaves the queue,
lands on a desk that will not act on it, and nobody finds out until the deadline
has gone.

So every test below that ends in a refusal checks three things, not one:

1. the escalation is STILL unassigned afterwards -- not on the admin, not on
   whoever raised it, not on the last person who touched anything;
2. the reason is recorded, in the chain and readable at the queue;
3. the chain still verifies, so the refusal is evidence rather than a log line.

The branches are the four the brief names plus the ones the schema allows:
no obligation maps to the change, the obligation has no owner, the owner's
account is gone, the owner is suspended, and two obligations name two different
people. Each has its own reason code, because "could not route" collapses five
different fixes into one sentence an analyst cannot act on.
"""

import pathlib
import re
from datetime import datetime, timezone

import pytest

from app.state.audit import event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    create_user,
    ensure_system_roles,
    grant_role,
    set_user_status,
)
from app.state.models import (
    AUTHOR_ANALYST,
    AUTHOR_SYSTEM,
    INVITE_PENDING,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    Change,
    ChangeObligation,
    Claim,
    Escalation,
    Invitation,
    Obligation,
)
from app.state.routing import (
    ACTION_ESCALATION_ROUTED,
    ACTION_ESCALATION_UNROUTED,
    ROUTE_MAPPING_UNCONFIRMED,
    ROUTE_NO_ESCALATION,
    ROUTE_NO_OBLIGATION,
    ROUTE_OBLIGATION_UNOWNED,
    ROUTE_OK,
    ROUTE_OK_CODES,
    ROUTE_OWNER_INACTIVE,
    ROUTE_OWNER_UNKNOWN,
    ROUTE_OWNERS_DISAGREE,
    ROUTE_ROLE_AMBIGUOUS,
    ROUTE_ROLE_EMPTY,
    ROUTE_ROLE_UNKNOWN,
    ROUTE_RULE_UNASSIGNED,
    ROUTE_RULE_UNKNOWN,
    ROUTE_USER_INACTIVE,
    ROUTE_USER_UNKNOWN,
    ROUTING_REASON_CODES,
    UNCONFIRMED_MAPPING,
    ensure_obligation,
    escalations_for_user,
    map_change_to_obligation,
    obligations_for_change,
    obligations_for_company,
    resolve_assignee,
    resolve_escalation_owner,
    route_escalation,
    set_obligation_owner,
    shared_queue,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
ACTOR = "system:test"
T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

PASSWORD = "strata-test-password"


# ---------------------------------------------------------------------------
# A small world. Rows are written directly where the row's own module is not
# what is under test: SQLite does not enforce the foreign keys, and building a
# real proceeding through the pipeline would test the pipeline instead.
# ---------------------------------------------------------------------------


def _person(session, company_id, name, role=ROLE_OBLIGATION_OWNER, user_id=None):
    user = create_user(
        session,
        company_id,
        email=f"{name}@{company_id.lower()}.example",
        display_name=name.title(),
        password=PASSWORD,
        actor=ACTOR,
        user_id=user_id or f"usr-{company_id.lower()}-{name}",
    )
    grant_role(session, company_id, user_id=user.id, role_name=role, actor=ACTOR)
    return user


def _change(session, company_id, change_id):
    session.add(
        Change(
            id=change_id,
            company_id=company_id,
            proceeding_id="MPUC-2026-0142",
            from_version_id="v1",
            to_version_id="v2",
            change_type="modified",
            alignment_confidence=1.0,
            status="DRAFT",
        )
    )
    session.flush()
    return change_id


def _escalation(session, company_id, escalation_id, change_id):
    claim_id = f"CLM-{escalation_id}"
    session.add(
        Claim(
            id=claim_id,
            company_id=company_id,
            change_id=change_id,
            statement="The customer share moved.",
            citation_version_id="v2",
            citation_start=0,
            citation_end=10,
            citation_quote="whatever",
            confidence_bp=10000,
        )
    )
    session.add(
        Escalation(
            id=escalation_id,
            company_id=company_id,
            claim_id=claim_id,
            reason_code="CITATION_QUOTE_MISMATCH",
            reason_text="the quote does not match the source",
        )
    )
    session.flush()
    return escalation_id


def _confirm(session, company_id, *, change_id, obligation_id):
    """A mapping a PERSON stands behind. The only kind that routes.

    Written here rather than left to the default because the default is the
    pipeline's. map_change_to_obligation defaults mapped_by_kind to
    AUTHOR_SYSTEM, which is right for the writer -- almost every row is the
    proposer's -- and wrong for a fixture whose whole subject is a person being
    told work is theirs. Every happy path below goes through this helper, so a
    test that routes is a test where somebody confirmed, and the difference is
    visible at the call site instead of hiding in a keyword default.
    """
    return map_change_to_obligation(
        session,
        company_id,
        change_id=change_id,
        obligation_id=obligation_id,
        mapped_by="person:analyst@mep.example",
        mapped_by_kind=AUTHOR_ANALYST,
    )


def _world(session):
    """One company, one owner, one obligation, one change, one escalation.

    THE MAPPING HERE IS A PERSON'S, AND IT DID NOT USE TO BE. This fixture wrote
    the default AUTHOR_SYSTEM row, so every happy-path test in this file was
    asserting that a mapping the PIPELINE proposed from a word overlap routes an
    escalation to a named human. They passed for the same reason the product was
    wrong: resolve_change_owner did not read mapped_by_kind. A fixture that
    cannot tell a candidate from a finding cannot prove the thing this file is
    about.
    """
    ensure_system_roles(session)
    owner = _person(session, COMPANY, "priya")
    ensure_obligation(
        session,
        COMPANY,
        obligation_id="OBL-001",
        title="Post security before construction starts.",
        owner_user_id=owner.id,
        actor=ACTOR,
    )
    change = _change(session, COMPANY, "CHG-1")
    _confirm(session, COMPANY, change_id=change, obligation_id="OBL-001")
    escalation = _escalation(session, COMPANY, "ESC-1", change)
    return owner, change, escalation


# ---------------------------------------------------------------------------
# Obligations and the map, both scoped and both idempotent
# ---------------------------------------------------------------------------


def test_an_obligation_is_written_once_however_often_the_seed_runs():
    init_db()
    with session_scope() as session:
        owner, _change_id, _escalation_id = _world(session)
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security before construction starts.",
            owner_user_id=owner.id,
            actor=ACTOR,
        )
        assert [o.id for o in obligations_for_company(session, COMPANY)] == ["OBL-001"]


def test_a_second_seed_does_not_take_an_obligation_back_off_a_person():
    """Re-seeding must not undo a reassignment somebody made in the product."""
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = _person(session, COMPANY, "priya")
        second = _person(session, COMPANY, "david")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security.",
            owner_user_id=first.id,
            actor=ACTOR,
        )
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security.",
            owner_user_id=second.id,
            actor=ACTOR,
        )
        stored = obligations_for_company(session, COMPANY)[0]
        assert stored.owner_user_id == first.id


def test_an_owner_can_be_filled_in_later_but_never_overwritten():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = _person(session, COMPANY, "priya")
        second = _person(session, COMPANY, "david")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security.",
            owner_user_id=None,
            actor=ACTOR,
        )
        set_obligation_owner(
            session, COMPANY, obligation_id="OBL-001", owner_user_id=first.id,
            actor=ACTOR,
        )
        set_obligation_owner(
            session, COMPANY, obligation_id="OBL-001", owner_user_id=second.id,
            actor=ACTOR, only_when_unowned=True,
        )
        assert obligations_for_company(session, COMPANY)[0].owner_user_id == first.id
        assert verify_chain(session, COMPANY)


def test_mapping_the_same_change_to_the_same_obligation_twice_writes_one_row():
    init_db()
    with session_scope() as session:
        _world(session)
        before = event_count(session, COMPANY)
        map_change_to_obligation(
            session, COMPANY, change_id="CHG-1", obligation_id="OBL-001",
            mapped_by=ACTOR,
        )
        assert session.query(ChangeObligation).count() == 1
        assert event_count(session, COMPANY) == before


def test_another_companys_obligations_are_not_in_this_companys_list():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        ensure_obligation(
            session, COMPANY, obligation_id="OBL-001", title="Ours.", actor=ACTOR
        )
        ensure_obligation(
            session, RIVAL, obligation_id="OBL-999", title="Theirs.", actor=ACTOR
        )
        assert [o.id for o in obligations_for_company(session, COMPANY)] == ["OBL-001"]
        assert [o.id for o in obligations_for_company(session, RIVAL)] == ["OBL-999"]


def test_ensure_obligation_refuses_an_id_another_company_already_holds():
    """The hole this closes, stated exactly.

    ensure_obligation fetched the stored row by primary key and returned it
    without ever asking whose it was. _require_scope ran, but it only checks
    that the string is well formed; it says nothing about the row about to be
    handed back. Obligation ids are unique across tenants and the corpus
    supplies them, so RIVAL loading OBL-001 first meant MEP's loader got back
    RIVAL's title, RIVAL's owner and RIVAL's project -- and every escalation
    that walked change to obligation to owner routed MEP's work to RIVAL's desk.

    It was latent rather than live: scripts/seed_demo_gaps.py is the only caller
    outside these tests, and it passes one company. Latent is the reason it was
    still there, not a reason to leave it.
    """
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        theirs = ensure_obligation(
            session,
            RIVAL,
            obligation_id="OBL-001",
            title="RIVAL: post security in Springfield.",
            actor=ACTOR,
        )

        with pytest.raises(ValueError):
            ensure_obligation(
                session,
                COMPANY,
                obligation_id="OBL-001",
                title="MEP: post security in Monrovia.",
                actor=ACTOR,
            )

        # The refusal changed nothing. Their row still says what it said, and
        # this company still has no obligation under that id.
        assert theirs.company_id == RIVAL
        assert theirs.title == "RIVAL: post security in Springfield."
        assert obligations_for_company(session, COMPANY) == []
        assert [o.id for o in obligations_for_company(session, RIVAL)] == ["OBL-001"]


def test_the_refusal_does_not_say_which_company_does_hold_the_id():
    """Telling the two apart would confirm that some other tenant holds that row,
    which is the fact the scope exists to withhold. It reads like a missing row,
    because that is all the asker is entitled to know."""
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        ensure_obligation(
            session,
            RIVAL,
            obligation_id="OBL-001",
            title="RIVAL: post security in Springfield.",
            actor=ACTOR,
        )
        with pytest.raises(ValueError) as caught:
            ensure_obligation(
                session,
                COMPANY,
                obligation_id="OBL-001",
                title="MEP: post security in Monrovia.",
                actor=ACTOR,
            )
        said = str(caught.value)
        assert RIVAL not in said
        assert "Springfield" not in said


def test_a_second_seed_of_our_own_obligation_is_still_a_no_op():
    """The control for the two above. Scoping the fetch must not break the
    idempotency the loader depends on -- a guard that refused every repeat call
    would pass the leak tests and break `make run`."""
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security.",
            actor=ACTOR,
        )
        again = ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Post security.",
            actor=ACTOR,
        )
        assert again is first
        assert [o.id for o in obligations_for_company(session, COMPANY)] == ["OBL-001"]


def test_an_unscoped_read_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        _world(session)
        for value in ("", None):
            with pytest.raises(ValueError):
                obligations_for_company(session, value)


def test_obligations_for_change_is_scoped_to_the_company():
    init_db()
    with session_scope() as session:
        _world(session)
        assert [o.id for o in obligations_for_change(session, COMPANY, "CHG-1")] == [
            "OBL-001"
        ]
        assert obligations_for_change(session, RIVAL, "CHG-1") == []


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_an_escalation_routes_to_the_owner_of_the_obligation_the_change_touches():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_OK
        assert resolution.routed
        assert resolution.user_id == owner.id
        assert resolution.obligation_ids == ("OBL-001",)


def test_routing_writes_the_assignment_the_time_and_an_audit_row():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        resolution = route_escalation(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        assert resolution.user_id == owner.id

        stored = session.get(Escalation, escalation)
        assert stored.assigned_to_user_id == owner.id
        assert stored.assigned_at == T0

        actions = [row.action for row in _events(session, COMPANY)]
        assert ACTION_ESCALATION_ROUTED in actions
        assert verify_chain(session, COMPANY)


def test_routing_an_escalation_already_on_a_desk_leaves_it_there():
    """Re-running the seed must not take work off the person holding it."""
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        route_escalation(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        before = event_count(session, COMPANY)

        again = route_escalation(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        assert again.user_id == owner.id
        assert event_count(session, COMPANY) == before


def test_two_obligations_naming_one_person_still_route():
    init_db()
    with session_scope() as session:
        owner, change, escalation = _world(session)
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-008",
            title="Return posted funds on energisation.",
            owner_user_id=owner.id,
            actor=ACTOR,
        )
        _confirm(session, COMPANY, change_id=change, obligation_id="OBL-008")
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_OK
        assert resolution.user_id == owner.id
        assert resolution.obligation_ids == ("OBL-001", "OBL-008")


# ---------------------------------------------------------------------------
# Absence is denial. Every branch, and every one of them stays in the queue.
# ---------------------------------------------------------------------------


def _events(session, company_id):
    from app.state.models import AuditEvent

    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq)
        .all()
    )


def _refusal_leaves_it_in_the_queue(session, escalation_id, expected_code):
    resolution = route_escalation(
        session, COMPANY, escalation_id=escalation_id, actor=ACTOR, now=T0
    )
    assert resolution.reason_code == expected_code
    assert not resolution.routed
    assert resolution.user_id is None
    assert resolution.reason_text

    stored = session.get(Escalation, escalation_id)
    assert stored.assigned_to_user_id is None, "a refusal put it on somebody's desk"
    assert stored.assigned_at is None

    rows = [
        row
        for row in _events(session, COMPANY)
        if row.action == ACTION_ESCALATION_UNROUTED
        and row.subject_id == escalation_id
    ]
    assert rows, "the refusal left no trace"
    assert expected_code in rows[-1].reason
    assert verify_chain(session, COMPANY)
    return resolution


def test_no_obligation_maps_to_the_change_so_it_stays_in_the_queue():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        change = _change(session, COMPANY, "CHG-ORPHAN")
        escalation = _escalation(session, COMPANY, "ESC-ORPHAN", change)
        _refusal_leaves_it_in_the_queue(session, escalation, ROUTE_NO_OBLIGATION)


def test_an_obligation_with_no_owner_stays_in_the_queue():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        ensure_obligation(
            session, COMPANY, obligation_id="OBL-001", title="Unowned.", actor=ACTOR
        )
        change = _change(session, COMPANY, "CHG-1")
        map_change_to_obligation(
            session, COMPANY, change_id=change, obligation_id="OBL-001",
            mapped_by=ACTOR,
        )
        escalation = _escalation(session, COMPANY, "ESC-1", change)
        _refusal_leaves_it_in_the_queue(session, escalation, ROUTE_OBLIGATION_UNOWNED)


def test_an_owner_whose_account_is_gone_stays_in_the_queue():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Owned by a ghost.",
            owner_user_id="usr-who",
            actor=ACTOR,
        )
        change = _change(session, COMPANY, "CHG-1")
        map_change_to_obligation(
            session, COMPANY, change_id=change, obligation_id="OBL-001",
            mapped_by=ACTOR,
        )
        escalation = _escalation(session, COMPANY, "ESC-1", change)
        _refusal_leaves_it_in_the_queue(session, escalation, ROUTE_OWNER_UNKNOWN)


def test_an_owner_from_another_company_is_not_an_owner_here():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        theirs = _person(session, RIVAL, "intruder")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            title="Owned across a tenant boundary.",
            owner_user_id=theirs.id,
            actor=ACTOR,
        )
        change = _change(session, COMPANY, "CHG-1")
        map_change_to_obligation(
            session, COMPANY, change_id=change, obligation_id="OBL-001",
            mapped_by=ACTOR,
        )
        escalation = _escalation(session, COMPANY, "ESC-1", change)
        _refusal_leaves_it_in_the_queue(session, escalation, ROUTE_OWNER_UNKNOWN)


def test_a_suspended_owner_stays_in_the_queue():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        set_user_status(session, COMPANY, owner.id, STATUS_SUSPENDED, ACTOR)
        _refusal_leaves_it_in_the_queue(session, escalation, ROUTE_OWNER_INACTIVE)


def test_two_obligations_naming_two_people_stay_in_the_queue():
    """The one a naive implementation gets wrong: it picks the first and moves on.

    BOTH MAPPINGS ARE A PERSON'S, and that is now load-bearing rather than
    incidental. Two duties a person confirmed, with two owners, is a genuine
    disagreement nobody but a human can settle. A duty somebody confirmed beside
    a duty the pipeline guessed is NOT one, and the test for that case is
    test_a_confirmed_mapping_beside_a_proposed_one_routes_on_the_confirmed_one.
    """
    init_db()
    with session_scope() as session:
        owner, change, escalation = _world(session)
        other = _person(session, COMPANY, "david")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-008",
            title="Return posted funds on energisation.",
            owner_user_id=other.id,
            actor=ACTOR,
        )
        _confirm(session, COMPANY, change_id=change, obligation_id="OBL-008")
        resolution = _refusal_leaves_it_in_the_queue(
            session, escalation, ROUTE_OWNERS_DISAGREE
        )
        # The names are in the reason, so the analyst can pick without guessing.
        assert set(resolution.candidate_user_ids) == {owner.id, other.id}


# ---------------------------------------------------------------------------
# A candidate is not a finding, in the place where it costs the most
#
# resolve_change_owner did not read ChangeObligation.mapped_by_kind, so a
# mapping the PIPELINE proposed from a word overlap routed an escalation to a
# named human exactly as a mapping a PERSON confirmed did. Somebody was told
# work was theirs on the strength of two words appearing in both documents.
#
# It was found on a real page: a Kentucky vegetation-management budget table
# shares "project" and "budget" with MEP's cost-allocation duty OBL-005, and the
# change screen printed "Sarah Lindqvist owns OBL-005" over it. The screen grew
# a caveat; the escalation queue and the approval route did not, because they do
# not go through that screen. These tests are the queue's half.
#
# THE THREE STATES ARE TESTED APART, because the interesting one is the third.
# Confirmed routes. Proposed-only refuses and still names who it would have
# given it to -- a refusal that will not say who it was about is a refusal
# nobody can clear. A mix routes on the confirmed one, and the proposed one does
# not get a vote: a guess sitting beside a person's judgement must not be able
# to overrule it by arithmetic, which is what a second owner in the disagree
# check would do.
# ---------------------------------------------------------------------------


def _proposed_world(session, *, kind=AUTHOR_SYSTEM):
    """The Kentucky case: an owned duty, reached only by a word overlap.

    The kind is a PARAMETER rather than two near-identical fixtures, so the
    confirmed control below differs from the refusal case in exactly one value
    and nothing else. A control that differed in two things would not be a
    control.

    It cannot be written by mapping the pair twice, and finding that out was
    worth the fixture. map_change_to_obligation is idempotent ON THE PAIR: given
    a stored proposed row it returns it and writes nothing, so a test that
    proposed and then "confirmed" the same pair through this function was still
    testing a proposed row. Promoting a candidate to a person's finding is
    app/state/mapping.py::confirm_obligation_for_change's job and only its job.
    """
    ensure_system_roles(session)
    owner = _person(session, COMPANY, "sarah")
    ensure_obligation(
        session,
        COMPANY,
        obligation_id="OBL-005",
        title="Allocate network upgrade costs between the parties.",
        owner_user_id=owner.id,
        actor=ACTOR,
    )
    change = _change(session, COMPANY, "CHG-VEG")
    map_change_to_obligation(
        session,
        COMPANY,
        change_id=change,
        obligation_id="OBL-005",
        mapped_by="system:proposer" if kind == AUTHOR_SYSTEM else "person:sarah",
        mapped_by_kind=kind,
    )
    escalation = _escalation(session, COMPANY, "ESC-VEG", change)
    return owner, change, escalation


def test_a_confirmed_mapping_routes_to_the_owner():
    """The control. Without it the refusal below proves only that nothing works."""
    init_db()
    with session_scope() as session:
        owner, _change, escalation = _proposed_world(session, kind=AUTHOR_ANALYST)

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_OK
        assert resolution.routed
        assert resolution.user_id == owner.id


def test_a_mapping_only_the_pipeline_proposed_refuses_and_names_who_it_would_have():
    """Work resting on an unconfirmed mapping is work nobody has agreed is theirs.

    The refusal carries the owner in candidate_user_ids rather than in user_id,
    which is the same shape ROUTE_OWNERS_DISAGREE uses and is not a detail.
    Resolution's invariant is that user_id is set if and only if the code is one
    of ROUTE_OK_CODES, so a caller that reads user_id can never be handed a name
    the product is refusing to stand behind -- while an analyst asked to fix
    this still gets the name, and knows which one confirming would route to.
    """
    init_db()
    with session_scope() as session:
        owner, _change, escalation = _proposed_world(session)

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_MAPPING_UNCONFIRMED
        assert resolution.routed is False
        assert resolution.user_id is None
        assert resolution.candidate_user_ids == (owner.id,)
        assert resolution.obligation_ids == ("OBL-005",)
        # The person is named and the duty is named, so the fix is one click.
        assert owner.display_name in resolution.reason_text
        assert "OBL-005" in resolution.reason_text
        # And the sentence is the library's own, not a second wording of it.
        assert UNCONFIRMED_MAPPING in resolution.reason_text


def test_an_unconfirmed_mapping_leaves_the_item_in_the_shared_queue():
    """Not on a desk, not silently assigned, and the reason is in the chain."""
    init_db()
    with session_scope() as session:
        _proposed_world(session)
        _refusal_leaves_it_in_the_queue(session, "ESC-VEG", ROUTE_MAPPING_UNCONFIRMED)
        assert [item.escalation.id for item in shared_queue(session, COMPANY)] == [
            "ESC-VEG"
        ]


def test_a_confirmed_mapping_beside_a_proposed_one_routes_on_the_confirmed_one():
    """A guess does not get a vote next to a judgement.

    Two owners are in play and they are different people, so the naive reading
    of this is ROUTE_OWNERS_DISAGREE -- which would mean the proposer could stop
    a change routing at all by guessing a second duty. The confirmed mapping is
    the only one considered, so the arithmetic never sees the candidate.
    """
    init_db()
    with session_scope() as session:
        owner, change, escalation = _proposed_world(session)
        other = _person(session, COMPANY, "david")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-002",
            title="File the annual reliability report.",
            owner_user_id=other.id,
            actor=ACTOR,
        )
        _confirm(session, COMPANY, change_id=change, obligation_id="OBL-002")

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_OK
        assert resolution.user_id == other.id
        # And the duty it reports is the confirmed one alone. A page that listed
        # OBL-005 here would be stating a mapping nobody made.
        assert resolution.obligation_ids == ("OBL-002",)
        assert owner.id != other.id


def test_an_unconfirmed_mapping_does_not_hide_a_duty_with_no_owner():
    """The precedence decision, pinned so nobody has to guess it later.

    The new code fires only where a NAME WOULD OTHERWISE HAVE BEEN HANDED OUT.
    It exists to stop an assignment, not to add a second complaint to a refusal
    that already stopped one. A proposed mapping onto an unowned duty still
    answers ROUTE_OBLIGATION_UNOWNED, because "give this duty an owner" is a
    true and actionable sentence whoever wrote the mapping -- and because
    app/state/invites.py branches on exactly that code to decide an invitation
    is the fix. That dependency is now deliberate rather than accidental.
    """
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        ensure_obligation(
            session, COMPANY, obligation_id="OBL-009", title="Unowned.", actor=ACTOR
        )
        change = _change(session, COMPANY, "CHG-2")
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=change,
            obligation_id="OBL-009",
            mapped_by="system:proposer",
            mapped_by_kind=AUTHOR_SYSTEM,
        )
        escalation = _escalation(session, COMPANY, "ESC-2", change)
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_OBLIGATION_UNOWNED


def test_an_invited_owner_behind_an_unconfirmed_mapping_is_not_handed_the_item():
    """Pending acceptance is an assignment too, and it converts like the rest.

    ROUTE_PENDING_ACCEPTANCE means the item HAS left the shared queue and is on
    a named desk; that is the whole reason it is in ROUTE_OK_CODES. So an
    unconfirmed mapping must stop it for the same reason it stops ROUTE_OK.
    Exempting it would be the convenient answer rather than the true one: the
    person invited on the strength of a word overlap is the sharpest version of
    the failure, not an exception to it.
    """
    init_db()
    with session_scope() as session:
        owner, _change, escalation = _proposed_world(session)
        owner.status = STATUS_INVITED
        session.add(
            Invitation(
                id="INV-1",
                company_id=COMPANY,
                email=owner.email,
                invited_by_user_id=owner.id,
                invited_at=T0,
                token_hash="0" * 64,
                expires_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                status=INVITE_PENDING,
                kind="handoff",
                subject_type="escalation",
                subject_id=escalation,
                invited_user_id=owner.id,
            )
        )
        session.flush()

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation, now=T0)
        assert resolution.reason_code == ROUTE_MAPPING_UNCONFIRMED
        assert resolution.routed is False
        assert resolution.pending_acceptance is False
        assert resolution.candidate_user_ids == (owner.id,)


def test_the_unconfirmed_code_is_a_refusal_and_not_an_outcome():
    """It must never join the codes that mean an item is on somebody's desk.

    ROUTE_OK_CODES is what Resolution.routed reads, and every consumer in the
    product -- the workflow step opener, the invitation gap check, the queue --
    branches on .routed. One careless addition to that tuple would put the bug
    back everywhere at once, silently, with no test naming it.
    """
    assert ROUTE_MAPPING_UNCONFIRMED in ROUTING_REASON_CODES
    assert ROUTE_MAPPING_UNCONFIRMED not in ROUTE_OK_CODES
    assert len(set(ROUTING_REASON_CODES)) == len(ROUTING_REASON_CODES)


# ---------------------------------------------------------------------------
# The vocabulary is described in four places, and adding a code went stale in
# three of them in a single commit
#
# THE FAILURE THAT TAUGHT THIS. ROUTE_MAPPING_UNCONFIRMED was the sixteenth
# refusal code. Four files describe this vocabulary in prose -- the library, the
# module that proposes the mappings it reads, the view that renders it and the
# template the view renders. The commit that added the code updated one of the
# four counts, left "Fifteen refusal codes" standing in the template, wrote "the
# other fourteen" into the library's own docstring, and left a whole paragraph in
# app/state/mapping.py describing the limit as still open and naming a symbol
# the same commit deleted. An adversarial reviewer found all four; no test did.
#
# A hand-written count in prose is a claim nobody checks. It is right on the day
# it is typed and wrong on the day the next code lands, and the reader who
# trusts it is the reader who has not read the tuple -- which is every reader,
# because that is what the sentence is for.
#
# So there are two guards, and they do different work. The first bans the
# hand-count outright: say what the codes DO, and let the tuple say how many
# there are. The second pins the size of the tuple, so a seventeenth code cannot
# arrive quietly -- it fails, names the four files, and the person adding the
# code rereads them. Neither guard can tell whether a sentence is true. Together
# they make a change to the vocabulary impossible to make in silence, which is
# the most a test can do about prose.
# ---------------------------------------------------------------------------

#: Every file that describes routing's reason codes in prose rather than using
#: them. Listed here rather than discovered by grep because the point is the
#: CHECKLIST: a person adding a code needs to be told where to go, and a
#: discovered list would silently shrink the day one of these was renamed. The
#: guard below asserts each path still exists for exactly that reason.
ROUTING_VOCABULARY_PROSE = (
    "app/state/routing.py",
    "app/state/mapping.py",
    "app/web/views/changes.py",
    "app/web/templates/change.html",
    # The market document counted them too, and a stale number there is read by
    # the panel rather than by a maintainer. It is the surface with the fewest
    # readers who could spot the error and the most who would act on it.
    "docs/mrd.html",
)

#: Cardinals and ordinals, "one" deliberately absent. "One code per fix" is a
#: rule and not a count, and banning it would push the writer towards a vaguer
#: sentence to satisfy a test -- which is worse prose bought with no truth.
_COUNT_WORDS = (
    "two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
)

#: A number word standing directly in front of "refusal code" or "routing code",
#: in either the cardinal or the ordinal form. Deliberately narrow. A looser
#: pattern flagged "one line of code", "the other two" and "the other six
#: duties" -- all true, none of them counting anything -- and a guard that cries
#: about correct prose gets suppressed within a week.
_HAND_COUNTED = re.compile(
    rf"\b({_COUNT_WORDS})(?:th|teenth|tieth)?\s+(?:refusal|routing)\s+codes?\b",
    re.IGNORECASE,
)


def test_no_prose_hand_counts_the_routing_refusal_codes():
    """Say what the codes do. The tuple says how many there are."""
    root = pathlib.Path(__file__).resolve().parents[1]
    found = []
    for name in ROUTING_VOCABULARY_PROSE:
        path = root / name
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if _HAND_COUNTED.search(line):
                found.append(f"{name}:{number}: {line.strip()}")
    assert not found, (
        "a refusal-code total written out by hand goes stale the day the next "
        "code lands, and three of these did:\n" + "\n".join(found)
    )


def test_a_new_reason_code_cannot_arrive_without_the_prose_being_reread():
    """The tripwire, and the checklist it hands you when it goes off.

    If this test failed for you, you added or removed a routing reason code.
    That is allowed and this test is not asking you not to. It is asking you to
    open every file in ROUTING_VOCABULARY_PROSE and read what it says about this
    vocabulary before you change the number below, because last time three of
    them were left describing a product that no longer existed.

    The refusal count is derived rather than typed twice, so this test cannot
    itself drift out of step with ROUTE_OK_CODES.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    missing = [name for name in ROUTING_VOCABULARY_PROSE if not (root / name).exists()]
    assert not missing, (
        "a file that describes the reason codes was moved or renamed and the "
        f"checklist was not updated: {missing}"
    )

    assert len(ROUTING_REASON_CODES) == 18
    assert set(ROUTE_OK_CODES) <= set(ROUTING_REASON_CODES)
    refusals = [c for c in ROUTING_REASON_CODES if c not in ROUTE_OK_CODES]
    assert len(refusals) == 16


def test_the_caveat_has_one_wording_and_nothing_holds_a_second_copy():
    """Two names for one sentence is two things to keep true.

    THE FAILURE. The words lived in app/web/views/changes.py as
    UNCONFIRMED_ROUTING, printed under a routed owner, because the fix was the
    screen's. When routing learned to refuse, the sentence moved to the library
    -- and the danger at that moment was leaving a copy behind, or an alias
    pointing at the new one. Either would give the product two places to edit
    and one of them would go stale on the screen a regulator reads.

    So the literal is searched for across the whole of app/, not just the two
    modules that use it. A copy pasted into a template is exactly the failure
    this guards, and a template would never show up in an import graph.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    # A distinctive fragment rather than the whole sentence, so a copy that
    # rewrapped the lines is still caught. Line wrapping is how a duplicate
    # usually arrives: somebody pastes it and their editor reflows it.
    fragment = "read the owner as a suggestion rather than as"
    holders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".html"}
        and fragment in path.read_text(errors="ignore")
    ]
    assert holders == ["state/routing.py"], (
        "the caveat is defined once and imported everywhere else; a second copy "
        f"is a second thing to keep true: {holders}"
    )


def test_an_escalation_from_another_company_resolves_to_nothing():
    init_db()
    with session_scope() as session:
        _world(session)
        resolution = resolve_escalation_owner(session, RIVAL, escalation_id="ESC-1", now=T0)
        assert resolution.reason_code == ROUTE_NO_ESCALATION
        assert resolution.user_id is None


def test_a_refusal_never_falls_back_to_the_admin_or_to_anybody_else():
    """The failure this whole module exists to prevent, stated once as a test."""
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        admin = _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
        analyst = _person(session, COMPANY, "denise", role=ROLE_ANALYST)
        change = _change(session, COMPANY, "CHG-ORPHAN")
        escalation = _escalation(session, COMPANY, "ESC-ORPHAN", change)

        route_escalation(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)

        stored = session.get(Escalation, escalation)
        assert stored.assigned_to_user_id not in (admin.id, analyst.id)
        assert stored.assigned_to_user_id is None


# ---------------------------------------------------------------------------
# The queue an analyst opens
# ---------------------------------------------------------------------------


def test_the_shared_queue_says_why_each_item_is_still_there():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "priya")
        orphan_change = _change(session, COMPANY, "CHG-ORPHAN")
        orphan = _escalation(session, COMPANY, "ESC-ORPHAN", orphan_change)

        unowned_change = _change(session, COMPANY, "CHG-UNOWNED")
        ensure_obligation(
            session, COMPANY, obligation_id="OBL-002", title="Unowned.", actor=ACTOR
        )
        map_change_to_obligation(
            session, COMPANY, change_id=unowned_change, obligation_id="OBL-002",
            mapped_by=ACTOR,
        )
        unowned = _escalation(session, COMPANY, "ESC-UNOWNED", unowned_change)

        queue = shared_queue(session, COMPANY)
        by_id = {item.escalation.id: item.resolution.reason_code for item in queue}
        assert by_id == {
            orphan: ROUTE_NO_OBLIGATION,
            unowned: ROUTE_OBLIGATION_UNOWNED,
        }


def test_a_routed_escalation_leaves_the_shared_queue_and_lands_on_a_desk():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        assert [item.escalation.id for item in shared_queue(session, COMPANY)] == [
            escalation
        ]
        route_escalation(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        assert shared_queue(session, COMPANY) == []
        mine = escalations_for_user(session, COMPANY, owner.id)
        assert [e.id for e in mine] == [escalation]


def test_a_resolved_escalation_is_not_in_the_queue_even_when_unrouted():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        change = _change(session, COMPANY, "CHG-ORPHAN")
        escalation = _escalation(session, COMPANY, "ESC-ORPHAN", change)
        session.get(Escalation, escalation).resolved_at = T0
        session.flush()
        assert shared_queue(session, COMPANY) == []


# ---------------------------------------------------------------------------
# The assignee rules a workflow step uses
# ---------------------------------------------------------------------------


def test_the_unassigned_rule_refuses_rather_than_picking_somebody():
    init_db()
    with session_scope() as session:
        _world(session)
        resolution = resolve_assignee(session, COMPANY, rule="unassigned", now=T0)
        assert resolution.reason_code == ROUTE_RULE_UNASSIGNED
        assert resolution.user_id is None


def test_a_rule_outside_the_vocabulary_refuses_rather_than_guessing():
    init_db()
    with session_scope() as session:
        _world(session)
        for rule in ("owner", "team:regulatory", "", "role"):
            resolution = resolve_assignee(session, COMPANY, rule=rule, now=T0)
            assert resolution.reason_code == ROUTE_RULE_UNKNOWN, rule


def test_a_user_rule_resolves_to_that_account_and_only_within_this_company():
    init_db()
    with session_scope() as session:
        owner, _change_id, _escalation_id = _world(session)
        good = resolve_assignee(session, COMPANY, rule=f"user:{owner.id}", now=T0)
        assert good.user_id == owner.id
        assert resolve_assignee(session, RIVAL, rule=f"user:{owner.id}", now=T0).reason_code == (
            ROUTE_USER_UNKNOWN
        )


def test_a_user_rule_naming_a_suspended_account_refuses():
    init_db()
    with session_scope() as session:
        owner, _change_id, _escalation_id = _world(session)
        set_user_status(session, COMPANY, owner.id, STATUS_SUSPENDED, ACTOR)
        resolution = resolve_assignee(session, COMPANY, rule=f"user:{owner.id}", now=T0)
        assert resolution.reason_code == ROUTE_USER_INACTIVE
        assert resolution.user_id is None


def test_a_role_rule_resolves_when_exactly_one_person_holds_the_role():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        admin = _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
        resolution = resolve_assignee(session, COMPANY, rule=f"role:{ROLE_ADMIN}", now=T0)
        assert resolution.user_id == admin.id


def test_a_role_nobody_holds_refuses_rather_than_falling_to_an_admin():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
        resolution = resolve_assignee(session, COMPANY, rule=f"role:{ROLE_ANALYST}", now=T0)
        assert resolution.reason_code == ROUTE_ROLE_EMPTY
        assert resolution.user_id is None


def test_a_role_two_people_hold_refuses_rather_than_picking_one():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = _person(session, COMPANY, "priya")
        second = _person(session, COMPANY, "david")
        resolution = resolve_assignee(
            session, COMPANY, rule=f"role:{ROLE_OBLIGATION_OWNER}", now=T0
        )
        assert resolution.reason_code == ROUTE_ROLE_AMBIGUOUS
        assert resolution.user_id is None
        assert set(resolution.candidate_user_ids) == {first.id, second.id}


def test_suspending_one_of_two_holders_makes_the_role_resolve_again():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = _person(session, COMPANY, "priya")
        second = _person(session, COMPANY, "david")
        set_user_status(session, COMPANY, second.id, STATUS_SUSPENDED, ACTOR)
        resolution = resolve_assignee(
            session, COMPANY, rule=f"role:{ROLE_OBLIGATION_OWNER}", now=T0
        )
        assert resolution.user_id == first.id


def test_a_role_this_company_does_not_have_refuses():
    init_db()
    with session_scope() as session:
        _world(session)
        resolution = resolve_assignee(session, COMPANY, rule="role:auditor", now=T0)
        assert resolution.reason_code == ROUTE_ROLE_UNKNOWN


def test_the_obligation_owner_rule_needs_an_escalation_to_resolve_against():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        with_escalation = resolve_assignee(
            session, COMPANY, rule="obligation_owner", escalation_id=escalation, now=T0
        )
        assert with_escalation.user_id == owner.id

        without = resolve_assignee(session, COMPANY, rule="obligation_owner", now=T0)
        assert without.reason_code == ROUTE_NO_ESCALATION
        assert without.user_id is None


def test_every_reason_code_a_resolution_can_carry_is_in_the_vocabulary():
    """A code outside the tuple is invisible to every query that filters on it."""
    init_db()
    with session_scope() as session:
        owner, change, escalation = _world(session)
        seen = {
            resolve_escalation_owner(
                session, COMPANY, escalation_id=escalation, now=T0
            ).reason_code,
            resolve_assignee(session, COMPANY, rule="unassigned", now=T0).reason_code,
            resolve_assignee(session, COMPANY, rule="nonsense", now=T0).reason_code,
            resolve_assignee(session, COMPANY, rule="role:auditor", now=T0).reason_code,
            resolve_assignee(session, COMPANY, rule="user:nobody", now=T0).reason_code,
        }
        assert seen <= set(ROUTING_REASON_CODES)
        assert len(set(ROUTING_REASON_CODES)) == len(ROUTING_REASON_CODES)


def test_an_obligation_and_its_map_are_never_read_across_the_tenant_line():
    init_db()
    with session_scope() as session:
        _world(session)
        ensure_system_roles(session)
        theirs = _person(session, RIVAL, "intruder")
        ensure_obligation(
            session,
            RIVAL,
            obligation_id="OBL-777",
            title="Theirs.",
            owner_user_id=theirs.id,
            actor=ACTOR,
        )
        # The rival tries to map THEIR obligation onto OUR change. The helper
        # refuses, because the change is not theirs to map.
        with pytest.raises(ValueError):
            map_change_to_obligation(
                session, RIVAL, change_id="CHG-1", obligation_id="OBL-777",
                mapped_by=ACTOR,
            )

        # And if such a row reached the table another way -- a direct write, a
        # future bug -- the read still refuses it. Three columns are filtered on
        # this query and each was written by a different call.
        session.add(
            ChangeObligation(
                company_id=RIVAL,
                change_id="CHG-1",
                obligation_id="OBL-777",
                mapped_at=T0,
                mapped_by=ACTOR,
            )
        )
        session.flush()

        ours = obligations_for_change(session, COMPANY, "CHG-1")
        assert [o.id for o in ours] == ["OBL-001"]

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id="ESC-1", now=T0)
        assert resolution.user_id != theirs.id
        assert resolution.reason_code == ROUTE_OK


def test_an_obligation_row_carries_the_companys_own_wording():
    init_db()
    with session_scope() as session:
        _world(session)
        stored = session.get(Obligation, "OBL-001")
        assert stored.title == "Post security before construction starts."
