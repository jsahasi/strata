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
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_SUSPENDED,
    Change,
    ChangeObligation,
    Claim,
    Escalation,
    Obligation,
)
from app.state.routing import (
    ACTION_ESCALATION_ROUTED,
    ACTION_ESCALATION_UNROUTED,
    ROUTE_NO_ESCALATION,
    ROUTE_NO_OBLIGATION,
    ROUTE_OBLIGATION_UNOWNED,
    ROUTE_OK,
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


def _world(session):
    """One company, one owner, one obligation, one change, one escalation."""
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
    map_change_to_obligation(
        session,
        COMPANY,
        change_id=change,
        obligation_id="OBL-001",
        mapped_by=ACTOR,
    )
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
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation)
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
        map_change_to_obligation(
            session, COMPANY, change_id=change, obligation_id="OBL-008",
            mapped_by=ACTOR,
        )
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation)
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
    """The one a naive implementation gets wrong: it picks the first and moves on."""
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
        map_change_to_obligation(
            session, COMPANY, change_id=change, obligation_id="OBL-008",
            mapped_by=ACTOR,
        )
        resolution = _refusal_leaves_it_in_the_queue(
            session, escalation, ROUTE_OWNERS_DISAGREE
        )
        # The names are in the reason, so the analyst can pick without guessing.
        assert set(resolution.candidate_user_ids) == {owner.id, other.id}


def test_an_escalation_from_another_company_resolves_to_nothing():
    init_db()
    with session_scope() as session:
        _world(session)
        resolution = resolve_escalation_owner(session, RIVAL, escalation_id="ESC-1")
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
        resolution = resolve_assignee(session, COMPANY, rule="unassigned")
        assert resolution.reason_code == ROUTE_RULE_UNASSIGNED
        assert resolution.user_id is None


def test_a_rule_outside_the_vocabulary_refuses_rather_than_guessing():
    init_db()
    with session_scope() as session:
        _world(session)
        for rule in ("owner", "team:regulatory", "", "role"):
            resolution = resolve_assignee(session, COMPANY, rule=rule)
            assert resolution.reason_code == ROUTE_RULE_UNKNOWN, rule


def test_a_user_rule_resolves_to_that_account_and_only_within_this_company():
    init_db()
    with session_scope() as session:
        owner, _change_id, _escalation_id = _world(session)
        good = resolve_assignee(session, COMPANY, rule=f"user:{owner.id}")
        assert good.user_id == owner.id
        assert resolve_assignee(session, RIVAL, rule=f"user:{owner.id}").reason_code == (
            ROUTE_USER_UNKNOWN
        )


def test_a_user_rule_naming_a_suspended_account_refuses():
    init_db()
    with session_scope() as session:
        owner, _change_id, _escalation_id = _world(session)
        set_user_status(session, COMPANY, owner.id, STATUS_SUSPENDED, ACTOR)
        resolution = resolve_assignee(session, COMPANY, rule=f"user:{owner.id}")
        assert resolution.reason_code == ROUTE_USER_INACTIVE
        assert resolution.user_id is None


def test_a_role_rule_resolves_when_exactly_one_person_holds_the_role():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        admin = _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
        resolution = resolve_assignee(session, COMPANY, rule=f"role:{ROLE_ADMIN}")
        assert resolution.user_id == admin.id


def test_a_role_nobody_holds_refuses_rather_than_falling_to_an_admin():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
        resolution = resolve_assignee(session, COMPANY, rule=f"role:{ROLE_ANALYST}")
        assert resolution.reason_code == ROUTE_ROLE_EMPTY
        assert resolution.user_id is None


def test_a_role_two_people_hold_refuses_rather_than_picking_one():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        first = _person(session, COMPANY, "priya")
        second = _person(session, COMPANY, "david")
        resolution = resolve_assignee(
            session, COMPANY, rule=f"role:{ROLE_OBLIGATION_OWNER}"
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
            session, COMPANY, rule=f"role:{ROLE_OBLIGATION_OWNER}"
        )
        assert resolution.user_id == first.id


def test_a_role_this_company_does_not_have_refuses():
    init_db()
    with session_scope() as session:
        _world(session)
        resolution = resolve_assignee(session, COMPANY, rule="role:auditor")
        assert resolution.reason_code == ROUTE_ROLE_UNKNOWN


def test_the_obligation_owner_rule_needs_an_escalation_to_resolve_against():
    init_db()
    with session_scope() as session:
        owner, _change_id, escalation = _world(session)
        with_escalation = resolve_assignee(
            session, COMPANY, rule="obligation_owner", escalation_id=escalation
        )
        assert with_escalation.user_id == owner.id

        without = resolve_assignee(session, COMPANY, rule="obligation_owner")
        assert without.reason_code == ROUTE_NO_ESCALATION
        assert without.user_id is None


def test_every_reason_code_a_resolution_can_carry_is_in_the_vocabulary():
    """A code outside the tuple is invisible to every query that filters on it."""
    init_db()
    with session_scope() as session:
        owner, change, escalation = _world(session)
        seen = {
            resolve_escalation_owner(
                session, COMPANY, escalation_id=escalation
            ).reason_code,
            resolve_assignee(session, COMPANY, rule="unassigned").reason_code,
            resolve_assignee(session, COMPANY, rule="nonsense").reason_code,
            resolve_assignee(session, COMPANY, rule="role:auditor").reason_code,
            resolve_assignee(session, COMPANY, rule="user:nobody").reason_code,
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

        resolution = resolve_escalation_owner(session, COMPANY, escalation_id="ESC-1")
        assert resolution.user_id != theirs.id
        assert resolution.reason_code == ROUTE_OK


def test_an_obligation_row_carries_the_companys_own_wording():
    init_db()
    with session_scope() as session:
        _world(session)
        stored = session.get(Obligation, "OBL-001")
        assert stored.title == "Post security before construction starts."
