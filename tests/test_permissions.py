"""Any permission to any person, and every departure from the grid on the record.

Three roles could not say what data/real/ says: Analyst II, Deputy Director, VP
Pricing and Planning, Indiana Regulatory Counsel. The shortage already produced a
defect -- scripts/seed_route.py routes "Legal review" and "Officer signs the
filing" to the same role:admin, so the person who draws the route also answers
it. This suite covers the layer that fixes it, and it is written around the four
things that have a cheap wrong implementation reading perfectly:

1. AN ADMIN MINTING AUTHORITY. user.manage arranges the authority an account
   already holds; it never creates new authority. There are three ways to break
   that -- a direct grant, a composed role, an edited role -- and each gets its
   own test, plus the one that matters most: an admin cannot hand out
   action.approve, because the admin role deliberately does not hold it.

2. A CONFLICT REFUSED INSTEAD OF SHOWN. A four-person team where one person
   proposes and approves is a real company that must still be able to work. Every
   conflict test therefore asserts twice: that the grant SUCCEEDED, and that the
   register names it, names both sides, and says how each side was obtained.

3. SUSPENSION BECOMING HALF A CONTROL. A direct grant that outlives a suspension
   would leave an account that is switched off still holding action.approve.

4. A CUSTOM ROLE THAT LIES ABOUT WHAT IT IS. A system role edited into something
   else, a company role called "analyst", a fork with no record of its origin.
   Each is refused, and refused before the write rather than by a database
   constraint the live database does not carry.
"""

from datetime import datetime, timezone

import pytest

from app.auth import policy
from app.state import permissions as perms
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    event_count,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.identity import (
    SYSTEM_ROLE_PERMISSIONS,
    create_user,
    ensure_system_roles,
    grant_role,
    permissions_for_role,
    permissions_for_user,
    revoke_role,
    role_for_company,
    set_user_status,
)
from app.state.models import (
    PERMISSION_CODES,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    AuditEvent,
    Role,
    RolePermission,
    UserPermission,
    conflicts_in,
    role_is_system,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
PASSWORD = "correct-horse-battery-staple"
SEED = "seed@mep.example"

APPROVE = "action.approve"
PROPOSE = "action.propose"
MANAGE = "user.manage"


def _person(session, email, role, *, company=COMPANY, name="A Person"):
    user = create_user(
        session,
        company,
        email=email,
        display_name=name,
        password=PASSWORD,
        actor=SEED,
    )
    if role is not None:
        grant_role(session, company, user_id=user.id, role_name=role, actor=SEED)
    return user


def _bootstrap(session, company=COMPANY):
    """One admin, one analyst, one obligation owner. The shipped grid, nothing else."""
    ensure_system_roles(session)
    prefix = company.lower()
    admin = _person(
        session, f"admin@{prefix}.example", ROLE_ADMIN, company=company, name="Ada Admin"
    )
    analyst = _person(
        session,
        f"analyst@{prefix}.example",
        ROLE_ANALYST,
        company=company,
        name="Dana Okafor",
    )
    owner = _person(
        session,
        f"owner@{prefix}.example",
        ROLE_OBLIGATION_OWNER,
        company=company,
        name="Owen Ruiz",
    )
    return admin, analyst, owner


def _deputy_admin(session, owner, admin):
    """An obligation owner who also holds user.manage directly.

    The only account in these tests that may hand out action.approve, and it may
    because it holds it. Built through the product rather than by writing a row.
    """
    perms.grant(
        session,
        COMPANY,
        actor=admin.id,
        user_id=owner.id,
        code=MANAGE,
        reason="deputy administrator while the director is away",
    )
    return owner


def _denials(session, company=COMPANY):
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company)
        .filter(AuditEvent.action == ACTION_ACCESS_DENIED)
        .order_by(AuditEvent.seq)
        .all()
    )


# ---------------------------------------------------------------------------
# The read: role grants and direct grants, and where each came from
# ---------------------------------------------------------------------------


def test_a_direct_grant_reaches_the_permission_check_the_product_already_uses():
    # The whole feature is worthless if the new table is a second grid nothing
    # reads. permissions_for_user is the one call every gate goes through.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        assert "audit.read" not in permissions_for_user(session, COMPANY, analyst.id)

        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain for the quarterly report",
        )

        assert "audit.read" in permissions_for_user(session, COMPANY, analyst.id)
        assert policy.has(session, COMPANY, analyst.id, "audit.read") is True


def test_effective_permissions_says_whether_a_code_came_from_a_role_or_directly():
    # The only question an auditor asks. A screen that cannot answer it is a
    # screen that cannot be shown to one.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain for the quarterly report",
        )

        held = perms.effective_permissions(session, COMPANY, analyst.id)
        by_code = {g.code: g for g in held}

        assert by_code[PROPOSE].via == perms.VIA_ROLE
        assert by_code[PROPOSE].role_name == ROLE_ANALYST
        assert by_code[PROPOSE].reason == ""

        assert by_code["audit.read"].via == perms.VIA_DIRECT
        assert by_code["audit.read"].role_name is None
        assert "quarterly report" in by_code["audit.read"].reason
        assert by_code["audit.read"].granted_by == admin.email


def test_the_two_reads_cannot_disagree_about_what_somebody_holds():
    # effective_permissions and permissions_for_user are two queries over the
    # same rows. Two answers to one question is the drift this pins shut.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        grant_role(session, COMPANY, user_id=analyst.id, role_name=ROLE_ADMIN, actor=SEED)

        held = perms.effective_permissions(session, COMPANY, analyst.id)
        listed = {row.code for row in held}
        assert listed == permissions_for_user(session, COMPANY, analyst.id)


