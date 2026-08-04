"""Pulling a colleague onto the item that is waiting for them, and refusing to guess.

THE GAP THIS CLOSES. app/state/routing.py already walks an escalation to the
claim, the claim to the change, the change to the obligation and the obligation
to its owner, and it already refuses correctly at every hop that breaks. One of
those refusals is not a dead end at all: the obligation's owner is a real person
named in data/company_context.json who simply has no account here. Today that
item sits in the shared queue for ever. It should reach the person.

WHAT MUST NOT HAPPEN WHILE CLOSING IT. Three things, and every test below is
one of them:

1. NO ADDRESS IS EVER INVENTED. The corpus carries owner_name and owner_title
   and no email at all -- grep it. app/seed.py builds first.last@mep.example for
   the demo logins, which is fine for accounts nobody mails; deriving a real
   colleague's address the same way is the same guess as a citation the product
   refuses to make, and an address inferred from a name pattern bounced on this
   project this morning. So the address is supplied by the person inviting, and
   its absence is a refusal.

2. AN INVITE IS NOT A PRIVILEGE-ESCALATION PATH. Acceptance grants
   obligation_owner and nothing else, whoever invited and whatever they hold.
   Two tests prove it directly and one proves the invited account reaches no
   other tenant.

3. "ASSIGNED" AND "ASSIGNED BUT NOT YET ACCEPTED" ARE DIFFERENT FACTS. A screen
   that renders them alike is lying about whether the work is moving. The
   difference is carried as data on Resolution -- pending_acceptance,
   invitation_id, invited_at -- so no surface has to parse a sentence to find it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.sessions import AuthFailed, login
from app.state.audit import event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    create_user,
    ensure_system_roles,
    grant_role,
    normalise_email,
    permissions_for_user,
    user_by_email,
    user_for_company,
)
from app.state.models import (
    INVITE_ACCEPTED,
    INVITE_APPROVED,
    INVITE_AWAITING_APPROVAL,
    INVITE_PENDING,
    INVITE_REVOKED,
    INVITE_SUBJECT_ESCALATION,
    INVITE_SUBJECT_OBLIGATION,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    AuditEvent,
    Change,
    Claim,
    Escalation,
    Invitation,
    Role,
    RolePermission,
)
from app.state.invites import (
    ACCEPT_OK,
    ACCEPT_PASSWORD,
    ACCEPT_REASON_CODES,
    ACCEPT_REFUSED,
    DOMAIN_FROM_ACCOUNTS,
    DOMAIN_FROM_CALLER,
    DOMAIN_UNDECIDED_NONE,
    DOMAIN_UNDECIDED_SEVERAL,
    ENV_INVITES_ENABLED,
    ENV_INVITES_NEED_APPROVAL,
    INV_ALREADY_A_USER,
    INV_ALREADY_ACCEPTED,
    INV_ALREADY_LIVE,
    INV_DISABLED,
    INV_EMAIL_MALFORMED,
    INV_EXPIRED,
    INV_NO_EMAIL,
    INV_NO_INVITER,
    INV_NO_NAME,
    INV_NO_SUBJECT,
    INV_NOT_A_GAP,
    INV_NOT_PERMITTED,
    INV_NOT_QUEUED,
    INV_OBLIGATIONS_AMBIGUOUS,
    INV_OK,
    INV_QUEUED,
    INV_REASON_CODES,
    INV_REVOKED_OK,
    INV_UNKNOWN,
    INV_WAS_REVOKED,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    accept_invitation,
    approve_invitation,
    company_domain,
    invitations_awaiting_approval,
    invitations_for_company,
    invite_owner_for_escalation,
    invite_owner_for_obligation,
    invite_policy,
    live_invitation_for_email,
    revoke_invitation,
)
from app.state.routing import (
    ACTION_ESCALATION_ROUTED_PENDING,
    ROUTE_OBLIGATION_UNOWNED,
    ROUTE_OK,
    ROUTE_OWNER_INACTIVE,
    ROUTE_PENDING_ACCEPTANCE,
    ROUTING_REASON_CODES,
    awaiting_acceptance,
    ensure_obligation,
    escalations_for_user,
    map_change_to_obligation,
    obligation_for_company,
    resolve_escalation_owner,
    route_escalation,
    shared_queue,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
DOMAIN = "mep.example"
ACTOR = "system:test"
T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
PASSWORD = "strata-test-password"
CHOSEN = "the-password-priya-chose"

# The person the corpus names on OBL-001 and has no address for.
OWNER_NAME = "Priya Nandakumar"
OWNER_EMAIL = "priya.nandakumar@mep.example"


# ---------------------------------------------------------------------------
# A small world, built the way tests/test_routing.py builds one: rows written
# directly where the row's own module is not what is under test.
# ---------------------------------------------------------------------------


def _person(session, company_id, name, role, *, domain=DOMAIN, user_id=None):
    user = create_user(
        session,
        company_id,
        email=f"{name}@{domain}",
        display_name=name.title(),
        password=PASSWORD,
        actor=ACTOR,
        user_id=user_id or f"usr-{company_id.lower()}-{name}",
        created_at=T0,
    )
    grant_role(
        session, company_id, user_id=user.id, role_name=role, actor=ACTOR, granted_at=T0
    )
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


def _may_invite(session, company_id, user):
    """Give one account user.invite, which no system role carries.

    user.invite is held apart from user.manage on purpose, so a company grants
    it through a role of its own. Building that role here rather than borrowing
    admin is what makes the permission test mean anything: this analyst can
    invite and cannot manage accounts.
    """
    role_id = f"role-{company_id.lower()}-inviter"
    if session.get(Role, role_id) is None:
        session.add(Role(id=role_id, company_id=company_id, name="inviter"))
        session.add(RolePermission(role_id=role_id, permission_id="user.invite"))
        session.flush()
    grant_role(
        session,
        company_id,
        user_id=user.id,
        role_name="inviter",
        actor=ACTOR,
        granted_at=T0,
    )
    return user


def _gap(session, company_id=COMPANY):
    """The dead end this module exists to close.

    One analyst who may invite, one obligation with NO owner account, one change
    mapped to it, one escalation. Exactly what the corpus produces: the duty has
    a name against it in data/company_context.json and nobody here to act on it.

    Ids carry the tenant where the tenant is not the corpus one. Every id in
    this schema is unique across companies, so two tenants asking for OBL-001
    is an IntegrityError rather than a test of isolation.
    """
    ensure_system_roles(session)
    analyst = _may_invite(
        session,
        company_id,
        _person(
            session,
            company_id,
            "dana",
            ROLE_ANALYST,
            domain=DOMAIN if company_id == COMPANY else f"{company_id.lower()}.example",
        ),
    )
    tag = "" if company_id == COMPANY else f"-{company_id}"
    ensure_obligation(
        session,
        company_id,
        obligation_id=f"OBL-001{tag}",
        title="Post security for new large-load interconnection work.",
        owner_user_id=None,
        actor=ACTOR,
    )
    change = _change(session, company_id, f"CHG-1{tag}")
    map_change_to_obligation(
        session,
        company_id,
        change_id=change,
        obligation_id=f"OBL-001{tag}",
        mapped_by=ACTOR,
        mapped_at=T0,
    )
    escalation = _escalation(session, company_id, f"ESC-1{tag}", change)
    return analyst, escalation


def _invite(session, *, escalation="ESC-1", email=OWNER_EMAIL, actor_user, **kwargs):
    return invite_owner_for_escalation(
        session,
        COMPANY,
        escalation_id=escalation,
        email=email,
        display_name=OWNER_NAME,
        invited_by_user_id=actor_user.id,
        actor=f"person:{actor_user.email}",
        now=kwargs.pop("now", T0),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The two tenant switches, and the fact that nothing has decided them
# ---------------------------------------------------------------------------


def test_the_policy_answers_from_a_default_and_says_which():
    """A switch nobody has set is not off and not on. It is undecided.

    models.py says this at length: a security switch that silently defaults to
    permissive is the worst instance of a fallback that does not announce
    itself. So the reader hands back the value AND where it came from, and a
    screen can say "on by default, nobody has set it".
    """
    init_db()
    with session_scope() as session:
        policy = invite_policy(session, company_id=COMPANY)
        assert policy.enabled.value is True
        assert policy.enabled.source == SOURCE_DEFAULT
        assert policy.enabled.decided is False
        assert "default" in policy.enabled.announcement.lower()
        assert policy.need_approval.value is False
        assert policy.need_approval.decided is False


def test_the_environment_decides_the_switch_and_the_answer_says_so(monkeypatch):
    init_db()
    monkeypatch.setenv(ENV_INVITES_NEED_APPROVAL, "true")
    with session_scope() as session:
        policy = invite_policy(session, company_id=COMPANY)
        assert policy.need_approval.value is True
        assert policy.need_approval.source == SOURCE_ENVIRONMENT
        assert policy.need_approval.decided is True


def test_a_switch_value_nobody_can_read_raises_rather_than_defaulting(monkeypatch):
    """A misread switch must not quietly become the permissive answer."""
    init_db()
    monkeypatch.setenv(ENV_INVITES_ENABLED, "maybe")
    with session_scope() as session:
        with pytest.raises(ValueError) as error:
            invite_policy(session, company_id=COMPANY)
        assert ENV_INVITES_ENABLED in str(error.value)


def test_an_unscoped_policy_read_is_refused():
    init_db()
    with session_scope() as session:
        with pytest.raises(ValueError):
            invite_policy(session, company_id="")


# ---------------------------------------------------------------------------
# Whose domain is the company's
# ---------------------------------------------------------------------------


def test_the_company_domain_comes_from_its_own_active_accounts():
    init_db()
    with session_scope() as session:
        _gap(session)
        verdict = company_domain(session, COMPANY)
        assert verdict.domain == DOMAIN
        assert verdict.source == DOMAIN_FROM_ACCOUNTS
        assert verdict.decided is True


def test_two_domains_among_the_accounts_leave_it_undecided_and_name_both():
    """There is no companies table, so the domain is derived. Derivation can fail.

    When it does, the answer is not a guess and not the first one sorted: it is
    "undecided", with both candidates, and the caller must fail closed.
    """
    init_db()
    with session_scope() as session:
        _gap(session)
        _person(session, COMPANY, "contractor", ROLE_ANALYST, domain="elsewhere.example")
        verdict = company_domain(session, COMPANY)
        assert verdict.domain is None
        assert verdict.source == DOMAIN_UNDECIDED_SEVERAL
        assert verdict.candidates == ("elsewhere.example", DOMAIN)
        assert verdict.decided is False


def test_a_company_with_no_accounts_has_no_derivable_domain():
    init_db()
    with session_scope() as session:
        verdict = company_domain(session, COMPANY)
        assert verdict.domain is None
        assert verdict.source == DOMAIN_UNDECIDED_NONE
        assert verdict.candidates == ()


def test_a_stated_domain_wins_over_the_derivation_and_says_where_it_came_from():
    """The seam for the settings table: one argument, not a literal at four sites."""
    init_db()
    with session_scope() as session:
        _gap(session)
        verdict = company_domain(session, COMPANY, stated="MEP.Example")
        assert verdict.domain == DOMAIN
        assert verdict.source == DOMAIN_FROM_CALLER


def test_an_invited_account_does_not_widen_the_company_domain():
    """A cross-domain invite must not vote on what the company's domain is.

    Otherwise the first queued invitation would widen the fast path that is
    supposed to be deciding it.
    """
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(
            session, email="priya@outside.example", actor_user=analyst
        )
        assert outcome.reason_code == INV_QUEUED
        assert company_domain(session, COMPANY).domain == DOMAIN


# ---------------------------------------------------------------------------
# THE REFUSALS. Every one leaves the item where it was.
# ---------------------------------------------------------------------------


def test_no_address_refuses_and_never_derives_one_from_the_name():
    """The flagship refusal. The corpus names Priya and carries no address.

    app/seed.py builds first.last@mep.example for demo logins. Doing the same
    for a real invitation is the guess this whole product refuses to make, and
    an address inferred that way bounced on this project this morning.
    """
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, email=None, actor_user=analyst)

        assert outcome.reason_code == INV_NO_EMAIL
        assert outcome.invitation_id is None
        assert session.query(Invitation).count() == 0
        # The item is exactly where it was: nobody's name on it.
        row = session.get(Escalation, escalation_id)
        assert row.assigned_to_user_id is None
        assert [item.escalation.id for item in shared_queue(session, COMPANY)] == [
            escalation_id
        ]
        assert verify_chain(session, COMPANY)


def test_a_malformed_address_refuses_rather_than_being_repaired():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, email="priya nandakumar at mep", actor_user=analyst)
        assert outcome.reason_code == INV_EMAIL_MALFORMED
        assert session.query(Invitation).count() == 0
        assert session.get(Escalation, escalation_id).assigned_to_user_id is None


def test_an_invitation_with_no_name_refuses():
    """An account nobody can name in a review is not attributable, and a name
    read off the local part of an address is the same guess as the address."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = invite_owner_for_escalation(
            session,
            COMPANY,
            escalation_id="ESC-1",
            email=OWNER_EMAIL,
            display_name="",
            invited_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0,
        )
        assert outcome.reason_code == INV_NO_NAME
        assert session.query(Invitation).count() == 0


