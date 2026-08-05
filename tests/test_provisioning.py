"""An admin adds a login, sees when each one last signed in, and can resend.

WHAT WAS ASKED FOR. "Each admin user in a workspace should be able to provision
additional logins. They should be able to see most recent login for each
existing login as well. The newly provisioned login sends/resends on request an
invitation email that the recipient can use for 24 hours to set up a password
(after that they need a fresh invite)."

THREE THINGS IN THE CODE CONFLICTED WITH THAT, AND EVERY TEST HERE IS ONE OF
THEM.

1. Invitation.subject_type and subject_id were both NOT NULL, and the docstring
   said why: no invitation may be a bare account creation, because every one of
   them has to name the work that justified it. Admin provisioning IS bare
   account creation. The control is kept and narrowed rather than deleted: a
   KIND discriminator says which of the two an invitation is, a handoff must
   still name its item exactly as before, and a provision invitation names an
   admin who held user.manage instead. Nothing is unjustified; the justification
   is now either an item or a named person answering for it.

2. The handoff clock is seven days and may be thirty. The requirement is
   twenty-four hours. The handoff default is NOT changed -- a handoff sits until
   somebody notices the item, and shortening it would break routing -- so
   provisioning gets a separate, shorter clock, in hours, that no caller can
   lengthen.

3. "After that they need a fresh invite" is a security requirement, not wording.
   AN EXPIRED INVITATION IS NEVER EXTENDED. A resend mints a new token and moves
   the previous row to a terminal state, so at most one token is ever valid for
   a person. Extending expires_at on the existing row is how a link that leaked
   into an inbox, a helpdesk ticket or a mail archive stays live for ever.
   test_the_old_token_dies_the_instant_a_new_one_is_issued is that requirement.

AND ONE THING NOBODY ASKED FOR THAT THE PRODUCT HAS TO REFUSE ANYWAY. An
inviter may never grant more than they hold. Without it "provision a login" is
the shortest path in the product from user.manage to any authority at all.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth.sessions import AuthFailed, login
from app.state.audit import ACTION_ACCESS_DENIED, event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    MIN_PASSWORD_LENGTH,
    create_user,
    ensure_system_roles,
    grant_role,
    hash_session_token,
    permissions_for_user,
    user_by_email,
    user_for_company,
)
from app.state.invites import (
    ACCEPT_OK,
    ACCEPT_PASSWORD,
    ACCEPT_REFUSED,
    ACCEPT_UNAVAILABLE,
    ACTION_INVITE_CREATED,
    ACTION_INVITE_SUPERSEDED,
    INV_ALREADY_A_USER,
    INV_ALREADY_ACCEPTED,
    INV_DISABLED,
    INV_EMAIL_MALFORMED,
    INV_GRANT_EXCEEDS,
    INV_NO_EMAIL,
    INV_NO_INVITER,
    INV_NO_NAME,
    INV_NO_ROLE,
    INV_NOT_PERMITTED,
    INV_NOT_PROVISION,
    INV_OK,
    INV_REASON_CODES,
    INV_UNKNOWN,
    INV_WAS_REVOKED,
    ENV_INVITES_ENABLED,
    accept,
    accounts_for_company,
    grantable_roles,
    provision_login,
    require_justification,
    resend,
    revoke_invitation,
)
from app.state.models import (
    INVITE_ACCEPTED,
    INVITE_KIND_HANDOFF,
    INVITE_KIND_PROVISION,
    INVITE_KINDS,
    INVITE_PENDING,
    INVITE_PROVISION_TTL_HOURS,
    INVITE_REVOKED,
    INVITE_SUBJECT_ESCALATION,
    INVITE_SUPERSEDED,
    INVITATION_STATUSES,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    AuditEvent,
    Invitation,
    Role,
    RolePermission,
    PERMISSION_CODES,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
DOMAIN = "mep.example"
ACTOR = "system:test"
T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
PASSWORD = "strata-test-password"
CHOSEN = "the-password-sam-chose"

NEW_EMAIL = "sam.okafor@mep.example"
NEW_NAME = "Sam Okafor"

# A role holding every code the product defines. It exists so the ceiling test
# has a top: somebody who holds everything may grant anything, and the admin --
# who deliberately holds no approval permission -- may not.
PRINCIPAL = "principal"


# ---------------------------------------------------------------------------
# A small world. Rows written directly where the row's own module is not what
# is under test, the way tests/test_invites.py and tests/test_routing.py do it.
# ---------------------------------------------------------------------------


def _person(session, company_id, name, role, *, domain=None):
    at = domain or (DOMAIN if company_id == COMPANY else f"{company_id.lower()}.example")
    user = create_user(
        session,
        company_id,
        email=f"{name}@{at}",
        display_name=name.title(),
        password=PASSWORD,
        actor=ACTOR,
        user_id=f"usr-{company_id.lower()}-{name}",
        created_at=T0,
    )
    grant_role(
        session, company_id, user_id=user.id, role_name=role, actor=ACTOR, granted_at=T0
    )
    return user


def _principal_role(session, company_id):
    """A tenant role carrying every permission code there is."""
    role_id = f"role-{company_id.lower()}-{PRINCIPAL}"
    if session.get(Role, role_id) is None:
        session.add(Role(id=role_id, company_id=company_id, name=PRINCIPAL))
        for code in PERMISSION_CODES:
            session.add(RolePermission(role_id=role_id, permission_id=code))
        session.flush()
    return role_id


def _world(session, company_id=COMPANY):
    """One admin, one analyst, and one account that holds everything."""
    ensure_system_roles(session)
    _principal_role(session, company_id)
    admin = _person(session, company_id, "ada", ROLE_ADMIN)
    analyst = _person(session, company_id, "dana", ROLE_ANALYST)
    root = _person(session, company_id, "root", ROLE_ADMIN)
    grant_role(
        session,
        company_id,
        user_id=root.id,
        role_name=PRINCIPAL,
        actor=ACTOR,
        granted_at=T0,
    )
    return admin, analyst, root


def _provision(session, actor, *, company_id=COMPANY, email=NEW_EMAIL, role=ROLE_ADMIN,
               name=NEW_NAME, now=T0):
    return provision_login(
        session,
        company_id=company_id,
        actor=actor.id,
        email=email,
        display_name=name,
        role=role,
        now=now,
    )


# ---------------------------------------------------------------------------
# 1. The control bends, and does not break: the kind discriminator
# ---------------------------------------------------------------------------


def test_the_kind_discriminator_names_both_reasons_an_invitation_exists():
    """Two kinds, and no third. A row that is neither cannot be written."""
    assert INVITE_KINDS == (INVITE_KIND_HANDOFF, INVITE_KIND_PROVISION)
    assert INVITE_KIND_HANDOFF in INVITE_KINDS
    assert INVITE_KIND_PROVISION in INVITE_KINDS


def test_a_handoff_with_no_item_is_refused_by_the_write_layer():
    """The control the NOT NULL used to be, kept as a rule with a reason.

    A handoff invitation exists because an item is waiting. One with no item is
    a bare account creation wearing a handoff's name, and the admin reading the
    queue would see nothing to review.
    """
    with pytest.raises(ValueError) as error:
        require_justification(
            kind=INVITE_KIND_HANDOFF, subject_type=None, subject_id=None
        )
    assert INVITE_KIND_HANDOFF in str(error.value)

    with pytest.raises(ValueError):
        require_justification(
            kind=INVITE_KIND_HANDOFF,
            subject_type=INVITE_SUBJECT_ESCALATION,
            subject_id="",
        )
    # A provision invitation with no item is the case this whole task exists
    # for, and it passes.
    require_justification(
        kind=INVITE_KIND_PROVISION, subject_type=None, subject_id=None
    )
    # An unknown kind is refused rather than guessed at.
    with pytest.raises(ValueError):
        require_justification(kind="whatever", subject_type=None, subject_id=None)


def test_a_handoff_with_no_item_is_refused_by_the_database_too():
    """The write layer is the message; the constraint is the guarantee.

    A writer that never heard of require_justification still cannot leave a
    handoff without its item. Fix the class, not the line.
    """
    init_db()
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            admin, _analyst, _root = _world(session)
            session.add(
                Invitation(
                    id="INV-9999",
                    company_id=COMPANY,
                    email="nobody@mep.example",
                    invited_by_user_id=admin.id,
                    invited_at=T0,
                    token_hash="0" * 64,
                    expires_at=T0 + timedelta(days=1),
                    accepted_at=None,
                    status=INVITE_PENDING,
                    kind=INVITE_KIND_HANDOFF,
                    subject_type=None,
                    subject_id=None,
                    invited_user_id=None,
                )
            )
            session.flush()


def test_a_provision_invitation_names_no_item_and_names_the_admin_instead():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        assert outcome.reason_code == INV_OK
        row = outcome.invitation
        assert row.kind == INVITE_KIND_PROVISION
        assert row.subject_type is None
        assert row.subject_id is None
        # The justification is a person who held user.manage, and it is stored.
        assert row.invited_by_user_id == admin.id


def test_the_invitation_still_has_no_role_column():
    """The other half of the original control, untouched.

    A role on the row would make writing a row a way to pick one. The role a
    provisioned account gets is granted at provision time, by the admin, through
    the existing grant path, where the audit chain records who chose it.
    """
    assert not hasattr(Invitation, "role")
    assert not hasattr(Invitation, "role_name")


# ---------------------------------------------------------------------------
# 2. The second clock
# ---------------------------------------------------------------------------


def test_the_provisioning_clock_is_a_day_and_is_counted_in_hours():
    assert INVITE_PROVISION_TTL_HOURS == 24


def test_a_provisioned_invitation_runs_out_a_day_later():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        assert outcome.invitation.expires_at == T0 + timedelta(hours=24)


def test_the_handoff_clock_is_not_shortened_by_this():
    """A handoff may sit until somebody notices the item. Seven days stands."""
    from app.state.models import INVITE_DEFAULT_TTL_DAYS, INVITE_MAX_TTL_DAYS

    assert INVITE_DEFAULT_TTL_DAYS == 7
    assert INVITE_MAX_TTL_DAYS == 30


# ---------------------------------------------------------------------------
# Provisioning itself
# ---------------------------------------------------------------------------


def test_provisioning_creates_the_account_at_invited_and_returns_the_token_once():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)

        person = user_by_email(session, COMPANY, NEW_EMAIL)
        assert person is not None
        assert person.status == STATUS_INVITED
        assert person.display_name == NEW_NAME
        assert outcome.user_id == person.id

        assert outcome.token
        assert len(outcome.token) >= 32
        # Stored hashed, like LoginSession.token_hash. The raw token exists at
        # this one moment and nowhere else.
        assert outcome.invitation.token_hash == hash_session_token(outcome.token)
        assert outcome.token not in outcome.invitation.token_hash


def test_no_raw_token_ever_reaches_the_audit_chain():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        again = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=outcome.invitation.id,
            now=T0 + timedelta(hours=1),
        )
        rows = session.query(AuditEvent).all()
        for row in rows:
            assert outcome.token not in (row.reason or "")
            assert again.token not in (row.reason or "")


def test_the_provisioned_account_holds_its_role_and_no_authority_until_it_accepts():
    """The grant is made when the admin makes the decision, and is inert.

    permissions_for_user filters on STATUS_ACTIVE, so an invited account holds
    the role on paper and can do nothing with it. That is what lets the admin
    screen show what this login will be before anybody has set a password.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, root = _world(session)
        outcome = _provision(session, root, role=ROLE_ANALYST)
        assert outcome.reason_code == INV_OK
        assert permissions_for_user(session, COMPANY, outcome.user_id) == frozenset()

        rows = accounts_for_company(session, company_id=COMPANY)
        mine = [row for row in rows if row.email == NEW_EMAIL][0]
        assert mine.roles == (ROLE_ANALYST,)
        assert mine.status == STATUS_INVITED