def test_one_code_held_twice_is_listed_twice_with_both_origins():
    # Revoking the direct grant would leave the role's copy in place. A register
    # that collapsed the two would show a permission vanish that did not.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=owner.id,
            code=APPROVE,
            reason="named on the Indiana filing as well as through the role",
        )

        held = perms.effective_permissions(session, COMPANY, owner.id)
        approvals = [g for g in held if g.code == APPROVE]
        assert {g.via for g in approvals} == {perms.VIA_ROLE, perms.VIA_DIRECT}


def test_a_suspended_account_holds_nothing_directly_either():
    # Otherwise suspension is half a control: the role grants stop and the
    # exceptions carry on.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        assert "audit.read" in permissions_for_user(session, COMPANY, analyst.id)

        set_user_status(session, COMPANY, analyst.id, STATUS_SUSPENDED, SEED)

        assert permissions_for_user(session, COMPANY, analyst.id) == frozenset()
        assert perms.effective_permissions(session, COMPANY, analyst.id) == ()
        # And the row is still there, because who held what in March is the
        # question an investigation asks.
        assert perms.direct_grants_for_user(session, COMPANY, analyst.id)


def test_another_tenants_direct_grant_is_not_visible_here():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, rival_analyst, _ = _bootstrap(session, company=RIVAL)
        perms.grant(
            session,
            RIVAL,
            actor=rival_admin.id,
            user_id=rival_analyst.id,
            code="audit.read",
            reason="their business, not ours",
        )

        assert perms.effective_permissions(session, COMPANY, rival_analyst.id) == ()
        assert permissions_for_user(session, COMPANY, rival_analyst.id) == frozenset()
        assert perms.direct_grants_for_user(session, COMPANY, rival_analyst.id) == []
        assert perms.conflicts(session, COMPANY) == ()


def test_an_unscoped_read_is_refused_rather_than_treated_as_every_company():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for empty in ("", None):
            with pytest.raises(ValueError):
                perms.effective_permissions(session, empty, analyst.id)
            with pytest.raises(ValueError):
                perms.conflicts(session, empty)


# ---------------------------------------------------------------------------
# The rule that prevents escalation
# ---------------------------------------------------------------------------


def test_an_admin_cannot_grant_action_approve_because_an_admin_does_not_hold_it():
    # The load-bearing test. Without it, user.manage is every role in one click
    # and the grid in identity.py is decoration.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        assert APPROVE not in SYSTEM_ROLE_PERMISSIONS[ROLE_ADMIN]

        before = event_count(session, COMPANY)
        with pytest.raises(policy.PermissionDenied) as denied:
            perms.grant(
                session,
                COMPANY,
                actor=admin.id,
                user_id=analyst.id,
                code=APPROVE,
                reason="wants to sign it themselves",
            )

        assert APPROVE in str(denied.value)
        assert APPROVE not in permissions_for_user(session, COMPANY, analyst.id)
        # The refusal is a fact in the chain, not a line in a log file.
        assert event_count(session, COMPANY) == before + 1
        assert APPROVE in _denials(session)[-1].reason


def test_an_admin_cannot_compose_a_role_holding_what_they_do_not_hold():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(policy.PermissionDenied):
            perms.create_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Officer",
                codes=("claim.read", APPROVE),
                reason="somebody has to sign the filing",
            )
        assert perms.composed_roles(session, COMPANY) == []


def test_an_admin_cannot_edit_a_permission_into_a_role_they_do_not_hold():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Reviewer",
            codes=("claim.read", "change.read"),
            reason="counsel reads the filing before it goes",
        )
        with pytest.raises(policy.PermissionDenied):
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Reviewer",
                codes=("claim.read", "change.read", APPROVE),
                reason="let counsel sign as well",
            )

        assert permissions_for_role(session, COMPANY, "Legal Reviewer") == frozenset(
            {"claim.read", "change.read"}
        )


def test_somebody_who_holds_approval_may_hand_it_on():
    # The rule is a ceiling, not a ban. An account that holds action.approve and
    # user.manage may grant action.approve -- and the second half of that
    # sentence is itself a conflict the register names.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)

        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="signs the Indiana filing while the director is away",
        )

        assert APPROVE in permissions_for_user(session, COMPANY, analyst.id)


def test_the_ceiling_reads_direct_grants_too_not_only_roles():
    # The deputy's user.manage came from a direct grant. If the ceiling read
    # only role grants, they could not use the authority they were given.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        assert MANAGE not in permissions_for_role(session, COMPANY, ROLE_OBLIGATION_OWNER)

        role = perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Deputy Director",
            codes=(APPROVE, "claim.read"),
            reason="the deputy signs when the director is away",
        )
        assert role.created_by_user_id == deputy.id


def test_nobody_without_user_manage_may_grant_anything():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        # The analyst holds action.propose, so this is not a ceiling refusal --
        # it is the gate in front of the whole module.
        assert PROPOSE in permissions_for_user(session, COMPANY, analyst.id)

        with pytest.raises(policy.PermissionDenied):
            perms.grant(
                session,
                COMPANY,
                actor=analyst.id,
                user_id=owner.id,
                code=PROPOSE,
                reason="sharing my own permission",
            )
        with pytest.raises(policy.PermissionDenied):
            perms.create_role(
                session,
                COMPANY,
                actor=analyst.id,
                name="Analyst II",
                codes=(PROPOSE,),
                reason="a title we actually use",
            )


def test_an_unknown_actor_is_refused_and_the_refusal_is_recorded():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(policy.PermissionDenied):
            perms.grant(
                session,
                COMPANY,
                actor="usr-nobody",
                user_id=analyst.id,
                code="audit.read",
                reason="from nowhere",
            )
        assert _denials(session)


