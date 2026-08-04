"""Identity and RBAC: what a stored password gives away, and who may approve.

Five things this suite exists to catch, because each has a cheap wrong
implementation that reads fine:

1. A password stored in a form somebody can reverse or replay. The tests assert
   the salt is random per hash, that the stored value is not the password, and
   that a hash keeps verifying after the module's cost parameters move -- the
   failure that makes raising the cost impossible and so means nobody ever does.
2. A revoked role that leaves the permission behind, or takes the history with
   it. Both directions are checked: the code goes, the row stays.
3. Authorisation that fails open. An unknown user, another tenant's user and a
   suspended user must all come back with nothing.
4. The segregation of duties control quietly not existing. The analyst role is
   asserted to lack action.approve and action.reject, because that absence is
   the control docs/security.html rests the approval design on.
5. A second audit log. Creating a user, granting and revoking all append to the
   existing hash chain, and the chain still verifies afterwards.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone

import pytest

from app.state import identity
from app.state.audit import event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    MIN_PASSWORD_LENGTH,
    SYSTEM_ROLE_PERMISSIONS,
    create_user,
    ensure_system_roles,
    grant_role,
    hash_password,
    hash_session_token,
    new_session_token,
    normalise_email,
    permissions_for_role,
    permissions_for_user,
    revoke_role,
    role_for_company,
    role_grants_for_user,
    roles_for_company,
    session_token_matches,
    set_user_status,
    user_by_email,
    user_for_company,
    user_has_permission,
    users_for_company,
    verify_password,
)
from app.state.models import (
    PERMISSION_CODES,
    AuditEvent,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
PASSWORD = "correct-horse-battery-staple"
ADMIN = "admin@mep.example"


def _fresh(session):
    """A clean database with the vocabulary and the three system roles in it."""
    ensure_system_roles(session)


def _analyst(session, email: str = "analyst@mep.example", company: str = COMPANY):
    return create_user(
        session,
        company,
        email=email,
        display_name="Dana Okafor",
        password=PASSWORD,
        actor=ADMIN,
    )


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def test_the_same_password_hashes_differently_every_time():
    # A per-password salt is what stops one cracked hash from unlocking every
    # other account that chose the same password, and what makes a precomputed
    # table useless against the whole file.
    first_hash, first_salt, _ = hash_password(PASSWORD)
    second_hash, second_salt, _ = hash_password(PASSWORD)

    assert first_salt != second_salt
    assert first_hash != second_hash
    assert PASSWORD not in first_hash


def test_verify_accepts_the_right_password_and_rejects_a_wrong_one():
    digest, salt, params = hash_password(PASSWORD)

    assert verify_password(PASSWORD, digest, salt, params) is True
    assert verify_password(PASSWORD + "!", digest, salt, params) is False
    assert verify_password(PASSWORD.upper(), digest, salt, params) is False
    assert verify_password("", digest, salt, params) is False


def test_a_hash_still_verifies_after_the_module_default_changes(monkeypatch):
    # The reason kdf_params sits on the row. If verifying assumed the module's
    # current cost, raising it would lock out every existing user -- so nobody
    # would raise it, and the cost would stay at whatever was chosen in 2026.
    old_hash, old_salt, old_params = hash_password(PASSWORD)
    assert json.loads(old_params)["n"] == identity.KDF_N

    monkeypatch.setattr(identity, "KDF_N", identity.KDF_N * 2)

    new_hash, new_salt, new_params = hash_password(PASSWORD)
    assert json.loads(new_params)["n"] == identity.KDF_N
    assert json.loads(new_params)["n"] != json.loads(old_params)["n"]

    # The password written under the old cost still opens the account.
    assert verify_password(PASSWORD, old_hash, old_salt, old_params) is True
    assert verify_password(PASSWORD, new_hash, new_salt, new_params) is True
    # And the two hashes are not interchangeable, so nothing is being ignored.
    assert old_hash != new_hash
    assert verify_password(PASSWORD, old_hash, old_salt, new_params) is False


def test_params_written_by_hand_are_read_back_rather_than_assumed():
    # Built with hashlib directly, at a cost the module does not use. If
    # verify_password fell back to its own constants this returns False.
    salt = secrets.token_bytes(16)
    written_n = 2**12
    assert written_n != identity.KDF_N
    digest = hashlib.scrypt(
        PASSWORD.encode("utf-8"), salt=salt, n=written_n, r=8, p=1, dklen=64
    ).hex()
    params = json.dumps({"n": written_n, "r": 8, "p": 1, "dklen": 64})

    assert verify_password(PASSWORD, digest, salt.hex(), params) is True
    assert verify_password("something else", digest, salt.hex(), params) is False


def test_an_unreadable_parameter_set_raises_rather_than_reporting_a_bad_password():
    # A corrupt row and a wrong password both deny access; only one of them is
    # a defect somebody has to fix, so they must not look the same.
    digest, salt, _ = hash_password(PASSWORD)

    unusable = (
        "{not json",
        "[]",
        '{"n":16384,"r":8}',
        '{"n":3,"r":8,"p":1,"dklen":64}',
    )
    for broken in unusable:
        with pytest.raises(ValueError):
            verify_password(PASSWORD, digest, salt, broken)


def test_a_password_below_the_floor_is_refused():
    with pytest.raises(ValueError):
        hash_password("short")
    assert len("short") < MIN_PASSWORD_LENGTH


def test_a_session_token_is_stored_only_as_a_hash():
    token = new_session_token()
    stored = hash_session_token(token)

    assert token not in stored
    assert len(stored) == 64
    assert session_token_matches(token, stored) is True
    assert session_token_matches(token + "x", stored) is False
    assert session_token_matches("", stored) is False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def test_a_created_user_stores_a_hash_and_never_the_password():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)

        assert user.password_hash != PASSWORD
        assert PASSWORD not in user.password_hash
        assert user.password_salt
        assert verify_password(
            PASSWORD, user.password_hash, user.password_salt, user.kdf_params
        )
        assert user.last_login_at is None, "never logged in is not the same as just now"
        assert user.failed_attempts == 0


def test_an_email_is_normalised_on_the_way_in_and_on_the_way_out():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = create_user(
            session,
            COMPANY,
            email="  Dana.Okafor@MEP.example  ",
            display_name="Dana Okafor",
            password=PASSWORD,
            actor=ADMIN,
        )
        assert user.email == "dana.okafor@mep.example"
        assert user_by_email(session, COMPANY, "DANA.OKAFOR@mep.EXAMPLE") is user


def test_the_same_address_twice_in_one_company_is_refused():
    init_db()
    with session_scope() as session:
        _fresh(session)
        _analyst(session)
        with pytest.raises(ValueError):
            _analyst(session)


def test_the_same_address_in_two_companies_is_two_people():
    init_db()
    with session_scope() as session:
        _fresh(session)
        mine = _analyst(session, email="shared@example.com")
        theirs = _analyst(session, email="shared@example.com", company=RIVAL)

        assert mine.id != theirs.id
        assert mine.company_id == COMPANY
        assert theirs.company_id == RIVAL


def test_a_malformed_address_is_refused_at_the_write_and_ignored_at_the_read():
    init_db()
    with session_scope() as session:
        _fresh(session)
        with pytest.raises(ValueError):
            create_user(
                session,
                COMPANY,
                email="not-an-address",
                display_name="Nobody",
                password=PASSWORD,
                actor=ADMIN,
            )
        # A login form must not be turned into an oracle by typing rubbish.
        assert user_by_email(session, COMPANY, "not-an-address") is None
        assert user_by_email(session, COMPANY, "") is None

    with pytest.raises(ValueError):
        normalise_email("two words@example.com")


# ---------------------------------------------------------------------------
# Roles, permissions and the union
# ---------------------------------------------------------------------------


def test_the_analyst_cannot_approve_which_is_the_whole_control():
    init_db()
    with session_scope() as session:
        _fresh(session)
        analyst = permissions_for_role(session, COMPANY, "analyst")
        owner = permissions_for_role(session, COMPANY, "obligation_owner")
        admin = permissions_for_role(session, COMPANY, "admin")

        assert "action.propose" in analyst
        assert "action.approve" not in analyst, "segregation of duties is this absence"
        assert "action.reject" not in analyst
        assert "action.approve" in owner
        assert "action.reject" in owner
        # The administrator manages people; it is not a way into the decision.
        assert "user.manage" in admin
        assert "action.approve" not in admin


def test_permissions_are_the_union_of_two_roles():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)

        grant_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)
        after_one = permissions_for_user(session, COMPANY, user.id)
        assert after_one == frozenset(SYSTEM_ROLE_PERMISSIONS["analyst"])

        grant_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)
        after_two = permissions_for_user(session, COMPANY, user.id)

        assert after_two == frozenset(SYSTEM_ROLE_PERMISSIONS["analyst"]) | frozenset(
            SYSTEM_ROLE_PERMISSIONS["admin"]
        )
        assert after_one < after_two
        # Overlapping codes are a set, not a bag.
        assert len(after_two) == len(set(after_two))


def test_revoking_removes_the_permission_and_leaves_the_grant_readable():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)
        grant_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)

        assert "user.manage" in permissions_for_user(session, COMPANY, user.id)

        revoke_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)

        after = permissions_for_user(session, COMPANY, user.id)
        assert "user.manage" not in after
        assert after == frozenset(SYSTEM_ROLE_PERMISSIONS["analyst"])

        # The history of who could do what, when, has to survive the revoke.
        grants = role_grants_for_user(session, COMPANY, user.id)
        assert len(grants) == 2
        revoked = [row for row in grants if row.revoked_at is not None]
        assert len(revoked) == 1
        assert revoked[0].granted_by == ADMIN
        assert revoked[0].granted_at is not None
        assert revoked[0].revoked_at >= revoked[0].granted_at

        live = role_grants_for_user(session, COMPANY, user.id, include_revoked=False)
        assert len(live) == 1


def test_granting_the_same_role_twice_writes_one_row_and_one_event():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        before = event_count(session, COMPANY)

        first = grant_role(
            session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN
        )
        second = grant_role(
            session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN
        )

        assert first is second
        assert event_count(session, COMPANY) == before + 1
        assert len(role_grants_for_user(session, COMPANY, user.id)) == 1


def test_revoking_a_role_the_user_does_not_hold_is_refused():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        with pytest.raises(ValueError):
            revoke_role(
                session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN
            )


def test_a_regrant_after_a_revoke_restores_the_permission_and_keeps_both_rows():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)
        revoke_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)
        grant_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)

        assert "user.manage" in permissions_for_user(session, COMPANY, user.id)
        assert len(role_grants_for_user(session, COMPANY, user.id)) == 2


def test_a_suspended_user_holds_nothing_while_the_grants_stay_on_the_record():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)
        assert permissions_for_user(session, COMPANY, user.id)

        set_user_status(session, COMPANY, user.id, "suspended", ADMIN)

        assert permissions_for_user(session, COMPANY, user.id) == frozenset()
        assert user_has_permission(session, COMPANY, user.id, "claim.read") is False
        # Suspension is not deletion: the grant is still there to be read.
        assert len(role_grants_for_user(session, COMPANY, user.id)) == 1

        set_user_status(session, COMPANY, user.id, "active", ADMIN)
        assert "claim.read" in permissions_for_user(session, COMPANY, user.id)


def test_revoking_closes_every_live_grant_of_the_same_role():
    # There is no unique index over (user_id, role_id), on purpose -- grant,
    # revoke and re-grant are three legitimate rows. So a duplicate is possible,
    # and revoking the newest while leaving an older one would take the
    # permission away in the interface and leave it in place in the check.
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)

        duplicate = UserRole(
            user_id=user.id,
            role_id="role-admin",
            company_id=COMPANY,
            granted_by=ADMIN,
            granted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            revoked_at=None,
        )
        session.add(duplicate)
        session.flush()
        assert len(role_grants_for_user(session, COMPANY, user.id)) == 2

        revoke_role(session, COMPANY, user_id=user.id, role_name="admin", actor=ADMIN)

        assert permissions_for_user(session, COMPANY, user.id) == frozenset()
        live = role_grants_for_user(session, COMPANY, user.id, include_revoked=False)
        assert live == []


def test_losing_and_regaining_authority_are_separate_action_codes():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        set_user_status(session, COMPANY, user.id, "suspended", ADMIN)
        set_user_status(session, COMPANY, user.id, "suspended", ADMIN)
        set_user_status(session, COMPANY, user.id, "active", ADMIN)

        actions = [
            row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq)
            .all()
        ]
        # Three writes, two decisions: re-stating a status is not a decision.
        assert actions == ["user.created", "user.suspended", "user.reinstated"]
        assert verify_chain(session, COMPANY) is True


def test_an_unknown_user_holds_nothing_rather_than_raising():
    init_db()
    with session_scope() as session:
        _fresh(session)
        assert permissions_for_user(session, COMPANY, "usr-missing") == frozenset()
        assert user_has_permission(session, COMPANY, "usr-nope", "audit.read") is False


def test_seeding_the_roles_twice_changes_nothing():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        roles = session.query(Role).count()
        permissions = session.query(Permission).count()
        grid = session.query(RolePermission).count()

        ensure_system_roles(session)

        assert session.query(Role).count() == roles
        assert session.query(Permission).count() == permissions
        assert session.query(RolePermission).count() == grid


def test_a_code_dropped_from_the_grid_is_dropped_from_the_role(monkeypatch):
    # A role bundle half-migrated does not fail: it answers every check
    # plausibly and wrongly. Principle 27 -- the grid is asserted whole.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        assert "steer.issue" in permissions_for_role(session, COMPANY, "analyst")

        trimmed = tuple(
            code for code in SYSTEM_ROLE_PERMISSIONS["analyst"] if code != "steer.issue"
        )
        monkeypatch.setitem(SYSTEM_ROLE_PERMISSIONS, "analyst", trimmed)
        ensure_system_roles(session)

        assert "steer.issue" not in permissions_for_role(session, COMPANY, "analyst")
        assert "action.propose" in permissions_for_role(session, COMPANY, "analyst")


def test_the_grid_and_the_vocabulary_cover_each_other(monkeypatch):
    # The coverage check written as a test, so the next edit to either list
    # cannot quietly leave a permission granted to nobody or granted from
    # nowhere. A grid row for a code with no permission behind it survives the
    # write on SQLite and disappears at the join: granted in the table, denied
    # in the check, and silent about it.
    granted = {code for codes in SYSTEM_ROLE_PERMISSIONS.values() for code in codes}

    assert granted <= set(PERMISSION_CODES)
    assert set(PERMISSION_CODES) <= granted, "a code no role grants is unreachable"

    monkeypatch.setitem(SYSTEM_ROLE_PERMISSIONS, "admin", ("invented.code",))
    init_db()
    with session_scope() as session:
        with pytest.raises(ValueError):
            ensure_system_roles(session)


def test_roles_available_to_a_company_are_the_system_ones():
    init_db()
    with session_scope() as session:
        _fresh(session)
        names = {role.name for role in roles_for_company(session, COMPANY)}
        assert names == {"analyst", "obligation_owner", "admin"}
        assert role_for_company(session, COMPANY, "analyst") is not None
        assert role_for_company(session, COMPANY, "auditor-general") is None


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


def test_a_user_in_one_company_is_invisible_to_another():
    init_db()
    with session_scope() as session:
        _fresh(session)
        mine = _analyst(session)
        grant_role(session, COMPANY, user_id=mine.id, role_name="analyst", actor=ADMIN)

        assert user_for_company(session, RIVAL, mine.id) is None
        assert user_by_email(session, RIVAL, mine.email) is None
        assert users_for_company(session, RIVAL) == []
        assert permissions_for_user(session, RIVAL, mine.id) == frozenset()

        # And the other tenant cannot grant against the id either.
        with pytest.raises(ValueError):
            grant_role(session, RIVAL, user_id=mine.id, role_name="admin", actor=ADMIN)
        with pytest.raises(ValueError):
            revoke_role(
                session, RIVAL, user_id=mine.id, role_name="analyst", actor=ADMIN
            )
        with pytest.raises(ValueError):
            set_user_status(session, RIVAL, mine.id, "suspended", ADMIN)

        # The grant itself is untouched by any of that.
        assert "claim.read" in permissions_for_user(session, COMPANY, mine.id)


def test_a_grant_scoped_to_another_company_is_not_counted():
    # The one column a bad write would have got wrong is UserRole.company_id,
    # so the read must not trust it alone -- nor trust it alone in reverse.
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)

        stray = session.query(UserRole).one()
        stray.company_id = RIVAL
        session.flush()

        assert permissions_for_user(session, COMPANY, user.id) == frozenset()
        assert permissions_for_user(session, RIVAL, user.id) == frozenset()


def test_every_function_refuses_an_unscoped_call():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)

        calls = (
            lambda scope: users_for_company(session, scope),
            lambda scope: user_for_company(session, scope, user.id),
            lambda scope: user_by_email(session, scope, user.email),
            lambda scope: roles_for_company(session, scope),
            lambda scope: role_for_company(session, scope, "analyst"),
            lambda scope: permissions_for_role(session, scope, "analyst"),
            lambda scope: permissions_for_user(session, scope, user.id),
            lambda scope: user_has_permission(session, scope, user.id, "claim.read"),
            lambda scope: role_grants_for_user(session, scope, user.id),
            lambda scope: set_user_status(session, scope, user.id, "suspended", ADMIN),
            lambda scope: create_user(
                session,
                scope,
                email="new@mep.example",
                display_name="New Person",
                password=PASSWORD,
                actor=ADMIN,
            ),
            lambda scope: grant_role(
                session, scope, user_id=user.id, role_name="analyst", actor=ADMIN
            ),
            lambda scope: revoke_role(
                session, scope, user_id=user.id, role_name="analyst", actor=ADMIN
            ),
        )

        for call in calls:
            for scope in ("", None):
                with pytest.raises(ValueError):
                    call(scope)


def test_an_unattributed_write_is_refused():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        for actor in ("", None):
            with pytest.raises(ValueError):
                create_user(
                    session,
                    COMPANY,
                    email="another@mep.example",
                    display_name="Another",
                    password=PASSWORD,
                    actor=actor,
                )
            with pytest.raises(ValueError):
                grant_role(
                    session, COMPANY, user_id=user.id, role_name="analyst", actor=actor
                )


# ---------------------------------------------------------------------------
# One audit log, not two
# ---------------------------------------------------------------------------


def test_identity_writes_land_in_the_existing_chain_and_it_still_verifies():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)
        grant_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)
        revoke_role(session, COMPANY, user_id=user.id, role_name="analyst", actor=ADMIN)

        assert event_count(session, COMPANY) == 3
        assert verify_chain(session, COMPANY) is True

        actions = [
            row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq)
            .all()
        ]
        assert actions == ["user.created", "user.role_granted", "user.role_revoked"]


def test_the_audit_entry_carries_the_account_and_never_the_secret():
    init_db()
    with session_scope() as session:
        _fresh(session)
        user = _analyst(session)

        entry = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq)
            .first()
        )
        assert entry.subject_id == user.id
        assert user.email in entry.reason
        assert PASSWORD not in entry.reason
        assert user.password_hash not in entry.reason
        assert user.password_salt not in entry.reason


def test_seeding_the_roles_writes_nothing_to_any_company_chain():
    # ensure_system_roles is vocabulary, not a decision. Inventing a company to
    # satisfy record_event would put a fabricated row in a tamper-evident log.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        assert event_count(session, COMPANY) == 0
        assert session.query(User).count() == 0