def test_an_invited_account_cannot_sign_in_before_accepting():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        _provision(session, admin)
        with pytest.raises(AuthFailed):
            login(session, COMPANY, email=NEW_EMAIL, password=CHOSEN)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_accepting_sets_a_password_through_the_existing_path_and_activates():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)

        landed = accept(
            session,
            token=outcome.token,
            password=CHOSEN,
            now=T0 + timedelta(hours=2),
        )
        assert landed.reason_code == ACCEPT_OK
        assert landed.company_id == COMPANY
        assert landed.user_id == outcome.user_id
        # A provision invitation lands on nothing, and the absence stays absence.
        assert landed.subject_type is None
        assert landed.subject_id is None
        assert landed.granted_roles == (ROLE_ADMIN,)

        person = user_for_company(session, COMPANY, outcome.user_id)
        assert person.status == STATUS_ACTIVE
        assert session.get(Invitation, outcome.invitation.id).status == INVITE_ACCEPTED
        # The scrypt path is the existing one, so the existing login works.
        live, _token = login(
            session, COMPANY, email=NEW_EMAIL, password=CHOSEN,
            now=T0 + timedelta(hours=3),
        )
        assert live is not None


def test_accepting_twice_is_refused_and_says_nothing_extra():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        first = accept(session, token=outcome.token, password=CHOSEN, now=T0)
        assert first.reason_code == ACCEPT_OK
        second = accept(
            session, token=outcome.token, password="a-different-password", now=T0
        )
        assert second.reason_code == ACCEPT_REFUSED
        assert second.reason_text == ACCEPT_UNAVAILABLE
        assert second.company_id is None
        # And the password the second attempt offered did not take.
        login(session, COMPANY, email=NEW_EMAIL, password=CHOSEN)
        with pytest.raises(AuthFailed):
            login(session, COMPANY, email=NEW_EMAIL, password="a-different-password")