def test_an_email_passed_as_the_actor_is_refused_by_name():
    # Both arguments are user ids and one of them is the person being granted
    # to. A display string in either is a call-site bug, and it fails with a
    # sentence rather than as a permission problem nobody can reproduce.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(ValueError) as error:
            perms.grant(
                session,
                COMPANY,
                actor=admin.email,
                user_id=analyst.id,
                code="audit.read",
                reason="looks right, is not",
            )
        assert "user id" in str(error.value)


# ---------------------------------------------------------------------------
# What a grant must carry
# ---------------------------------------------------------------------------


def test_a_permission_change_with_no_reason_is_refused_everywhere():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for blank in ("", "   ", None):
            with pytest.raises(ValueError):
                perms.grant(
                    session,
                    COMPANY,
                    actor=admin.id,
                    user_id=analyst.id,
                    code="audit.read",
                    reason=blank,
                )
            with pytest.raises(ValueError):
                perms.create_role(
                    session,
                    COMPANY,
                    actor=admin.id,
                    name="Analyst II",
                    codes=("claim.read",),
                    reason=blank,
                )
        assert "audit.read" not in permissions_for_user(session, COMPANY, analyst.id)


def test_a_code_the_product_does_not_define_is_refused_before_the_insert():
    # The foreign key documents the join and enforces nothing: no path in this
    # codebase issues PRAGMA foreign_keys=ON.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for bogus in ("action.aprove", "", None, "user.manage "):
            with pytest.raises(ValueError):
                perms.grant(
                    session,
                    COMPANY,
                    actor=admin.id,
                    user_id=analyst.id,
                    code=bogus,
                    reason="a typo nobody would notice",
                )
        assert perms.direct_grants_for_user(session, COMPANY, analyst.id) == []


def test_a_grant_to_another_tenants_user_is_refused():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, rival_analyst, _ = _bootstrap(session, company=RIVAL)
        with pytest.raises(ValueError):
            perms.grant(
                session,
                COMPANY,
                actor=admin.id,
                user_id=rival_analyst.id,
                code="audit.read",
                reason="not ours to give",
            )


def test_granting_twice_writes_one_row_and_one_event():
    # A double click is not a decision. The partial unique index says the same
    # thing at the storage layer; this is the half that keeps the message clear.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        first = perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        before = event_count(session, COMPANY)
        second = perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )

        assert first.id == second.id
        assert event_count(session, COMPANY) == before
        assert len(perms.direct_grants_for_user(session, COMPANY, analyst.id)) == 1


def test_a_second_grant_with_a_different_reason_is_refused_not_swallowed():
    # The standing sentence is the one that granted the authority. Keeping it
    # while reporting success would drop the new justification silently, in the
    # one field this table exists to carry.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        with pytest.raises(ValueError) as error:
            perms.grant(
                session,
                COMPANY,
                actor=admin.id,
                user_id=analyst.id,
                code="audit.read",
                reason="a different sentence, same authority",
            )
        assert "reconciles the chain" in str(error.value)

        # The way through, and it keeps both sentences.
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="restating why they hold it",
        )
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="a different sentence, same authority",
        )
        history = perms.direct_grants_for_user(session, COMPANY, analyst.id)
        assert [row.reason for row in history] == [
            "reconciles the chain",
            "a different sentence, same authority",
        ]


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revoking_takes_the_permission_away_and_keeps_the_record():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        row = perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="the quarter is closed",
        )

        assert "audit.read" not in permissions_for_user(session, COMPANY, analyst.id)
        # Written together or not at all -- the check constraint says so and so
        # does the writer.
        assert row.revoked_at is not None
        assert row.revoked_by_user_id == admin.id
        assert row.reason == "reconciles the chain"
        history = perms.direct_grants_for_user(session, COMPANY, analyst.id)
        assert len(history) == 1
        assert perms.direct_grants_for_user(
            session, COMPANY, analyst.id, include_revoked=False
        ) == []


def test_a_revoke_and_a_regrant_are_two_rows():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="first time",
        )
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="the quarter is closed",
        )
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="second time, new quarter",
        )

        history = perms.direct_grants_for_user(session, COMPANY, analyst.id)
        assert len(history) == 2
        assert [row.reason for row in history] == [
            "first time",
            "second time, new quarter",
        ]
        assert "audit.read" in permissions_for_user(session, COMPANY, analyst.id)


def test_revoking_a_permission_nobody_holds_directly_raises_rather_than_passing():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        # The analyst holds action.propose through the analyst ROLE. Revoking it
        # here must not look like it worked.
        with pytest.raises(ValueError) as error:
            perms.revoke(
                session,
                COMPANY,
                actor=admin.id,
                user_id=analyst.id,
                code=PROPOSE,
                reason="tidying up",
            )
        assert ROLE_ANALYST in str(error.value)
        assert PROPOSE in permissions_for_user(session, COMPANY, analyst.id)


def test_revoking_a_direct_grant_that_a_role_also_carries_announces_itself():
    # The screen would otherwise show a revocation that changed nothing.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=owner.id,
            code=APPROVE,
            reason="named on the Indiana filing as well",
        )
        perms.revoke(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=owner.id,
            code=APPROVE,
            reason="the Indiana filing is in",
        )

        assert APPROVE in permissions_for_user(session, COMPANY, owner.id)
        last = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert "still holds" in last.reason
        assert ROLE_OBLIGATION_OWNER in last.reason


def test_revoking_needs_no_ceiling():
    # Taking authority away is not escalation. An admin who cannot hold
    # action.approve must still be able to take it off somebody.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="signs while the director is away",
        )
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="the director is back",
        )
        assert APPROVE not in permissions_for_user(session, COMPANY, analyst.id)


# ---------------------------------------------------------------------------
# A company's own roles
# ---------------------------------------------------------------------------