def test_invites_switched_off_refuse_and_the_item_stays_in_the_queue(monkeypatch):
    init_db()
    monkeypatch.setenv(ENV_INVITES_ENABLED, "0")
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)
        assert outcome.reason_code == INV_DISABLED
        assert session.query(Invitation).count() == 0
        assert [item.escalation.id for item in shared_queue(session, COMPANY)] == [
            escalation_id
        ]
        assert verify_chain(session, COMPANY)


def test_an_account_without_the_permission_cannot_invite():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        _gap(session)
        plain = _person(session, COMPANY, "unprivileged", ROLE_OBLIGATION_OWNER)
        outcome = _invite(session, actor_user=plain)
        assert outcome.reason_code == INV_NOT_PERMITTED
        assert session.query(Invitation).count() == 0
        assert verify_chain(session, COMPANY)


def test_an_inviter_who_is_not_an_account_here_is_refused():
    init_db()
    with session_scope() as session:
        _gap(session)
        outcome = invite_owner_for_escalation(
            session,
            COMPANY,
            escalation_id="ESC-1",
            email=OWNER_EMAIL,
            display_name=OWNER_NAME,
            invited_by_user_id="usr-nobody",
            actor=ACTOR,
            now=T0,
        )
        assert outcome.reason_code == INV_NO_INVITER