def test_a_password_below_the_minimum_gets_its_own_code_not_the_dead_one():
    """The invitation is fine and the person is entitled to be told what to fix.

    Answering "this invitation is not available" for a short password would send
    somebody back to their admin for a link that already works.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        short = "x" * (MIN_PASSWORD_LENGTH - 1)
        refused = accept(session, token=outcome.token, password=short, now=T0)
        assert refused.reason_code == ACCEPT_PASSWORD
        assert str(MIN_PASSWORD_LENGTH) in refused.reason_text
        # Still invited, and the link still works.
        person = user_for_company(session, COMPANY, outcome.user_id)
        assert person.status == STATUS_INVITED
        assert accept(
            session, token=outcome.token, password=CHOSEN, now=T0
        ).reason_code == ACCEPT_OK


# ---------------------------------------------------------------------------
# 3. A resend mints a new token and kills the old one
# ---------------------------------------------------------------------------


def test_the_old_token_dies_the_instant_a_new_one_is_issued():
    """THIS TEST IS THE REQUIREMENT.

    "After that they need a fresh invite." Extending expires_at on the standing
    row would leave the first link -- which is sitting in an inbox, a helpdesk
    ticket and a mail archive -- working for another day every time somebody
    presses resend. At most one token is valid for a person at a time.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        later = T0 + timedelta(hours=1)
        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=later,
        )
        assert second.reason_code == INV_OK
        assert second.token != first.token
        assert second.invitation.id != first.invitation.id
        assert second.superseded_invitation_id == first.invitation.id

        # The old row is terminal. Not expired -- nobody ran out of time -- and
        # not revoked -- nobody withdrew it. A fourth word for a fourth fact.
        assert session.get(Invitation, first.invitation.id).status == INVITE_SUPERSEDED
        # The old token is dead NOW, an hour before it would have run out.
        assert accept(
            session, token=first.token, password=CHOSEN, now=later
        ).reason_code == ACCEPT_REFUSED
        # The new one works.
        assert accept(
            session, token=second.token, password=CHOSEN, now=later
        ).reason_code == ACCEPT_OK