def test_a_company_composes_a_role_and_it_carries_its_author_and_its_date():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        role = perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read", "change.read", "proceeding.read"),
            reason="a title in data/real/ that the three roles cannot say",
            description="Reads the record. Signs nothing.",
        )

        assert role.company_id == COMPANY
        assert role_is_system(role) is False
        assert role.created_by_user_id == admin.id
        assert role.created_at is not None
        assert role.created_at.tzinfo is not None
        # Composed from scratch. NULL here means exactly that and never "unknown".
        assert role.derived_from_role_id is None
        assert permissions_for_role(session, COMPANY, "Legal Assistant") == frozenset(
            {"claim.read", "change.read", "proceeding.read"}
        )


def test_a_composed_role_grants_what_it_says_when_somebody_holds_it():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read", "change.read"),
            reason="a title in data/real/",
        )
        clerk = _person(session, "clerk@mep.example", None, name="Casey Lin")
        grant_role(
            session, COMPANY, user_id=clerk.id, role_name="Legal Assistant", actor=SEED
        )

        assert permissions_for_user(session, COMPANY, clerk.id) == frozenset(
            {"claim.read", "change.read"}
        )
        held = perms.effective_permissions(session, COMPANY, clerk.id)
        assert {g.role_name for g in held} == {"Legal Assistant"}
        assert all(g.via == perms.VIA_ROLE for g in held)


def test_a_company_role_may_not_take_a_system_role_name():
    # The quiet edit: (company_id='MEP', name='analyst') would silently redefine
    # what every grant of "analyst" means in that tenant. The CHECK constraint
    # says so on a fresh database; this says so where the live database is.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for taken in (ROLE_ANALYST, ROLE_ADMIN, ROLE_OBLIGATION_OWNER):
            with pytest.raises(ValueError) as error:
                perms.create_role(
                    session,
                    COMPANY,
                    actor=admin.id,
                    name=taken,
                    codes=("claim.read",),
                    reason="narrowing it for us",
                )
            assert "system role" in str(error.value)
        assert perms.composed_roles(session, COMPANY) == []


def test_two_roles_cannot_share_a_name_in_one_company():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Reviewer",
            codes=("claim.read",),
            reason="first",
        )
        with pytest.raises(ValueError):
            perms.create_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Reviewer",
                codes=("change.read",),
                reason="second",
            )
        # And the same name in another tenant is a different role entirely.
        rival_admin, _, _ = _bootstrap(session, company=RIVAL)
        perms.create_role(
            session,
            RIVAL,
            actor=rival_admin.id,
            name="Legal Reviewer",
            codes=("change.read",),
            reason="theirs",
        )
        assert len(perms.composed_roles(session, COMPANY)) == 1
        assert len(perms.composed_roles(session, RIVAL)) == 1


def test_a_name_that_only_reads_the_same_is_refused_too():
    # SQLite compares text case-sensitively, so the unique index and the CHECK
    # constraint both accept "Analyst" beside "analyst". Two roles nobody can
    # tell apart on a list is the same failure as one role redefined.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(ValueError) as error:
            perms.create_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Analyst",
                codes=("claim.read",),
                reason="ours is different",
            )
        assert "system role" in str(error.value)

        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Reviewer",
            codes=("claim.read",),
            reason="first",
        )
        with pytest.raises(ValueError):
            perms.create_role(
                session,
                COMPANY,
                actor=admin.id,
                name="legal reviewer",
                codes=("change.read",),
                reason="looks like a second role and is not",
            )
        assert len(perms.composed_roles(session, COMPANY)) == 1


def test_a_role_that_carries_nothing_is_refused():
    # A role granting nothing is a label that looks like authority.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for empty in ((), [], None):
            with pytest.raises(ValueError):
                perms.create_role(
                    session,
                    COMPANY,
                    actor=admin.id,
                    name="Placeholder",
                    codes=empty,
                    reason="we will fill it in later",
                )


def test_editing_a_system_role_is_refused_and_does_not_fork_behind_your_back():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(ValueError) as error:
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                name=ROLE_ANALYST,
                codes=("claim.read",),
                reason="our analysts do less than that",
            )

        assert "fork" in str(error.value).lower()
        # Unchanged, and no copy invented on the way past.
        assert permissions_for_role(session, COMPANY, ROLE_ANALYST) == frozenset(
            SYSTEM_ROLE_PERMISSIONS[ROLE_ANALYST]
        )
        assert perms.composed_roles(session, COMPANY) == []


def test_a_fork_records_where_it_came_from():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)

        forked = perms.fork_role(
            session,
            COMPANY,
            actor=deputy.id,
            source_role=ROLE_OBLIGATION_OWNER,
            name="Legal Reviewer",
            reason="counsel approves but does not read the audit chain",
            codes=tuple(
                code
                for code in SYSTEM_ROLE_PERMISSIONS[ROLE_OBLIGATION_OWNER]
                if code != "audit.read"
            ),
        )

        origin = session.get(Role, forked.derived_from_role_id)
        assert origin is not None
        assert origin.name == ROLE_OBLIGATION_OWNER
        assert role_is_system(origin) is True
        codes = permissions_for_role(session, COMPANY, "Legal Reviewer")
        assert "audit.read" not in codes
        assert APPROVE in permissions_for_role(session, COMPANY, "Legal Reviewer")


def test_a_fork_with_no_codes_copies_the_role_it_started_from():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        forked = perms.fork_role(
            session,
            COMPANY,
            actor=deputy.id,
            source_role=ROLE_OBLIGATION_OWNER,
            name="Indiana Regulatory Counsel",
            reason="same authority, a name the company recognises",
        )
        assert permissions_for_role(
            session, COMPANY, "Indiana Regulatory Counsel"
        ) == frozenset(SYSTEM_ROLE_PERMISSIONS[ROLE_OBLIGATION_OWNER])
        assert forked.derived_from_role_id is not None