def test_an_escalation_this_company_does_not_have_is_refused():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(session, escalation="ESC-NOPE", actor_user=analyst)
        assert outcome.reason_code == INV_NO_SUBJECT


def test_an_item_whose_refusal_is_not_the_owner_gap_carries_the_routing_reason():
    """Inviting is the fix for ONE routing refusal. It is not a fix for the rest.

    A change mapped to no obligation needs a mapping, not a person; saying so
    with the routing code attached is what lets a screen offer the right button.
    """
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        analyst = _may_invite(
            session, COMPANY, _person(session, COMPANY, "dana", ROLE_ANALYST)
        )
        change = _change(session, COMPANY, "CHG-2")
        escalation_id = _escalation(session, COMPANY, "ESC-2", change)
        outcome = _invite(session, escalation=escalation_id, actor_user=analyst)
        assert outcome.reason_code == INV_NOT_A_GAP
        assert outcome.route_reason_code is not None
        assert session.query(Invitation).count() == 0


def test_two_unowned_obligations_refuse_rather_than_picking_one():
    """Which duty would the invited person own? Nothing here can say."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-008",
            title="Keep engineering study files for six years.",
            owner_user_id=None,
            actor=ACTOR,
        )
        map_change_to_obligation(
            session,
            COMPANY,
            change_id="CHG-1",
            obligation_id="OBL-008",
            mapped_by=ACTOR,
            mapped_at=T0,
        )
        outcome = _invite(session, actor_user=analyst)
        assert outcome.reason_code == INV_OBLIGATIONS_AMBIGUOUS
        assert session.query(Invitation).count() == 0
        assert session.get(Escalation, escalation_id).assigned_to_user_id is None


def test_an_address_that_already_has_an_account_is_not_invited_again():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        existing = _person(session, COMPANY, "priya", ROLE_OBLIGATION_OWNER)
        outcome = _invite(session, email=existing.email, actor_user=analyst)
        assert outcome.reason_code == INV_ALREADY_A_USER
        assert outcome.invited_user_id == existing.id
        assert session.query(Invitation).count() == 0


def test_a_live_invitation_blocks_a_second_one_for_the_same_address():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        first = _invite(session, actor_user=analyst)
        assert first.reason_code == INV_OK
        second = _invite(session, actor_user=analyst, now=T0 + timedelta(hours=1))
        assert second.reason_code == INV_ALREADY_LIVE
        assert second.invitation_id == first.invitation_id
        assert session.query(Invitation).count() == 1


def test_a_queued_invitation_also_blocks_a_second_one():
    """An invitation waiting for an admin is out, even though nobody can accept
    it yet. Two rows for one person is two decisions for an admin to make about
    the same thing, and whichever they refuse the other still stands."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        first = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        assert first.reason_code == INV_QUEUED
        second = _invite(
            session,
            email="priya@lawfirm.example",
            actor_user=analyst,
            now=T0 + timedelta(hours=1),
        )
        assert second.reason_code == INV_ALREADY_LIVE
        assert second.invitation_id == first.invitation_id
        assert len(invitations_awaiting_approval(session, COMPANY)) == 1