def test_a_resend_does_not_extend_anything_it_starts_the_clock_again():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        later = T0 + timedelta(hours=20)
        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=later,
        )
        assert second.invitation.expires_at == later + timedelta(hours=24)
        # The superseded row keeps the expiry it was written with. Rewriting it
        # would erase what the first link's life actually was.
        assert session.get(Invitation, first.invitation.id).expires_at == (
            T0 + timedelta(hours=24)
        )


def test_an_expired_invitation_is_replaced_rather_than_extended():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        too_late = T0 + timedelta(hours=25)
        assert accept(
            session, token=first.token, password=CHOSEN, now=too_late
        ).reason_code == ACCEPT_REFUSED

        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=too_late,
        )
        assert second.reason_code == INV_OK
        assert session.get(Invitation, first.invitation.id).status == INVITE_SUPERSEDED
        assert accept(
            session, token=second.token, password=CHOSEN, now=too_late
        ).reason_code == ACCEPT_OK


def test_a_resend_is_two_audit_events_so_the_record_shows_both():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        before = event_count(session, COMPANY)
        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=T0 + timedelta(hours=1),
        )
        assert event_count(session, COMPANY) == before + 2

        rows = {
            row.subject_id: row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.subject_type == "invitation")
            .all()
        }
        assert rows[first.invitation.id] == ACTION_INVITE_SUPERSEDED
        assert rows[second.invitation.id] == ACTION_INVITE_CREATED
        assert verify_chain(session, COMPANY)