def test_editing_a_composed_role_asserts_the_whole_set():
    # Principle 27: a code dropped from the intended set is dropped from the
    # role, rather than lingering as a permission the product no longer believes
    # it grants.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read", "change.read", "proceeding.read"),
            reason="a title in data/real/",
        )
        perms.edit_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="they do not need the diff view",
            description="Reads claims. Nothing else.",
        )

        role = perms.composed_roles(session, COMPANY)[0]
        assert permissions_for_role(session, COMPANY, "Legal Assistant") == frozenset(
            {"claim.read"}
        )
        assert role.description == "Reads claims. Nothing else."
        rows = (
            session.query(RolePermission)
            .filter(RolePermission.role_id == role.id)
            .all()
        )
        assert len(rows) == 1


def test_an_edit_that_changes_nothing_writes_nothing():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read", "change.read"),
            reason="a title in data/real/",
            description="Reads the record.",
        )
        before = event_count(session, COMPANY)
        perms.edit_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("change.read", "claim.read"),
            reason="pressed save twice",
            description="Reads the record.",
        )
        assert event_count(session, COMPANY) == before


def test_another_tenants_role_cannot_be_edited_or_deleted_from_here():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, _, _ = _bootstrap(session, company=RIVAL)
        perms.create_role(
            session,
            RIVAL,
            actor=rival_admin.id,
            name="Legal Reviewer",
            codes=("claim.read",),
            reason="theirs",
        )

        with pytest.raises(ValueError):
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Reviewer",
                codes=("change.read",),
                reason="reaching across the fence",
            )
        assert permissions_for_role(session, RIVAL, "Legal Reviewer") == frozenset(
            {"claim.read"}
        )


def test_deleting_a_role_nobody_ever_held_takes_its_permission_rows_with_it():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        role = perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Typo Reviewer",
            codes=("claim.read",),
            reason="composed by mistake",
        )
        role_id = role.id
        perms.delete_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Typo Reviewer",
            reason="composed by mistake, nobody was ever given it",
        )

        assert perms.composed_roles(session, COMPANY) == []
        assert (
            session.query(RolePermission)
            .filter(RolePermission.role_id == role_id)
            .count()
            == 0
        )


def test_a_role_somebody_has_held_is_not_deleted():
    # The grant row would be left pointing at nothing, and what it granted in
    # March would become unanswerable.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="a title in data/real/",
        )
        clerk = _person(session, "clerk@mep.example", None, name="Casey Lin")
        grant_role(
            session, COMPANY, user_id=clerk.id, role_name="Legal Assistant", actor=SEED
        )
        revoke_role(
            session, COMPANY, user_id=clerk.id, role_name="Legal Assistant", actor=SEED
        )

        # Even after the grant is revoked: the revoked row still needs the role
        # to say what it meant.
        with pytest.raises(ValueError) as error:
            perms.delete_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Assistant",
                reason="reorganised",
            )
        assert "held" in str(error.value)
        assert len(perms.composed_roles(session, COMPANY)) == 1


def test_a_role_another_role_was_forked_from_is_not_deleted():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="a title in data/real/",
        )
        perms.fork_role(
            session,
            COMPANY,
            actor=admin.id,
            source_role="Legal Assistant",
            name="Legal Assistant II",
            reason="a senior version of the same job",
        )
        with pytest.raises(ValueError):
            perms.delete_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Assistant",
                reason="superseded",
            )


def test_a_role_can_be_named_by_its_id_because_that_is_what_a_form_posts():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        role = perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="a title in data/real/",
        )
        perms.edit_role(
            session,
            COMPANY,
            actor=admin.id,
            role_id=role.id,
            codes=("claim.read", "change.read"),
            reason="they read the diff after all",
        )
        assert permissions_for_role(session, COMPANY, "Legal Assistant") == frozenset(
            {"claim.read", "change.read"}
        )

        # Both selectors at once is a caller that has not decided.
        with pytest.raises(ValueError):
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Legal Assistant",
                role_id=role.id,
                codes=("claim.read",),
                reason="two ways of saying one thing",
            )

        perms.delete_role(
            session,
            COMPANY,
            actor=admin.id,
            role_id=role.id,
            reason="composed by mistake",
        )
        assert perms.composed_roles(session, COMPANY) == []


def test_a_system_role_id_is_refused_the_same_way_its_name_is():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        system = role_for_company(session, COMPANY, ROLE_ANALYST)
        with pytest.raises(ValueError) as error:
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                role_id=system.id,
                codes=("claim.read",),
                reason="round the back",
            )
        assert "fork" in str(error.value).lower()


def test_another_tenants_role_id_reads_as_absent_rather_than_as_forbidden():
    # Which of the two it is would itself tell a caller that somebody else holds
    # that id.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, _, _ = _bootstrap(session, company=RIVAL)
        theirs = perms.create_role(
            session,
            RIVAL,
            actor=rival_admin.id,
            name="Legal Reviewer",
            codes=("claim.read",),
            reason="theirs",
        )
        with pytest.raises(ValueError) as error:
            perms.edit_role(
                session,
                COMPANY,
                actor=admin.id,
                role_id=theirs.id,
                codes=("change.read",),
                reason="reaching across the fence",
            )
        assert "composed by this company" in str(error.value)