def test_a_revoked_invitation_refuses_a_second_one():
    """Somebody decided this person should not be pulled in. Re-inviting would
    undo that decision quietly. The way back is an admin creating the account."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        first = _invite(session, actor_user=analyst)
        revoke_invitation(
            session,
            COMPANY,
            invitation_id=first.invitation_id,
            revoked_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0 + timedelta(days=1),
        )
        again = _invite(session, actor_user=analyst, now=T0 + timedelta(days=2))
        assert again.reason_code == INV_WAS_REVOKED
        assert again.invitation_id == first.invitation_id
        assert session.query(Invitation).count() == 1


# ---------------------------------------------------------------------------
# THE SAME-DOMAIN PATH: the item reaches a person, and says it has not landed
# ---------------------------------------------------------------------------


def test_a_same_domain_invite_routes_the_item_and_marks_it_pending():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)

        assert outcome.reason_code == INV_OK
        assert outcome.status == INVITE_PENDING
        assert outcome.needs_approval is False
        assert outcome.routed_escalation_ids == (escalation_id,)

        invitation = session.get(Invitation, outcome.invitation_id)
        assert invitation.company_id == COMPANY
        assert invitation.email == OWNER_EMAIL
        assert invitation.subject_type == INVITE_SUBJECT_ESCALATION
        assert invitation.subject_id == escalation_id
        assert invitation.invited_by_user_id == analyst.id
        assert invitation.approved_by_user_id is None
        assert invitation.accepted_at is None
        assert invitation.expires_at == T0 + timedelta(days=7)

        invited = session.get(Invitation, outcome.invitation_id).invited_user_id
        assert invited is not None
        person = user_for_company(session, COMPANY, invited)
        assert person.status == STATUS_INVITED
        assert person.display_name == OWNER_NAME
        # The duty now has a name on it, and the item is on that desk.
        assert obligation_for_company(session, COMPANY, "OBL-001").owner_user_id == invited
        assert session.get(Escalation, escalation_id).assigned_to_user_id == invited
        assert verify_chain(session, COMPANY)


def test_the_pending_state_is_data_rather_than_a_sentence_to_parse():
    """"Waiting on Priya" and "waiting on Priya, invited 2 days ago, not yet
    accepted" are different facts. A screen must not have to read prose to tell."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)

        resolution = resolve_escalation_owner(
            session, COMPANY, escalation_id=escalation_id
        )
        assert resolution.reason_code == ROUTE_PENDING_ACCEPTANCE
        assert resolution.routed is True
        assert resolution.pending_acceptance is True
        assert resolution.user_id == outcome.invited_user_id
        assert resolution.invitation_id == outcome.invitation_id
        assert resolution.invited_at == T0
        # And the sentence for a human is there too, saying both halves.
        assert "not yet accepted" in resolution.reason_text


def test_an_item_waiting_on_an_invitation_leaves_the_shared_queue_for_its_own():
    """It is not unrouted -- somebody's name is on it -- and it is not moving."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        _invite(session, actor_user=analyst)

        assert shared_queue(session, COMPANY) == []
        waiting = awaiting_acceptance(session, COMPANY)
        assert [item.escalation.id for item in waiting] == [escalation_id]
        assert waiting[0].resolution.pending_acceptance is True
        assert waiting[0].resolution.invited_at == T0


def test_routing_to_somebody_who_has_not_accepted_gets_its_own_action_code():
    """Three outcomes, three codes: routed, routed-pending, unrouted. "What did
    we route to somebody who never accepted" must be a filter, not a grep."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        _invite(session, actor_user=analyst)
        actions = [
            row.action
            for row in session.query(AuditEvent)
            .filter_by(company_id=COMPANY, subject_id=escalation_id)
            .all()
        ]
        assert ACTION_ESCALATION_ROUTED_PENDING in actions
        assert verify_chain(session, COMPANY)