def test_a_stale_resend_still_leaves_exactly_one_live_token():
    """Somebody presses a button on a screen drawn two resends ago.

    The invariant is one live token per ADDRESS, not per row the caller happens
    to name, so this is allowed and still leaves one. Refusing it would send an
    admin hunting for an invitation id they have no reason to care about.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        second = resend(
            session, company_id=COMPANY, actor=admin.id,
            invitation_id=first.invitation.id, now=T0 + timedelta(hours=1),
        )
        # The stale button: it names the row the FIRST resend already replaced.
        third = resend(
            session, company_id=COMPANY, actor=admin.id,
            invitation_id=first.invitation.id, now=T0 + timedelta(hours=2),
        )
        assert third.reason_code == INV_OK
        # It names what it actually killed, which was the second row.
        assert third.superseded_invitation_id == second.invitation.id

        moment = T0 + timedelta(hours=3)
        live = [
            row
            for row in session.query(Invitation)
            .filter(Invitation.company_id == COMPANY)
            .all()
            if row.status == INVITE_PENDING and row.expires_at > moment
        ]
        assert [row.id for row in live] == [third.invitation.id]
        for dead in (first, second):
            assert accept(
                session, token=dead.token, password=CHOSEN, now=moment
            ).reason_code == ACCEPT_REFUSED
        assert accept(
            session, token=third.token, password=CHOSEN, now=moment
        ).reason_code == ACCEPT_OK


def test_a_resend_never_mints_a_second_account():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=T0 + timedelta(hours=1),
        )
        assert second.user_id == first.user_id
        assert (
            session.query(Invitation)
            .filter(Invitation.company_id == COMPANY)
            .filter(Invitation.invited_user_id == first.user_id)
            .count()
            == 2
        )
        rows = [r for r in accounts_for_company(session, company_id=COMPANY)
                if r.email == NEW_EMAIL]
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# One answer for every dead token
# ---------------------------------------------------------------------------


def test_expired_revoked_superseded_and_unknown_all_give_one_answer():
    """A caller holding a guess must not learn that an invitation once existed.

    Five sentences would be a probe. The real reason goes in the audit chain,
    where the company is known and the reader is entitled to it.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)

        expired = _provision(session, admin, email="a@mep.example", name="A One")
        revoked = _provision(session, admin, email="b@mep.example", name="B Two")
        superseded = _provision(session, admin, email="c@mep.example", name="C Three")

        revoke_invitation(
            session,
            COMPANY,
            invitation_id=revoked.invitation.id,
            revoked_by_user_id=admin.id,
            actor=f"person:{admin.email}",
            now=T0,
        )
        resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=superseded.invitation.id,
            now=T0,
        )

        moment = T0 + timedelta(hours=25)
        answers = [
            accept(session, token=expired.token, password=CHOSEN, now=moment),
            accept(session, token=revoked.token, password=CHOSEN, now=moment),
            accept(session, token=superseded.token, password=CHOSEN, now=moment),
            accept(session, token="a-token-nobody-ever-issued", password=CHOSEN,
                   now=moment),
            accept(session, token="", password=CHOSEN, now=moment),
        ]
        for answer in answers:
            assert answer == answers[0], "a dead token must not be distinguishable"
            assert answer.reason_code == ACCEPT_REFUSED
            assert answer.reason_text == ACCEPT_UNAVAILABLE
            assert answer.company_id is None
            assert answer.user_id is None
            assert answer.invitation_id is None
            assert answer.granted_roles == ()


def test_the_chain_still_records_which_of_them_it_was():
    """The caller learns nothing; the admin reading the log learns everything."""
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=outcome.invitation.id,
            now=T0,
        )
        accept(session, token=outcome.token, password=CHOSEN, now=T0)
        reasons = " ".join(
            row.reason or ""
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.subject_id == outcome.invitation.id)
            .all()
        )
        assert INVITE_SUPERSEDED in reasons
        assert verify_chain(session, COMPANY)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_address_that_already_has_an_account_here_is_refused():
    init_db()
    with session_scope() as session:
        admin, analyst, _root = _world(session)
        outcome = _provision(session, admin, email=analyst.email, name="Dana Again")
        assert outcome.reason_code == INV_ALREADY_A_USER
        assert outcome.token is None
        assert outcome.invitation is None


def test_an_address_whose_account_is_suspended_is_still_refused():
    """Reinstating an account is a decision, not a second account."""
    init_db()
    with session_scope() as session:
        admin, analyst, _root = _world(session)
        analyst.status = STATUS_SUSPENDED
        session.flush()
        outcome = _provision(session, admin, email=analyst.email, name="Dana Again")
        assert outcome.reason_code == INV_ALREADY_A_USER


def test_a_malformed_address_is_refused_rather_than_repaired():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        assert _provision(
            session, admin, email="sam okafor at mep"
        ).reason_code == INV_EMAIL_MALFORMED
        assert _provision(session, admin, email="").reason_code == INV_NO_EMAIL
        assert _provision(
            session, admin, name="  "
        ).reason_code == INV_NO_NAME


def test_an_actor_without_user_manage_is_refused_and_the_refusal_is_recorded():
    init_db()
    with session_scope() as session:
        admin, analyst, _root = _world(session)
        outcome = _provision(session, analyst)
        assert outcome.reason_code == INV_NOT_PERMITTED
        assert outcome.invitation is None
        assert user_by_email(session, COMPANY, NEW_EMAIL) is None
        denials = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.action == ACTION_ACCESS_DENIED)
            .all()
        )
        assert denials, "a refusal nobody can see afterwards is a refusal nobody can dispute"
        assert verify_chain(session, COMPANY)