def test_a_fork_can_name_its_origin_by_id_too():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        origin = role_for_company(session, COMPANY, ROLE_OBLIGATION_OWNER)

        forked = perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Deputy Director",
            codes=(APPROVE, "claim.read"),
            reason="the deputy signs while the director is away",
            derived_from_role_id=origin.id,
        )
        assert forked.derived_from_role_id == origin.id

        with pytest.raises(ValueError):
            perms.create_role(
                session,
                COMPANY,
                actor=deputy.id,
                name="Another",
                codes=("claim.read",),
                reason="two ways of saying one thing",
                derived_from=ROLE_OBLIGATION_OWNER,
                derived_from_role_id=origin.id,
            )


def test_the_company_wide_read_is_the_auditors_one_and_is_scoped():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, rival_analyst, _ = _bootstrap(session, company=RIVAL)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        perms.grant(
            session,
            RIVAL,
            actor=rival_admin.id,
            user_id=rival_analyst.id,
            code="audit.read",
            reason="their business",
        )

        ours = perms.direct_grants_for_company(session, COMPANY)
        assert [row.user_id for row in ours] == [analyst.id]

        # A suspended person's exception stays on the register: it is the record
        # of what somebody granted, and it takes effect again when the account
        # does. permissions_for_user is where "in force" is decided.
        set_user_status(session, COMPANY, analyst.id, STATUS_SUSPENDED, SEED)
        still_listed = perms.direct_grants_for_company(session, COMPANY)
        assert [row.user_id for row in still_listed] == [analyst.id]
        assert "audit.read" not in permissions_for_user(session, COMPANY, analyst.id)

        # Revoked rows are out by default and back on request.
        set_user_status(session, COMPANY, analyst.id, STATUS_ACTIVE, SEED)
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="the quarter is closed",
        )
        assert perms.direct_grants_for_company(session, COMPANY) == []
        with_history = perms.direct_grants_for_company(
            session, COMPANY, include_revoked=True
        )
        assert len(with_history) == 1


def test_a_system_role_is_never_deleted():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        with pytest.raises(ValueError):
            perms.delete_role(
                session,
                COMPANY,
                actor=admin.id,
                name=ROLE_ANALYST,
                reason="we do not use it",
            )
        assert permissions_for_role(session, COMPANY, ROLE_ANALYST) == frozenset(
            SYSTEM_ROLE_PERMISSIONS[ROLE_ANALYST]
        )


def test_a_fork_must_say_what_it_started_from():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        for missing in ("", None):
            with pytest.raises(ValueError):
                perms.fork_role(
                    session,
                    COMPANY,
                    actor=admin.id,
                    source_role=missing,
                    name="Something",
                    reason="from nowhere",
                )
        with pytest.raises(ValueError) as error:
            perms.fork_role(
                session,
                COMPANY,
                actor=admin.id,
                source_role="Role That Does Not Exist",
                name="Something",
                reason="from a role nobody composed",
            )
        assert "start from" in str(error.value)
        # And the same by id.
        with pytest.raises(ValueError):
            perms.create_role(
                session,
                COMPANY,
                actor=admin.id,
                name="Something",
                codes=("claim.read",),
                reason="from an id nobody issued",
                derived_from_role_id="role-nobody",
            )


def test_revoking_from_another_tenants_user_is_refused():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        rival_admin, rival_analyst, _ = _bootstrap(session, company=RIVAL)
        perms.grant(
            session,
            RIVAL,
            actor=rival_admin.id,
            user_id=rival_analyst.id,
            code="audit.read",
            reason="their business",
        )
        with pytest.raises(ValueError):
            perms.revoke(
                session,
                COMPANY,
                actor=admin.id,
                user_id=rival_analyst.id,
                code="audit.read",
                reason="not ours to take",
            )
        assert "audit.read" in permissions_for_user(session, RIVAL, rival_analyst.id)


def test_revoking_from_a_suspended_account_says_what_it_cannot_report():
    # A suspended account holds nothing, so "do they still have it through a
    # role" has no answer to give. The silence is explained rather than left.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        set_user_status(session, COMPANY, analyst.id, STATUS_SUSPENDED, SEED)
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="they have left",
        )
        last = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert STATUS_SUSPENDED in last.reason
        assert perms.direct_grants_for_user(session, COMPANY, analyst.id)[0].revoked_at


def test_a_grant_says_in_one_line_where_it_came_from():
    # The sentence a screen prints and an export carries. Never parsed.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        said = {row.code: row.sentence() for row in
                perms.effective_permissions(session, COMPANY, analyst.id)}
        assert said[PROPOSE] == f"{PROPOSE} through the role {ROLE_ANALYST}"
        assert said["audit.read"] == "audit.read directly (reconciles the chain)"


def test_a_read_with_no_user_returns_nothing_rather_than_everything():
    init_db()
    with session_scope() as session:
        _bootstrap(session)
        assert perms.direct_grants_for_user(session, COMPANY, "") == []
        assert perms.effective_permissions(session, COMPANY, "") == ()
        assert perms.conflicts_for_user(session, COMPANY, "") == ()
        assert perms.effective_permissions(session, COMPANY, "usr-nobody") == ()
        assert perms.conflicts_for_user(session, COMPANY, "usr-nobody") == ()


# ---------------------------------------------------------------------------
# Conflicts: reported, never refused
# ---------------------------------------------------------------------------


def test_a_person_may_hold_both_sides_and_the_register_says_who_and_why():
    # A four-person regulatory team is a real customer. The product records the
    # decision; it does not overrule it.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="four people work here and somebody has to sign",
        )

        # It worked. That is half the test.
        assert APPROVE in permissions_for_user(session, COMPANY, analyst.id)

        register = perms.conflicts(session, COMPANY)
        mine = [row for row in register if row.user_id == analyst.id]
        assert len(mine) == 1
        found = mine[0]
        assert (found.left, found.right) == (PROPOSE, APPROVE)
        assert found.email == analyst.email
        assert "approves it" in found.why
        # How each side was obtained. The question the register exists for.
        assert [g.via for g in found.left_grants] == [perms.VIA_ROLE]
        assert found.left_grants[0].role_name == ROLE_ANALYST
        assert [g.via for g in found.right_grants] == [perms.VIA_DIRECT]
        assert "somebody has to sign" in found.right_grants[0].reason