def test_an_invited_account_cannot_sign_in_before_accepting():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)
        assert permissions_for_user(session, COMPANY, outcome.invited_user_id) == frozenset()
        with pytest.raises(AuthFailed):
            login(session, COMPANY, email=OWNER_EMAIL, password=CHOSEN)


def test_the_token_is_returned_once_and_never_stored():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)
        assert outcome.token
        assert len(outcome.token) >= 32
        stored = session.get(Invitation, outcome.invitation_id)
        assert stored.token_hash != outcome.token
        assert outcome.token not in stored.token_hash
        # And no audit row carries it either.
        rows = session.query(AuditEvent).all()
        assert all(outcome.token not in (row.reason or "") for row in rows)


# ---------------------------------------------------------------------------
# THE CROSS-DOMAIN PATH: visible, never silent, and the item does not move
# ---------------------------------------------------------------------------


def test_a_cross_domain_invite_is_queued_and_writes_no_account():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, email="priya@lawfirm.example", actor_user=analyst)

        assert outcome.reason_code == INV_QUEUED
        assert outcome.status == INVITE_AWAITING_APPROVAL
        assert outcome.needs_approval is True
        assert outcome.invited_user_id is None
        assert outcome.routed_escalation_ids == ()
        assert user_by_email(session, COMPANY, "priya@lawfirm.example") is None

        # The item is exactly where it was, with the reason it is still there.
        item = shared_queue(session, COMPANY)
        assert [row.escalation.id for row in item] == [escalation_id]
        assert item[0].resolution.reason_code == ROUTE_OBLIGATION_UNOWNED
        assert [row.id for row in invitations_awaiting_approval(session, COMPANY)] == [
            outcome.invitation_id
        ]
        assert verify_chain(session, COMPANY)


def test_a_subdomain_is_not_the_same_domain():
    """contractor.mep.example can be delegated to somebody who is not MEP. A
    label to the left of the company's domain is not proof of the company."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(
            session, email="priya@contractor.mep.example", actor_user=analyst
        )
        assert outcome.reason_code == INV_QUEUED


def test_the_domain_check_ignores_case():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        outcome = _invite(session, email="Priya.Nandakumar@MEP.Example", actor_user=analyst)
        assert outcome.reason_code == INV_OK
        assert session.get(Invitation, outcome.invitation_id).email == OWNER_EMAIL


def test_an_undecidable_company_domain_sends_every_invite_to_approval():
    """Fail closed. When the derivation cannot say, nobody takes the fast path."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        _person(session, COMPANY, "contractor", ROLE_ANALYST, domain="elsewhere.example")
        outcome = _invite(session, actor_user=analyst)
        assert outcome.reason_code == INV_QUEUED
        assert "domain" in outcome.reason_text.lower()


def test_the_tenant_switch_forces_even_a_same_domain_invite_through_approval(monkeypatch):
    init_db()
    monkeypatch.setenv(ENV_INVITES_NEED_APPROVAL, "yes")
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        outcome = _invite(session, actor_user=analyst)
        assert outcome.reason_code == INV_QUEUED
        assert outcome.invited_user_id is None
        assert session.get(Escalation, escalation_id).assigned_to_user_id is None