def test_an_actor_who_is_not_an_account_here_is_refused():
    init_db()
    with session_scope() as session:
        _world(session)
        outcome = provision_login(
            session,
            company_id=COMPANY,
            actor="usr-nobody",
            email=NEW_EMAIL,
            display_name=NEW_NAME,
            role=ROLE_ADMIN,
            now=T0,
        )
        assert outcome.reason_code == INV_NO_INVITER


def test_an_actor_from_another_tenant_cannot_provision_here():
    init_db()
    with session_scope() as session:
        _world(session)
        rival_admin, _a, _r = _world(session, RIVAL)
        outcome = provision_login(
            session,
            company_id=COMPANY,
            actor=rival_admin.id,
            email=NEW_EMAIL,
            display_name=NEW_NAME,
            role=ROLE_ADMIN,
            now=T0,
        )
        assert outcome.reason_code == INV_NO_INVITER
        assert user_by_email(session, COMPANY, NEW_EMAIL) is None


def test_a_role_the_company_cannot_grant_is_refused():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        assert _provision(
            session, admin, role="wizard"
        ).reason_code == INV_NO_ROLE
        assert _provision(session, admin, role="").reason_code == INV_NO_ROLE


def test_provisioning_is_refused_when_invites_are_switched_off(monkeypatch):
    """The switch answers in one place and applies to every way in."""
    init_db()
    monkeypatch.setenv(ENV_INVITES_ENABLED, "false")
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        assert outcome.reason_code == INV_DISABLED
        assert user_by_email(session, COMPANY, NEW_EMAIL) is None


def test_an_unscoped_provision_is_refused():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        with pytest.raises(ValueError):
            provision_login(
                session,
                company_id="",
                actor=admin.id,
                email=NEW_EMAIL,
                display_name=NEW_NAME,
                role=ROLE_ADMIN,
                now=T0,
            )


# ---------------------------------------------------------------------------
# An inviter may never grant more than they hold
# ---------------------------------------------------------------------------


def test_an_inviter_may_never_grant_more_than_they_hold():
    """The shortest path from user.manage to any authority at all, closed.

    The admin role deliberately holds no approval permission. Without this rule
    an admin provisions an obligation_owner login, accepts it themselves, and
    holds action.approve -- which is exactly the escalation the role grid is
    drawn to refuse. The refusal names the codes, so the operator can see what
    they would have to hold.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, root = _world(session)

        refused = _provision(session, admin, role=ROLE_OBLIGATION_OWNER)
        assert refused.reason_code == INV_GRANT_EXCEEDS
        assert "action.approve" in refused.reason_text
        assert user_by_email(session, COMPANY, NEW_EMAIL) is None

        # The analyst role is no better: the admin holds none of its codes.
        assert _provision(
            session, admin, role=ROLE_ANALYST
        ).reason_code == INV_GRANT_EXCEEDS

        # An account that holds everything may grant anything.
        allowed = _provision(session, root, role=ROLE_OBLIGATION_OWNER)
        assert allowed.reason_code == INV_OK


def test_an_admin_may_still_provision_another_admin():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        assert _provision(session, admin, role=ROLE_ADMIN).reason_code == INV_OK


def test_grantable_roles_is_the_ceiling_a_screen_should_offer():
    """So the screen offers what will work rather than a list that refuses."""
    init_db()
    with session_scope() as session:
        admin, analyst, root = _world(session)
        assert grantable_roles(session, company_id=COMPANY, actor=admin.id) == (
            ROLE_ADMIN,
        )
        assert ROLE_OBLIGATION_OWNER in grantable_roles(
            session, company_id=COMPANY, actor=root.id
        )
        # Somebody who cannot manage accounts may grant nothing.
        assert grantable_roles(session, company_id=COMPANY, actor=analyst.id) == ()


# ---------------------------------------------------------------------------
# Resend refusals
# ---------------------------------------------------------------------------


def test_resend_refuses_another_tenants_invitation_and_leaves_it_alive():
    """The same answer as an id that was never issued, and no collateral.

    A cross-tenant caller must not be able to kill somebody else's invitation,
    which is what a resend that reached across would do.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        rival_admin, _a, _r = _world(session, RIVAL)
        mine = _provision(session, admin)

        outcome = resend(
            session,
            company_id=RIVAL,
            actor=rival_admin.id,
            invitation_id=mine.invitation.id,
            now=T0,
        )
        assert outcome.reason_code == INV_UNKNOWN
        assert outcome.token is None
        assert session.get(Invitation, mine.invitation.id).status == INVITE_PENDING
        assert accept(
            session, token=mine.token, password=CHOSEN, now=T0
        ).reason_code == ACCEPT_OK