def test_the_register_names_the_pair_that_lets_a_person_grant_themselves_the_rest():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)

        register = perms.conflicts(session, COMPANY)
        pairs = {(row.user_id, row.left, row.right) for row in register}
        assert (deputy.id, MANAGE, APPROVE) in pairs
        # And the admin, who holds user.manage and no approval, is not in it.
        assert admin.id not in {row.user_id for row in register}


def test_a_conflict_carried_by_one_composed_role_is_reported_too():
    # The role is the company's decision. The register still names it, because
    # the auditor's question is what a person holds, not how tidily.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        # The person composing it has to hold both halves themselves, which in a
        # four-person team is the founder. The ceiling is what makes that true.
        grant_role(
            session, COMPANY, user_id=deputy.id, role_name=ROLE_ANALYST, actor=SEED
        )
        perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Manager Regulatory Affairs",
            codes=(PROPOSE, APPROVE, "claim.read"),
            reason="the manager proposes and signs in a team this size",
        )
        manager = _person(session, "manager@mep.example", None, name="Morgan Vale")
        grant_role(
            session,
            COMPANY,
            user_id=manager.id,
            role_name="Manager Regulatory Affairs",
            actor=SEED,
        )

        found = [
            row for row in perms.conflicts(session, COMPANY) if row.user_id == manager.id
        ]
        assert len(found) == 1
        assert (found[0].left, found[0].right) == (PROPOSE, APPROVE)
        assert found[0].left_grants[0].role_name == "Manager Regulatory Affairs"
        assert found[0].right_grants[0].role_name == "Manager Regulatory Affairs"


def test_a_clean_company_has_an_empty_register():
    init_db()
    with session_scope() as session:
        _bootstrap(session)
        assert perms.conflicts(session, COMPANY) == ()


def test_a_suspended_account_drops_out_of_the_register():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        assert perms.conflicts(session, COMPANY)

        set_user_status(session, COMPANY, deputy.id, STATUS_SUSPENDED, SEED)
        assert perms.conflicts(session, COMPANY) == ()


def test_the_conflict_vocabulary_is_the_one_in_models_not_a_second_copy():
    # A second list of pairs would disagree with the first, and the screen would
    # show whichever one it imported.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        register = perms.conflicts(session, COMPANY)
        declared = {(left, right, why) for left, right, why in conflicts_in(
            permissions_for_user(session, COMPANY, deputy.id)
        )}
        assert {(row.left, row.right, row.why) for row in register} <= declared


def test_a_grant_that_creates_a_conflict_says_so_in_the_audit_chain():
    # The disclosure lands in the append-only record at the moment the conflict
    # is created, not only on a screen somebody has to open.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="four people work here",
        )
        last = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert PROPOSE in last.reason
        assert "CONFLICT" in last.reason.upper()


# ---------------------------------------------------------------------------
# One log, and it still verifies
# ---------------------------------------------------------------------------


def test_every_write_lands_in_the_existing_chain_and_the_chain_verifies():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        before = event_count(session, COMPANY)

        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
        )
        perms.revoke(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="the quarter is closed",
        )
        perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="a title in data/real/",
        )
        perms.edit_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read", "change.read"),
            reason="they read the diff after all",
        )
        perms.delete_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            reason="composed in the wrong company",
        )

        assert event_count(session, COMPANY) == before + 5
        assert verify_chain(session, COMPANY) is True


def test_every_permission_event_names_the_actor_the_reason_and_the_subject():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain for the quarterly report",
        )
        row = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )

        assert row.action == perms.ACTION_PERMISSION_GRANTED
        assert row.actor == admin.email
        assert row.actor_user_id == admin.id
        assert row.actor_kind == "user"
        assert row.subject_type == "user"
        assert row.subject_id == analyst.id
        assert "quarterly report" in row.reason
        assert "audit.read" in row.reason


def test_the_role_events_name_the_role_and_the_codes():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        role = perms.create_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("claim.read",),
            reason="a title in data/real/",
        )
        created = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert created.action == perms.ACTION_ROLE_CREATED
        assert created.subject_type == "role"
        assert created.subject_id == role.id
        assert "Legal Assistant" in created.reason
        assert "claim.read" in created.reason

        perms.edit_role(
            session,
            COMPANY,
            actor=admin.id,
            name="Legal Assistant",
            codes=("change.read",),
            reason="a different job than we thought",
        )
        edited = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert edited.action == perms.ACTION_ROLE_EDITED
        # Both directions named, because "what changed" is the question.
        assert "change.read" in edited.reason
        assert "claim.read" in edited.reason