def test_inviting_your_own_address_with_a_tag_goes_to_approval():
    """The escalation path a fast lane would open: an analyst who cannot approve
    invites analyst+alt@, accepts it, and now holds obligation_owner. The tag
    test is a heuristic, so it queues for a person rather than refusing."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        local, _at, domain = analyst.email.partition("@")
        outcome = _invite(
            session, email=f"{local}+owner@{domain}", actor_user=analyst
        )
        assert outcome.reason_code == INV_QUEUED
        assert outcome.self_invite is True
        assert session.get(Invitation, outcome.invitation_id).status == (
            INVITE_AWAITING_APPROVAL
        )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def test_approval_admits_the_person_and_routes_the_item():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        admin = _person(session, COMPANY, "sarah", ROLE_ADMIN)

        outcome = approve_invitation(
            session,
            COMPANY,
            invitation_id=queued.invitation_id,
            approver_user_id=admin.id,
            actor=f"person:{admin.email}",
            now=T0 + timedelta(hours=2),
        )
        assert outcome.reason_code == INV_OK
        assert outcome.status == INVITE_APPROVED

        stored = session.get(Invitation, queued.invitation_id)
        assert stored.approved_by_user_id == admin.id
        assert stored.invited_user_id is not None
        assert session.get(Escalation, escalation_id).assigned_to_user_id == (
            stored.invited_user_id
        )
        resolution = resolve_escalation_owner(session, COMPANY, escalation_id=escalation_id)
        assert resolution.pending_acceptance is True
        assert verify_chain(session, COMPANY)


def test_only_a_holder_of_user_manage_may_approve():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        owner = _person(session, COMPANY, "tom", ROLE_OBLIGATION_OWNER)

        outcome = approve_invitation(
            session,
            COMPANY,
            invitation_id=queued.invitation_id,
            approver_user_id=owner.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=2),
        )
        assert outcome.reason_code == INV_NOT_PERMITTED
        assert session.get(Invitation, queued.invitation_id).status == (
            INVITE_AWAITING_APPROVAL
        )
        assert session.get(Escalation, escalation_id).assigned_to_user_id is None


def test_approving_something_that_was_never_queued_is_refused():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        direct = _invite(session, actor_user=analyst)
        admin = _person(session, COMPANY, "sarah", ROLE_ADMIN)
        outcome = approve_invitation(
            session,
            COMPANY,
            invitation_id=direct.invitation_id,
            approver_user_id=admin.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=1),
        )
        assert outcome.reason_code == INV_NOT_QUEUED


def test_an_expired_invitation_cannot_be_approved_into_life():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        admin = _person(session, COMPANY, "sarah", ROLE_ADMIN)
        outcome = approve_invitation(
            session,
            COMPANY,
            invitation_id=queued.invitation_id,
            approver_user_id=admin.id,
            actor=ACTOR,
            now=T0 + timedelta(days=30),
        )
        assert outcome.reason_code == INV_EXPIRED
        assert session.query(Invitation).filter_by(status=INVITE_APPROVED).count() == 0


def test_another_tenants_invitation_is_not_visible_to_this_one():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        ensure_system_roles(session)
        rival_admin = _person(session, RIVAL, "mallory", ROLE_ADMIN, domain="rival.example")
        outcome = approve_invitation(
            session,
            RIVAL,
            invitation_id=queued.invitation_id,
            approver_user_id=rival_admin.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=1),
        )
        assert outcome.reason_code == INV_UNKNOWN
        assert invitations_for_company(session, RIVAL) == []


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_acceptance_activates_the_account_and_lands_them_on_the_item():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)

        accepted = accept_invitation(
            session,
            token=invited.token,
            password=CHOSEN,
            now=T0 + timedelta(days=2),
        )
        assert accepted.reason_code == ACCEPT_OK
        assert accepted.company_id == COMPANY
        assert accepted.subject_type == INVITE_SUBJECT_ESCALATION
        assert accepted.subject_id == escalation_id

        person = user_for_company(session, COMPANY, accepted.user_id)
        assert person.status == STATUS_ACTIVE
        assert session.get(Invitation, invited.invitation_id).status == INVITE_ACCEPTED
        assert session.get(Invitation, invited.invitation_id).accepted_at == (
            T0 + timedelta(days=2)
        )
        # The item was already theirs, and now it is theirs and moving.
        assert [row.id for row in escalations_for_user(session, COMPANY, person.id)] == [
            escalation_id
        ]
        assert resolve_escalation_owner(
            session, COMPANY, escalation_id=escalation_id
        ).reason_code == ROUTE_OK
        assert awaiting_acceptance(session, COMPANY) == []
        assert verify_chain(session, COMPANY)


def test_the_password_goes_through_the_existing_scrypt_path():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        accept_invitation(session, token=invited.token, password=CHOSEN, now=T0)
        live, token = login(session, COMPANY, email=OWNER_EMAIL, password=CHOSEN)
        assert token
        assert live.user_id == invited.invited_user_id


def test_acceptance_grants_obligation_owner_and_nothing_else():
    """The reach an invited person gets is fixed in code, not chosen per row."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        accepted = accept_invitation(
            session, token=invited.token, password=CHOSEN, now=T0
        )
        held = permissions_for_user(session, COMPANY, accepted.user_id)
        assert "action.approve" in held
        # Not the admin's, not the analyst's, and not the invite power itself.
        assert "user.manage" not in held
        assert "user.invite" not in held
        assert "action.propose" not in held
        assert "escalation.resolve" not in held
        assert accepted.granted_roles == (ROLE_OBLIGATION_OWNER,)


def test_an_inviter_cannot_hand_over_more_than_the_one_narrow_role():
    """An admin invites. The invited person is still only an obligation owner."""
    init_db()
    with session_scope() as session:
        _gap(session)
        admin = _person(session, COMPANY, "sarah", ROLE_ADMIN)
        invited = _invite(session, actor_user=admin)
        accepted = accept_invitation(
            session, token=invited.token, password=CHOSEN, now=T0
        )
        held = permissions_for_user(session, COMPANY, accepted.user_id)
        assert "user.manage" not in held
        assert "threshold.set" not in held
        assert "workflow.manage" not in held
        assert accepted.granted_roles == (ROLE_OBLIGATION_OWNER,)


def test_an_accepted_invitation_reaches_no_other_tenant():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        _gap(session, RIVAL)
        invited = _invite(session, actor_user=analyst)
        accepted = accept_invitation(
            session, token=invited.token, password=CHOSEN, now=T0
        )
        assert user_for_company(session, RIVAL, accepted.user_id) is None
        assert permissions_for_user(session, RIVAL, accepted.user_id) == frozenset()
        assert escalations_for_user(session, RIVAL, accepted.user_id) == []