def test_a_token_reaches_only_the_tenant_that_minted_it():
    """accept() takes no company argument, so the row is what decides the scope.

    Two tenants, the same address, two accounts -- which is what the schema
    means by one row per person per company. Accepting the MEP token must
    activate the MEP account and touch nothing in RIVAL, and the RIVAL token
    must still be the RIVAL one afterwards.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        rival_admin, _a, _r = _world(session, RIVAL)
        here = _provision(session, admin)
        there = _provision(session, rival_admin, company_id=RIVAL)

        assert here.user_id != there.user_id
        landed = accept(session, token=here.token, password=CHOSEN, now=T0)
        assert landed.company_id == COMPANY
        assert landed.user_id == here.user_id

        assert user_for_company(session, COMPANY, here.user_id).status == STATUS_ACTIVE
        assert (
            user_for_company(session, RIVAL, there.user_id).status == STATUS_INVITED
        )
        # And the MEP account is not reachable as a RIVAL account.
        assert user_for_company(session, RIVAL, here.user_id) is None

        settled = accept(session, token=there.token, password=CHOSEN, now=T0)
        assert settled.company_id == RIVAL
        assert settled.user_id == there.user_id


def test_resend_refuses_an_id_that_was_never_issued():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        assert resend(
            session, company_id=COMPANY, actor=admin.id, invitation_id="INV-4242",
            now=T0,
        ).reason_code == INV_UNKNOWN


def test_resend_refuses_an_accepted_invitation():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        accept(session, token=outcome.token, password=CHOSEN, now=T0)
        again = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=outcome.invitation.id,
            now=T0,
        )
        assert again.reason_code == INV_ALREADY_ACCEPTED
        assert again.token is None


def test_resend_refuses_a_withdrawn_invitation():
    """Somebody stopped this. Quietly minting a new token would undo them."""
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        revoke_invitation(
            session,
            COMPANY,
            invitation_id=outcome.invitation.id,
            revoked_by_user_id=admin.id,
            actor=f"person:{admin.email}",
            now=T0,
        )
        again = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=outcome.invitation.id,
            now=T0,
        )
        assert again.reason_code == INV_WAS_REVOKED


def test_resend_is_refused_when_invites_are_switched_off(monkeypatch):
    """The switch has to reach the button that mints the second link as well.

    provision_login asks it and resend did not, so a tenant that turned invites
    off stopped new credential links and left the resend button minting them --
    on a row that already existed, which is exactly the case an operator turning
    the switch off is trying to stop. A resend is a NEW invitation with a NEW
    token, said at length in the function's own docstring, so a switch that lets
    it through is not the control its refusal claims to be.

    The invitation is provisioned BEFORE the switch goes off, because that is
    the state this is about: the row is already there and the link is already in
    somebody's inbox when the tenant decides nobody else gets in.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        assert first.reason_code == INV_OK

        monkeypatch.setenv(ENV_INVITES_ENABLED, "false")
        again = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=T0 + timedelta(hours=1),
        )

        assert again.reason_code == INV_DISABLED
        assert again.token is None, "a refused resend minted a token anyway"
        # The first link is untouched: refusing to mint a second one must not
        # kill the one already out, which would be a withdrawal nobody decided.
        row = session.get(Invitation, first.invitation.id)
        assert row.status == INVITE_PENDING
        assert verify_chain(session, COMPANY)