def test_audit_declares_the_five_codes_and_permissions_defines_none_of_them():
    """app/state/audit.py owns the audited vocabulary. One spelling, one file.

    These five were parked in permissions.py behind getattr for the hour audit.py
    had not grown them yet. That parking is gone, so the drift test that guarded
    it is gone with it -- and this replaces it with the assertion that actually
    still has a way to fail: that audit.py declares all five, and that nothing
    re-introduces a local copy.

    A misspelt action code never raises. It writes a row that hash-verifies
    perfectly and that no query for that action will ever return, which is worse
    than a missing row -- the log looks complete and answers wrongly.
    """
    import ast
    import pathlib

    from app.state import audit

    names = (
        "ACTION_PERMISSION_GRANTED",
        "ACTION_PERMISSION_REVOKED",
        "ACTION_ROLE_CREATED",
        "ACTION_ROLE_EDITED",
        "ACTION_ROLE_DELETED",
    )

    for name in names:
        assert hasattr(audit, name), (
            f"app/state/audit.py no longer declares {name}, which "
            "app/state/permissions.py imports and writes to the chain."
        )
        assert getattr(perms, name) == getattr(audit, name)

    # And permissions.py must not assign any of them itself. Read as source
    # rather than through the module, because an assignment that happens to
    # match today still drifts the day audit.py changes its string.
    source = pathlib.Path(perms.__file__).read_text(encoding="utf-8")
    assigned = {
        target.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    clashes = sorted(assigned & set(names))
    assert not clashes, (
        f"app/state/permissions.py assigns {clashes} rather than importing "
        "them. app/state/audit.py owns the vocabulary; a second spelling of an "
        "action code is invisible until an investigation comes up empty."
    )


# ---------------------------------------------------------------------------
# The grid stays the default it always was
# ---------------------------------------------------------------------------


def test_the_shipped_grid_is_untouched_by_any_of_this():
    # SYSTEM_ROLE_PERMISSIONS is what a company starts from. Composing roles and
    # granting exceptions must not edit the default every document describes.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)
        perms.grant(
            session,
            COMPANY,
            actor=deputy.id,
            user_id=analyst.id,
            code=APPROVE,
            reason="a small team",
        )
        perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Deputy Director",
            codes=(APPROVE, "claim.read"),
            reason="a real title",
        )

        assert APPROVE not in SYSTEM_ROLE_PERMISSIONS[ROLE_ADMIN]
        assert APPROVE not in SYSTEM_ROLE_PERMISSIONS[ROLE_ANALYST]
        for name, codes in SYSTEM_ROLE_PERMISSIONS.items():
            assert permissions_for_role(session, COMPANY, name) == frozenset(codes)
        # And nothing here invented a permission code.
        held = {
            row.code
            for row in session.query(UserPermission).all()
        }
        assert held <= set(PERMISSION_CODES)


def test_a_new_company_can_reach_an_approving_role_in_two_steps():
    # The ceiling looks like a deadlock: a tenant's first admin holds no
    # approval, so they cannot compose a role that approves. Authority enters
    # through the system roles instead, and everything after is arranged by
    # somebody who already holds what they are arranging.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        founder = _person(session, "founder@mep.example", ROLE_ADMIN, name="Fran Okoye")

        with pytest.raises(policy.PermissionDenied):
            perms.create_role(
                session,
                COMPANY,
                actor=founder.id,
                name="Certifying officer",
                codes=("claim.read", APPROVE),
                reason="somebody has to sign",
            )

        # Step one: the system role that carries approval, granted the way roles
        # have always been granted.
        grant_role(
            session,
            COMPANY,
            user_id=founder.id,
            role_name=ROLE_OBLIGATION_OWNER,
            actor=SEED,
        )
        # Step two: now they hold it, they may compose with it.
        perms.create_role(
            session,
            COMPANY,
            actor=founder.id,
            name="Certifying officer",
            codes=("claim.read", APPROVE),
            reason="signs what leaves the company",
        )
        assert APPROVE in permissions_for_role(session, COMPANY, "Certifying officer")
        # And the founder now holds both sides, which the register says out loud.
        assert (founder.id, MANAGE, APPROVE) in {
            (row.user_id, row.left, row.right)
            for row in perms.conflicts(session, COMPANY)
        }


def test_the_seed_route_defect_is_expressible_now():
    # scripts/seed_route.py sends "Legal review" and "Officer signs the filing"
    # to role:admin because no other role exists. Two composed roles, two people,
    # and the route can name them apart.
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        deputy = _deputy_admin(session, owner, admin)

        perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Indiana Regulatory Counsel",
            codes=("claim.read", "change.read", APPROVE),
            reason="legal review is a different desk from the officer's",
        )
        perms.create_role(
            session,
            COMPANY,
            actor=deputy.id,
            name="Officer",
            codes=("claim.read", APPROVE),
            reason="the officer signs the filing",
        )

        counsel = _person(session, "counsel@mep.example", None, name="Robin Shah")
        officer = _person(session, "officer@mep.example", None, name="Sam Ortiz")
        grant_role(
            session,
            COMPANY,
            user_id=counsel.id,
            role_name="Indiana Regulatory Counsel",
            actor=SEED,
        )
        grant_role(session, COMPANY, user_id=officer.id, role_name="Officer", actor=SEED)

        assert APPROVE in permissions_for_user(session, COMPANY, counsel.id)
        assert APPROVE in permissions_for_user(session, COMPANY, officer.id)
        # Neither of them draws the route. That stays with the admin.
        assert "workflow.manage" not in permissions_for_user(session, COMPANY, counsel.id)
        assert "workflow.manage" not in permissions_for_user(session, COMPANY, officer.id)
        assert "workflow.manage" in permissions_for_user(session, COMPANY, admin.id)
        assert APPROVE not in permissions_for_user(session, COMPANY, admin.id)


def test_a_grant_carries_an_aware_timestamp():
    init_db()
    with session_scope() as session:
        admin, analyst, owner = _bootstrap(session)
        row = perms.grant(
            session,
            COMPANY,
            actor=admin.id,
            user_id=analyst.id,
            code="audit.read",
            reason="reconciles the chain",
            granted_at=datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc),
        )
        assert row.granted_at.tzinfo is not None

        with pytest.raises(ValueError):
            perms.grant(
                session,
                COMPANY,
                actor=admin.id,
                user_id=owner.id,
                code="audit.read",
                reason="reconciles the chain",
                granted_at=datetime(2026, 8, 4, 9, 30),
            )
