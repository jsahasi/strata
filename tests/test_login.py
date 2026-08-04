"""The login surface: what it refuses, what it records, and what it gives away.

Eight failures this file exists to catch, each with a cheap wrong version that
reads fine:

1. A refusal that says which half was wrong. The message for an unknown address
   and the message for a wrong password are compared byte for byte, because a
   difference of one word turns the form into a list of who has an account.
2. A throttle that counts to five and forgets. login() writes the failed attempt
   and raises; a caller that catches the exception outside its transaction rolls
   the counter back, and the lockout then never happens however carefully it was
   written. The HTTP test below is what would notice.
3. An early return on an unknown address. The password check has to run either
   way, or the response time answers the question the message refuses to.
4. A session that outlives its expiry, or one that survives being revoked.
5. A cookie a script can read, or one a cross-site POST carries.
6. A protected screen reachable with no session at all.
7. A password written into an append-only log by somebody typing it in the email
   box, where it can never be taken out again.
8. A chain that stops verifying once logins are in it.

The suite drives the real ASGI stack -- middleware, cookies, redirects -- over
https://testserver, because the session cookie is marked Secure and a client on
http would silently drop it. That is the production configuration, tested as it
ships rather than with the flag turned off for the convenience of the test.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import sessions
from app.auth.sessions import (
    LOCKOUT,
    LOGIN_REFUSED,
    MAX_FAILED_ATTEMPTS,
    UNPARSEABLE_ADDRESS,
    AuthFailed,
    login,
    logout,
    resolve_session,
    revoke_all_for_user,
)
from app.state.audit import (
    ACTION_LOGIN_FAILED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_LOGOUT,
    ACTION_SESSION_EXPIRED,
    ACTOR_USER,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role
from app.state.models import ROLE_ANALYST, AuditEvent, LoginSession, User
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.deps import (
    LOGIN_URL,
    SESSION_COOKIE,
    install_auth,
    principal_for_token,
)
from app.web.views import auth as auth_view
from app.web.views import proceedings as proceedings_view

COMPANY = "MEP"
RIVAL = "RIVAL"

ANALYST = "denise.okoro@mep.example"
PASSWORD = "correct-horse-battery"
WRONG = "wrong-horse-battery"
UNKNOWN = "nobody@mep.example"

# A protected screen. Any of them would do; this one needs no corpus.
# The proceedings list, which is the screen this file mounts. It moved off
# "/" when the project list took the landing path.
PROTECTED = "/proceedings"

SEEDED = "system:test"


def _build_app() -> FastAPI:
    """The real assembly: the login router, a screen, and the guard in front."""
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_view.router)
    app.include_router(proceedings_view.router)
    install_auth(app)
    return app


def _make_user(
    company_id: str = COMPANY,
    email: str = ANALYST,
    password: str = PASSWORD,
    status: str = "active",
) -> str:
    with session_scope() as session:
        ensure_system_roles(session)
        user = create_user(
            session,
            company_id,
            email=email,
            display_name="Denise Okoro",
            password=password,
            actor=SEEDED,
            status=status,
        )
        if status == "active":
            grant_role(
                session,
                company_id,
                user_id=user.id,
                role_name=ROLE_ANALYST,
                actor=SEEDED,
            )
        return user.id


@pytest.fixture(autouse=True)
def fresh_database(monkeypatch):
    """One empty database per test, and no demo panel bleeding into assertions."""
    monkeypatch.setenv("STRATA_DEMO_ACCOUNTS", "0")
    monkeypatch.delenv("STRATA_COMPANY_ID", raising=False)
    init_db()


@pytest.fixture
def client() -> TestClient:
    """https, so the Secure cookie survives the round trip. See the docstring."""
    return TestClient(_build_app(), base_url="https://testserver")


def _sign_in(client: TestClient, email: str = ANALYST, password: str = PASSWORD):
    return client.post(
        LOGIN_URL, data={"email": email, "password": password}, follow_redirects=False
    )


def _events(company_id: str = COMPANY) -> list[AuditEvent]:
    with session_scope() as session:
        return list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.company_id == company_id)
                .order_by(AuditEvent.seq)
            ).scalars()
        )


def _of(action: str, company_id: str = COMPANY) -> list[AuditEvent]:
    return [row for row in _events(company_id) if row.action == action]


# --------------------------------------------------------------- signing in


def test_a_good_password_signs_in_and_the_cookie_is_locked_down(client):
    """The three cookie flags, asserted on the wire rather than in a constant."""
    _make_user()

    response = _sign_in(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{SESSION_COOKIE}=")
    lowered = raw.lower()
    assert "httponly" in lowered, "a script that can read the cookie holds the session"
    assert "samesite=lax" in lowered, "without it a cross-site POST carries the session"
    assert "secure" in lowered, "the session must not travel in the clear"
    assert "path=/" in lowered

    # The screen behind the wall now answers.
    assert client.get(PROTECTED).status_code == 200


def test_the_secure_flag_comes_off_only_for_plain_http_to_this_machine():
    """The one relaxation, pinned from both sides.

    Loopback over http drops Secure, because some browsers refuse to keep a
    Secure cookie there and `make run` would be a login page nobody can get
    past. Any other host keeps it, http or not -- that is the whole difference
    between a local convenience and a hole.
    """
    _make_user()

    local = TestClient(_build_app(), base_url="http://127.0.0.1:8000")
    assert "secure" not in _sign_in(local).headers["set-cookie"].lower()

    remote = TestClient(_build_app(), base_url="http://strata.example")
    assert "secure" in _sign_in(remote).headers["set-cookie"].lower()


def test_the_variable_overrules_the_loopback_rule_in_both_directions(monkeypatch):
    """A deployment that has decided this is not second-guessed by a host check."""
    _make_user()

    monkeypatch.setenv("STRATA_COOKIE_SECURE", "1")
    local = TestClient(_build_app(), base_url="http://127.0.0.1:8000")
    assert "secure" in _sign_in(local).headers["set-cookie"].lower()

    monkeypatch.setenv("STRATA_COOKIE_SECURE", "0")
    remote = TestClient(_build_app(), base_url="https://strata.example")
    assert "secure" not in _sign_in(remote).headers["set-cookie"].lower()


def test_the_token_in_the_cookie_is_not_what_the_database_stores(client):
    """A stolen table must not be a drawer of working sessions."""
    _make_user()
    _sign_in(client)

    token = client.cookies.get(SESSION_COOKIE)
    assert token

    with session_scope() as session:
        rows = session.query(LoginSession).all()
        assert len(rows) == 1
        assert rows[0].token_hash != token
        assert token not in rows[0].token_hash


def test_signing_in_records_who_and_which_session(client):
    """Attribution is the point of the login, not a by-product of it."""
    user_id = _make_user()
    _sign_in(client)

    succeeded = _of(ACTION_LOGIN_SUCCEEDED)
    assert len(succeeded) == 1
    row = succeeded[0]
    assert row.actor_kind == ACTOR_USER
    assert row.actor_user_id == user_id
    assert row.session_id and row.session_id.startswith("sess-")
    assert PASSWORD not in row.reason


def test_a_signed_in_person_is_not_shown_the_form_again(client):
    _make_user()
    _sign_in(client)

    response = client.get(LOGIN_URL, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ------------------------------------------------------------------ refusals


def test_a_bad_password_is_refused_and_recorded(client):
    """The refusal has to survive the transaction that raised it."""
    user_id = _make_user()

    response = _sign_in(client, password=WRONG)

    assert response.status_code == 401
    assert LOGIN_REFUSED in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")

    failed = _of(ACTION_LOGIN_FAILED)
    assert len(failed) == 1, "the audit row was rolled back with the exception"
    assert failed[0].actor_user_id == user_id
    assert failed[0].actor_kind == ACTOR_USER

    with session_scope() as session:
        assert session.get(User, user_id).failed_attempts == 1


def test_an_unknown_address_and_a_wrong_password_answer_identically(client):
    """Byte for byte. One word of difference is an account oracle."""
    _make_user()

    wrong = _sign_in(client, password=WRONG)
    unknown = _sign_in(client, email=UNKNOWN, password=PASSWORD)

    assert wrong.status_code == unknown.status_code == 401

    # The address the person typed is returned to the box, so the two pages
    # differ there and must differ nowhere else. Blanking that one value is
    # what makes the rest of the comparison exact rather than approximate.
    assert wrong.text.replace(ANALYST, "@") == unknown.text.replace(UNKNOWN, "@")

    reasons = [row.reason for row in _of(ACTION_LOGIN_FAILED)]
    assert len(reasons) == 2
    assert reasons[0] != reasons[1], "the log must tell apart what the page does not"


def test_an_unknown_address_still_pays_for_a_password_check(monkeypatch):
    """The equalisation, asserted as a call rather than as a stopwatch.

    A timing assertion on a shared machine is flaky and proves less: what
    matters is that the derivation runs on the path where there is no user, and
    that is a fact about the code rather than about the clock.
    """
    _make_user()
    calls: list[tuple] = []
    real = sessions.verify_password

    def counted(password, hash_hex, salt_hex, params):
        calls.append((hash_hex, salt_hex))
        return real(password, hash_hex, salt_hex, params)

    monkeypatch.setattr(sessions, "verify_password", counted)

    with session_scope() as session:
        with pytest.raises(AuthFailed):
            login(session, COMPANY, email=UNKNOWN, password=PASSWORD, ip="203.0.113.7")

    assert len(calls) == 1, "an unknown address returned before hashing anything"
    assert calls[0][0] == sessions._DUMMY_HASH


def test_a_suspended_account_cannot_sign_in(client):
    """Suspension has to reach the login path, not only the permission read."""
    _make_user(status="suspended")

    response = _sign_in(client)

    assert response.status_code == 401
    assert LOGIN_REFUSED in response.text
    assert "suspend" not in response.text.lower()
    assert "not active" in _of(ACTION_LOGIN_FAILED)[0].reason


def test_another_tenants_account_is_not_reachable_from_this_one(client):
    """The same address in two companies is two people, and login knows it."""
    _make_user(company_id=RIVAL, email=ANALYST)

    response = _sign_in(client)

    assert response.status_code == 401
    assert client.cookies.get(SESSION_COOKIE) is None
    assert _of(ACTION_LOGIN_FAILED, RIVAL) == []
    assert len(_of(ACTION_LOGIN_FAILED, COMPANY)) == 1


# ------------------------------------------------------------------- lockout


def test_five_failures_lock_the_account_and_the_sixth_is_refused(client):
    """The right password after five wrong ones must not open the door."""
    user_id = _make_user()

    for _ in range(MAX_FAILED_ATTEMPTS):
        assert _sign_in(client, password=WRONG).status_code == 401

    with session_scope() as session:
        user = session.get(User, user_id)
        assert user.failed_attempts == MAX_FAILED_ATTEMPTS
        assert user.locked_until is not None

    sixth = _sign_in(client)
    assert sixth.status_code == 401
    assert LOGIN_REFUSED in sixth.text
    assert "lock" not in sixth.text.lower(), "the page must not disclose the lock"
    assert client.cookies.get(SESSION_COOKIE) is None

    failed = _of(ACTION_LOGIN_FAILED)
    assert len(failed) == MAX_FAILED_ATTEMPTS + 1
    assert "locked" in failed[-1].reason, "the log says what the page will not"


def test_the_lock_lets_go_and_a_good_password_clears_the_count():
    """Fifteen minutes later the account works, and the counter starts again."""
    user_id = _make_user()
    start = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    with session_scope() as session:
        for offset in range(MAX_FAILED_ATTEMPTS):
            with pytest.raises(AuthFailed):
                login(
                    session,
                    COMPANY,
                    email=ANALYST,
                    password=WRONG,
                    now=start + timedelta(seconds=offset),
                )

    # The lock runs from the LAST failure, not the first, so the wait is the
    # lockout plus the span the five attempts took.
    later = start + LOCKOUT + timedelta(minutes=1)
    with session_scope() as session:
        live, token = login(
            session, COMPANY, email=ANALYST, password=PASSWORD, now=later
        )
        assert token
        user = session.get(User, user_id)
        assert user.failed_attempts == 0
        assert user.locked_until is None
        assert user.last_login_at == later


def test_a_wrong_password_during_the_lock_does_not_extend_it():
    """A lock an attacker can renew is a way to keep a real person out."""
    user_id = _make_user()
    start = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    with session_scope() as session:
        for offset in range(MAX_FAILED_ATTEMPTS):
            with pytest.raises(AuthFailed):
                login(
                    session,
                    COMPANY,
                    email=ANALYST,
                    password=WRONG,
                    now=start + timedelta(seconds=offset),
                )
        locked_at = session.get(User, user_id).locked_until

    with session_scope() as session:
        with pytest.raises(AuthFailed):
            login(
                session,
                COMPANY,
                email=ANALYST,
                password=WRONG,
                now=start + timedelta(minutes=5),
            )
        assert session.get(User, user_id).locked_until == locked_at


# ------------------------------------------------------------------ sessions


def test_an_expired_token_resolves_to_nothing():
    _make_user()
    start = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    with session_scope() as session:
        live, token = login(
            session,
            COMPANY,
            email=ANALYST,
            password=PASSWORD,
            now=start,
            ttl=timedelta(hours=1),
        )
        session_id = live.id

    after = start + timedelta(hours=1, seconds=1)
    with session_scope() as session:
        assert resolve_session(session, token, now=after) is None
        closed = session.get(LoginSession, session_id)
        assert closed.revoked_at == after
        assert closed.revoked_reason == "expired"

    expired = _of(ACTION_SESSION_EXPIRED)
    assert len(expired) == 1

    # Replaying the dead token writes no second row. An unauthenticated caller
    # must not be able to grow the log one request at a time.
    with session_scope() as session:
        assert resolve_session(session, token, now=after) is None
    assert len(_of(ACTION_SESSION_EXPIRED)) == 1


def test_a_revoked_token_resolves_to_nothing():
    _make_user()
    with session_scope() as session:
        live, token = login(session, COMPANY, email=ANALYST, password=PASSWORD)
        session_id = live.id

    with session_scope() as session:
        logout(session, session_id, f"person:{ANALYST}", company_id=COMPANY)

    with session_scope() as session:
        assert resolve_session(session, token) is None


def test_a_token_nobody_issued_resolves_to_nothing():
    _make_user()
    with session_scope() as session:
        assert resolve_session(session, "not-a-token-anybody-issued") is None
        assert resolve_session(session, "") is None


def test_suspending_the_account_stops_the_session_it_already_holds():
    """A suspension that leaves live cookies working is a suspension in one table."""
    user_id = _make_user()
    with session_scope() as session:
        _, token = login(session, COMPANY, email=ANALYST, password=PASSWORD)

    with session_scope() as session:
        session.get(User, user_id).status = "suspended"

    with session_scope() as session:
        assert resolve_session(session, token) is None


def test_logout_revokes_the_session_and_clears_the_cookie(client):
    _make_user()
    _sign_in(client)
    assert client.get(PROTECTED).status_code == 200

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_URL)

    with session_scope() as session:
        row = session.query(LoginSession).one()
        assert row.revoked_at is not None
        assert row.revoked_reason == "user signed out"

    assert len(_of(ACTION_LOGOUT)) == 1
    # The browser is signed out too: the next request is sent back to /login.
    assert client.get(PROTECTED, follow_redirects=False).status_code == 303


def test_logging_out_twice_is_not_two_decisions():
    _make_user()
    with session_scope() as session:
        live, _ = login(session, COMPANY, email=ANALYST, password=PASSWORD)
        session_id = live.id

    for _ in range(2):
        with session_scope() as session:
            logout(session, session_id, "person:someone", company_id=COMPANY)

    assert len(_of(ACTION_LOGOUT)) == 1


def test_another_tenant_cannot_end_this_tenants_session():
    _make_user()
    with session_scope() as session:
        live, token = login(session, COMPANY, email=ANALYST, password=PASSWORD)
        session_id = live.id

    with session_scope() as session:
        assert logout(session, session_id, "person:intruder", company_id=RIVAL) is None

    with session_scope() as session:
        assert resolve_session(session, token) is not None


def test_revoking_every_session_ends_all_of_them_and_names_each():
    user_id = _make_user()
    tokens = []
    with session_scope() as session:
        for _ in range(3):
            _, token = login(session, COMPANY, email=ANALYST, password=PASSWORD)
            tokens.append(token)

    with session_scope() as session:
        closed = revoke_all_for_user(
            session, COMPANY, user_id, actor="person:admin", reason="password changed"
        )
    assert closed == 3

    with session_scope() as session:
        for token in tokens:
            assert resolve_session(session, token) is None

    assert len(_of(ACTION_LOGOUT)) == 3

    with session_scope() as session:
        again = revoke_all_for_user(
            session, COMPANY, user_id, actor="person:admin", reason="password changed"
        )
    assert again == 0, "nothing left to close, and nothing written for it"


# ----------------------------------------------------------------- the guard


def test_an_anonymous_request_to_a_protected_route_goes_to_login(client):
    response = client.get(PROTECTED, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_URL)


def test_the_login_page_and_the_health_check_stay_open(client):
    assert client.get(LOGIN_URL).status_code == 200
    assert client.get("/static/strata.css").status_code == 200


def test_the_wall_remembers_where_the_person_was_going(client):
    _make_user()
    wall = client.get("/proceedings/MPUC-2026-0142", follow_redirects=False)
    assert wall.status_code == 303
    assert "next=" in wall.headers["location"]

    form = client.get(wall.headers["location"])
    assert "/proceedings/MPUC-2026-0142" in form.text


def test_the_next_parameter_cannot_send_anyone_off_site(client):
    """An open redirect on a login page is a phishing tool with our name on it."""
    _make_user()
    response = client.post(
        LOGIN_URL,
        data={"email": ANALYST, "password": PASSWORD, "next": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_forged_cookie_is_not_a_session(client):
    client.cookies.set(SESSION_COOKIE, "made-up-token", domain="testserver")
    response = client.get(PROTECTED, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_URL)


def test_a_database_with_no_accounts_says_so_rather_than_failing(client):
    """A reviewer who has not seeded gets the command, not a wall or a 500."""
    page = client.get(LOGIN_URL)
    assert page.status_code == 200
    assert "make seed" in page.text


def test_a_live_cookie_against_a_dropped_database_lands_on_the_login_page(client):
    """The one fallback in the guard, and the page it hands over to.

    A cookie from a previous run, and tables that are no longer there. The
    request must not 500 on its way to a screen it could not have served; it
    goes to the login page, which reports the missing tables in the one place a
    person can act on it.
    """
    from app.state.db import get_engine
    from app.state.models import Base

    _make_user()
    _sign_in(client)
    Base.metadata.drop_all(get_engine())

    walled = client.get(PROTECTED, follow_redirects=False)
    assert walled.status_code == 303
    assert walled.headers["location"].startswith(LOGIN_URL)

    page = client.get(LOGIN_URL)
    assert page.status_code == 200
    assert "make seed" in page.text


def test_the_tenant_follows_the_person_and_not_the_environment(client, monkeypatch):
    """current_company() derives from the user. The variable is only a fallback."""
    _make_user(company_id=RIVAL, email="rival.analyst@rival.example")
    monkeypatch.setenv("STRATA_COMPANY_ID", COMPANY)

    signed_in = client.post(
        LOGIN_URL,
        data={"email": "rival.analyst@rival.example", "password": PASSWORD},
        follow_redirects=False,
    )
    # The form authenticates against the configured tenant, which is MEP here,
    # so a RIVAL account cannot sign in through it. That is the documented
    # one-tenant-per-deployment limit, asserted rather than assumed.
    assert signed_in.status_code == 401

    monkeypatch.setenv("STRATA_COMPANY_ID", RIVAL)
    assert _sign_in(client, email="rival.analyst@rival.example").status_code == 303

    # Now move the variable back to MEP. The screens must still read RIVAL,
    # because the person signed in is a RIVAL person. This is the assertion the
    # ContextVar exists for: the view calls current_company() with no request in
    # hand, and it has to get the same answer a dependency would.
    monkeypatch.setenv("STRATA_COMPANY_ID", COMPANY)
    assert principal_for_token(client.cookies.get(SESSION_COOKIE)).company_id == RIVAL

    page = client.get(PROTECTED)
    assert page.status_code == 200
    assert RIVAL in page.text
    assert f'"{COMPANY}"' not in page.text


# --------------------------------------------------------------- the log


def test_a_password_typed_into_the_email_box_is_not_written_to_the_log(client):
    """An append-only table cannot be redacted afterwards, so it never learns it."""
    _make_user()
    secret = "Tr0ub4dor&3-and-then-some"

    response = _sign_in(client, email=secret, password=secret)
    assert response.status_code == 401

    rows = _events()
    assert rows, "the attempt was not recorded at all"
    for row in rows:
        blob = " ".join(
            part or ""
            for part in (row.actor, row.subject_id, row.reason, row.citation)
        )
        assert secret not in blob
    assert UNPARSEABLE_ADDRESS in rows[-1].actor


def test_no_audit_row_anywhere_carries_the_password(client):
    _make_user()
    _sign_in(client)
    _sign_in(client, password=WRONG)
    client.post("/logout", follow_redirects=False)

    for row in _events():
        for field in (row.actor, row.subject_id, row.reason, row.citation):
            assert PASSWORD not in (field or "")
            assert WRONG not in (field or "")


def test_the_chain_still_verifies_after_all_of_it(client):
    """One log, and it holds. Logins, failures, lockout and sign-out together."""
    _make_user()

    _sign_in(client)
    client.post("/logout", follow_redirects=False)
    for _ in range(MAX_FAILED_ATTEMPTS + 1):
        _sign_in(client, password=WRONG)
    _sign_in(client, email=UNKNOWN, password=PASSWORD)

    with session_scope() as session:
        assert verify_chain(session, COMPANY) is True

    actions = [row.action for row in _events()]
    assert ACTION_LOGIN_SUCCEEDED in actions
    assert ACTION_LOGIN_FAILED in actions
    assert ACTION_LOGOUT in actions