def test_resend_refuses_a_handoff_invitation_and_says_what_to_do_instead():
    """A handoff carries an item whose state has to be re-derived first.

    Minting a fresh token for one here would skip that check and could admit
    somebody onto a duty that gained an owner while the invitation sat.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        row = session.get(Invitation, outcome.invitation.id)
        row.kind = INVITE_KIND_HANDOFF
        row.subject_type = INVITE_SUBJECT_ESCALATION
        row.subject_id = "ESC-1"
        session.flush()

        again = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=outcome.invitation.id,
            now=T0,
        )
        assert again.reason_code == INV_NOT_PROVISION
        assert "invite_owner_for" in again.reason_text


def test_resend_needs_user_manage_too():
    init_db()
    with session_scope() as session:
        admin, analyst, _root = _world(session)
        outcome = _provision(session, admin)
        again = resend(
            session,
            company_id=COMPANY,
            actor=analyst.id,
            invitation_id=outcome.invitation.id,
            now=T0,
        )
        assert again.reason_code == INV_NOT_PERMITTED
        # And the standing invitation is untouched.
        assert session.get(Invitation, outcome.invitation.id).status == INVITE_PENDING


# ---------------------------------------------------------------------------
# The admin screen's read
# ---------------------------------------------------------------------------


def test_never_logged_in_stays_none_and_is_not_coerced():
    """User.last_login_at is nullable on purpose and the read must not fill it.

    "Never logged in" and "logged in a year ago" are different facts about an
    account. The screen renders the distinction; this read must not destroy it
    before the screen sees it.
    """
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        _provision(session, admin)
        rows = accounts_for_company(session, company_id=COMPANY)
        mine = [row for row in rows if row.email == NEW_EMAIL][0]
        assert mine.last_login_at is None
        assert not isinstance(mine.last_login_at, str)
        assert not isinstance(mine.last_login_at, datetime)


def test_the_read_shows_the_last_login_once_there_is_one():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        outcome = _provision(session, admin)
        accept(session, token=outcome.token, password=CHOSEN, now=T0)
        when = T0 + timedelta(days=3)
        login(session, COMPANY, email=NEW_EMAIL, password=CHOSEN, now=when)

        rows = accounts_for_company(session, company_id=COMPANY)
        mine = [row for row in rows if row.email == NEW_EMAIL][0]
        assert mine.last_login_at == when
        assert mine.status == STATUS_ACTIVE
        # The invitation is spent, so there is nothing outstanding.
        assert mine.invitation_live is False
        assert mine.invitation_status == INVITE_ACCEPTED


def test_the_read_shows_every_account_with_status_roles_and_any_outstanding_invite():
    init_db()
    with session_scope() as session:
        admin, analyst, root = _world(session)
        outcome = _provision(session, admin)

        rows = {row.email: row for row in accounts_for_company(session, company_id=COMPANY)}
        assert set(rows) == {admin.email, analyst.email, root.email, NEW_EMAIL}

        assert rows[admin.email].roles == (ROLE_ADMIN,)
        assert rows[analyst.email].roles == (ROLE_ANALYST,)
        assert rows[root.email].roles == (ROLE_ADMIN, PRINCIPAL)
        assert rows[admin.email].invitation_id is None
        assert rows[admin.email].invitation_status is None

        pending = rows[NEW_EMAIL]
        assert pending.invitation_id == outcome.invitation.id
        assert pending.invitation_status == INVITE_PENDING
        assert pending.invitation_kind == INVITE_KIND_PROVISION
        assert pending.invitation_expires_at == T0 + timedelta(hours=24)
        assert pending.invitation_live is True


def test_the_read_reports_an_invitation_that_ran_out_as_not_live():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        _provision(session, admin)
        rows = accounts_for_company(
            session, company_id=COMPANY, now=T0 + timedelta(hours=25)
        )
        mine = [row for row in rows if row.email == NEW_EMAIL][0]
        assert mine.invitation_live is False
        assert mine.invitation_status == INVITE_PENDING


def test_the_read_follows_the_newest_invitation_after_a_resend():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        first = _provision(session, admin)
        second = resend(
            session,
            company_id=COMPANY,
            actor=admin.id,
            invitation_id=first.invitation.id,
            now=T0 + timedelta(hours=1),
        )
        rows = accounts_for_company(
            session, company_id=COMPANY, now=T0 + timedelta(hours=2)
        )
        mine = [row for row in rows if row.email == NEW_EMAIL][0]
        assert mine.invitation_id == second.invitation.id
        assert mine.invitation_live is True


def test_the_read_never_crosses_a_tenant():
    init_db()
    with session_scope() as session:
        admin, _analyst, _root = _world(session)
        rival_admin, _a, _r = _world(session, RIVAL)
        _provision(session, admin)
        _provision(session, rival_admin, company_id=RIVAL, email="sam@rival.example")

        here = {row.email for row in accounts_for_company(session, company_id=COMPANY)}
        there = {row.email for row in accounts_for_company(session, company_id=RIVAL)}
        assert NEW_EMAIL in here
        assert NEW_EMAIL not in there
        assert "sam@rival.example" in there
        assert here & there == set()


def test_an_unscoped_read_is_refused():
    init_db()
    with session_scope() as session:
        _world(session)
        with pytest.raises(ValueError):
            accounts_for_company(session, company_id="")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_superseded_is_a_status_of_its_own():
    """Revoked, expired and superseded are three different answers to "why is
    this person not here". Collapsing them would make the log unreadable."""
    assert INVITE_SUPERSEDED in INVITATION_STATUSES
    assert INVITE_SUPERSEDED != INVITE_REVOKED
    assert len(set(INVITATION_STATUSES)) == len(INVITATION_STATUSES)


def test_the_new_reason_codes_are_in_the_published_list():
    for code in (INV_GRANT_EXCEEDS, INV_NO_ROLE, INV_NOT_PROVISION):
        assert code in INV_REASON_CODES