def test_an_unknown_token_a_revoked_one_and_an_expired_one_answer_alike():
    """A refusal that tells the holder which of the three it was is an oracle."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        revoked = _invite(session, actor_user=analyst)
        revoke_invitation(
            session,
            COMPANY,
            invitation_id=revoked.invitation_id,
            revoked_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=1),
        )

        unknown = accept_invitation(
            session, token="not-a-token-anybody-issued", password=CHOSEN, now=T0
        )
        dead = accept_invitation(
            session, token=revoked.token, password=CHOSEN, now=T0 + timedelta(hours=2)
        )
        assert unknown.reason_code == ACCEPT_REFUSED
        assert dead.reason_code == ACCEPT_REFUSED
        assert unknown.reason_text == dead.reason_text
        assert dead.user_id is None
        assert dead.company_id is None


def test_an_expired_invitation_refuses_acceptance():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        outcome = accept_invitation(
            session,
            token=invited.token,
            password=CHOSEN,
            now=T0 + timedelta(days=8),
        )
        assert outcome.reason_code == ACCEPT_REFUSED
        assert user_for_company(
            session, COMPANY, invited.invited_user_id
        ).status == STATUS_INVITED


def test_a_second_acceptance_is_refused():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        accept_invitation(session, token=invited.token, password=CHOSEN, now=T0)
        again = accept_invitation(
            session, token=invited.token, password="another-password-entirely", now=T0
        )
        assert again.reason_code == ACCEPT_REFUSED
        # And the first password still works, so a replay cannot reset it.
        assert login(session, COMPANY, email=OWNER_EMAIL, password=CHOSEN)


def test_a_queued_invitation_cannot_be_accepted_before_it_is_approved():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        outcome = accept_invitation(
            session, token=queued.token, password=CHOSEN, now=T0
        )
        assert outcome.reason_code == ACCEPT_REFUSED
        assert user_by_email(session, COMPANY, "priya@lawfirm.example") is None


def test_a_password_below_the_floor_is_refused_and_named_as_such():
    """Distinct from ACCEPT_REFUSED on purpose: the invitation is fine and the
    person has to be told what to fix."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        outcome = accept_invitation(
            session, token=invited.token, password="short", now=T0
        )
        assert outcome.reason_code == ACCEPT_PASSWORD
        assert user_for_company(
            session, COMPANY, invited.invited_user_id
        ).status == STATUS_INVITED
        assert session.get(Invitation, invited.invitation_id).status == INVITE_PENDING


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revoking_suspends_the_account_the_invitation_made_and_frees_the_item():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)

        outcome = revoke_invitation(
            session,
            COMPANY,
            invitation_id=invited.invitation_id,
            revoked_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0 + timedelta(days=1),
        )
        assert outcome.reason_code == INV_REVOKED_OK
        assert session.get(Invitation, invited.invitation_id).status == INVITE_REVOKED
        assert user_for_company(
            session, COMPANY, invited.invited_user_id
        ).status == STATUS_SUSPENDED

        # The work goes back to the shared queue with the reason it is there.
        row = session.get(Escalation, escalation_id)
        assert row.assigned_to_user_id is None
        assert row.assigned_at is None
        assert obligation_for_company(session, COMPANY, "OBL-001").owner_user_id is None
        queue = shared_queue(session, COMPANY)
        assert [item.escalation.id for item in queue] == [escalation_id]
        assert queue[0].resolution.reason_code == ROUTE_OBLIGATION_UNOWNED
        assert verify_chain(session, COMPANY)


def test_revoking_never_touches_an_account_that_predates_the_invitation():
    """invited_user_id records which account THIS invitation created. An
    invitation that made none must not suspend somebody who was already here."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        queued = _invite(session, email="priya@lawfirm.example", actor_user=analyst)
        bystander = _person(session, COMPANY, "priya", ROLE_OBLIGATION_OWNER)

        revoke_invitation(
            session,
            COMPANY,
            invitation_id=queued.invitation_id,
            revoked_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0 + timedelta(days=1),
        )
        assert user_for_company(session, COMPANY, bystander.id).status == STATUS_ACTIVE


def test_only_the_person_who_invited_or_an_admin_may_withdraw():
    """Same rule as a share link: the sender or an admin, and nobody else.

    Without it, any account in the tenant could take an item off a colleague's
    desk and suspend the person on it, and the only trace would say the
    invitation was withdrawn rather than by whom it should not have been.
    """
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        stranger = _person(session, COMPANY, "mallory", ROLE_OBLIGATION_OWNER)

        outcome = revoke_invitation(
            session,
            COMPANY,
            invitation_id=invited.invitation_id,
            revoked_by_user_id=stranger.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=1),
        )
        assert outcome.reason_code == INV_NOT_PERMITTED
        assert session.get(Invitation, invited.invitation_id).status == INVITE_PENDING
        assert session.get(Escalation, escalation_id).assigned_to_user_id is not None
        assert user_for_company(
            session, COMPANY, invited.invited_user_id
        ).status == STATUS_INVITED


def test_an_admin_who_did_not_send_it_may_still_withdraw_it():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        admin = _person(session, COMPANY, "sarah", ROLE_ADMIN)
        outcome = revoke_invitation(
            session,
            COMPANY,
            invitation_id=invited.invitation_id,
            revoked_by_user_id=admin.id,
            actor=ACTOR,
            now=T0 + timedelta(hours=1),
        )
        assert outcome.reason_code == INV_REVOKED_OK


def test_an_accepted_invitation_cannot_be_revoked():
    """Taking somebody's account away is a suspension, and it is a different
    decision with a different audit code."""
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        accept_invitation(session, token=invited.token, password=CHOSEN, now=T0)
        outcome = revoke_invitation(
            session,
            COMPANY,
            invitation_id=invited.invitation_id,
            revoked_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0 + timedelta(days=1),
        )
        assert outcome.reason_code == INV_ALREADY_ACCEPTED
        assert user_for_company(
            session, COMPANY, invited.invited_user_id
        ).status == STATUS_ACTIVE


# ---------------------------------------------------------------------------
# What routing must keep refusing, and the one refusal it stops making
# ---------------------------------------------------------------------------


def test_an_invited_owner_with_no_live_invitation_is_still_a_refusal():
    """An account at STATUS_INVITED whose invitation died cannot act, and the
    item must go back to the queue rather than sit on a stalled desk."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        session.get(Invitation, invited.invitation_id).status = INVITE_REVOKED
        session.flush()

        resolution = resolve_escalation_owner(
            session, COMPANY, escalation_id=escalation_id
        )
        assert resolution.reason_code == ROUTE_OWNER_INACTIVE
        assert resolution.routed is False
        assert resolution.pending_acceptance is False


def test_an_expired_invitation_stops_the_item_reading_as_pending():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        _invite(session, actor_user=analyst)
        resolution = resolve_escalation_owner(
            session,
            COMPANY,
            escalation_id=escalation_id,
            now=T0 + timedelta(days=9),
        )
        assert resolution.reason_code == ROUTE_OWNER_INACTIVE


def test_re_routing_an_item_already_held_by_an_invitee_still_says_pending():
    """The early return in route_escalation must not report a pending handoff
    as held. It is the path a re-run of the seed takes."""
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        _invite(session, actor_user=analyst)
        resolution = route_escalation(
            session,
            COMPANY,
            escalation_id=escalation_id,
            actor=ACTOR,
            now=T0 + timedelta(days=2),
        )
        assert resolution.reason_code == ROUTE_PENDING_ACCEPTANCE
        assert resolution.pending_acceptance is True


def test_a_suspended_owner_is_still_refused_rather_than_read_as_pending():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        accept_invitation(session, token=invited.token, password=CHOSEN, now=T0)
        person = user_for_company(session, COMPANY, invited.invited_user_id)
        person.status = STATUS_SUSPENDED
        session.flush()
        resolution = resolve_escalation_owner(
            session, COMPANY, escalation_id=escalation_id
        )
        assert resolution.reason_code == ROUTE_OWNER_INACTIVE


# ---------------------------------------------------------------------------
# Inviting from the obligation rather than from the escalation
# ---------------------------------------------------------------------------


def test_inviting_the_owner_of_a_duty_routes_everything_that_was_waiting_on_it():
    init_db()
    with session_scope() as session:
        analyst, escalation_id = _gap(session)
        second = _escalation(session, COMPANY, "ESC-2", "CHG-1")

        outcome = invite_owner_for_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            email=OWNER_EMAIL,
            display_name=OWNER_NAME,
            invited_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0,
        )
        assert outcome.reason_code == INV_OK
        assert session.get(Invitation, outcome.invitation_id).subject_type == (
            INVITE_SUBJECT_OBLIGATION
        )
        assert sorted(outcome.routed_escalation_ids) == sorted([escalation_id, second])
        assert shared_queue(session, COMPANY) == []
        assert verify_chain(session, COMPANY)


def test_a_duty_that_already_has_an_owner_is_not_a_gap():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        owner = _person(session, COMPANY, "tom", ROLE_OBLIGATION_OWNER)
        obligation = obligation_for_company(session, COMPANY, "OBL-001")
        obligation.owner_user_id = owner.id
        session.flush()

        outcome = invite_owner_for_obligation(
            session,
            COMPANY,
            obligation_id="OBL-001",
            email=OWNER_EMAIL,
            display_name=OWNER_NAME,
            invited_by_user_id=analyst.id,
            actor=ACTOR,
            now=T0,
        )
        assert outcome.reason_code == INV_NOT_A_GAP
        assert session.query(Invitation).count() == 0


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_every_reason_code_this_module_can_return_is_declared():
    assert INV_OK in INV_REASON_CODES
    assert INV_QUEUED in INV_REASON_CODES
    assert len(set(INV_REASON_CODES)) == len(INV_REASON_CODES)
    assert set(ACCEPT_REASON_CODES) == {ACCEPT_OK, ACCEPT_REFUSED, ACCEPT_PASSWORD}
    assert ROUTE_PENDING_ACCEPTANCE in ROUTING_REASON_CODES


def test_a_live_invitation_is_findable_by_address():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        invited = _invite(session, actor_user=analyst)
        found = live_invitation_for_email(
            session, COMPANY, email="PRIYA.NANDAKUMAR@mep.example", now=T0
        )
        assert found is not None and found.id == invited.invitation_id
        assert normalise_email(found.email) == OWNER_EMAIL
        assert live_invitation_for_email(
            session, RIVAL, email=OWNER_EMAIL, now=T0
        ) is None


def test_a_refusal_writes_no_invitation_and_leaves_the_chain_sound():
    init_db()
    with session_scope() as session:
        analyst, _escalation_id = _gap(session)
        before = event_count(session, COMPANY)
        for email in (None, "nonsense", "priya@lawfirm.example"):
            _invite(session, email=email, actor_user=analyst)
        assert verify_chain(session, COMPANY)
        assert event_count(session, COMPANY) > before
