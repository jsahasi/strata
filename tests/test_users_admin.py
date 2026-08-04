"""Provisioning a login, and the link that sets its password.

WHAT WAS ASKED FOR, AND WHICH TEST HOLDS EACH PART.

  "Each admin user in a workspace should be able to provision additional
   logins."                            -> the gate tests and the provision tests
  "See most recent login for each existing login."
                                       -> the never-signed-in tests, which are
                                          the ones worth reading
  "Sends/resends on request an invitation email the recipient can use for 24
   hours to set up a password (after that they need a fresh invite)."
                                       -> test_a_resend_kills_the_previous_link
                                          _the_instant_it_issues_a_new_one

THE THIRD OF THOSE IS A SECURITY REQUIREMENT AND IS THE TEST THIS FILE EXISTS
FOR. "They need a fresh invite" means an expired invitation is never extended: a
resend mints a new token and moves the old row to a terminal state, so at most
one token is ever valid for one person. Pushing expires_at forward on the
standing row would leave every copy of the first link -- in the recipient's
inbox, in the helpdesk ticket where they pasted it, in whatever archived the
mail on the way past -- working for another day, every time anybody pressed the
button. So the test does not read a column: it takes the first link, presses
resend, and tries the first link again.

WHY "NEVER SIGNED IN" GETS FIVE TESTS. An admin scanning this list is looking
for exactly two things: a dormant account that should be suspended, and an
invitation nobody ever took up. Rendered as an empty cell those two are the same
pixel, and the nullable column that keeps them apart -- app/state/models.py:
"Never logged in and logged in a year ago are different facts about an account"
-- is wasted at the last step. So there is a test that a login that happened
carries both a relative time and an exact one, one that never-signed-in says so
in words rather than as a blank, and three that hold apart the cases which look
identical if you only test the happy one:

  the link is still open        wait; nothing is wrong
  the link ran out              resend, or nobody is ever getting in
  the account works and is idle the suspension candidate

Two of those three were written after the screen was rendered and read: the
first draft told an active never-used account that no invitation had ever been
issued for it, and would have told somebody who accepted their invitation and
then never signed in that the invitation was never taken up. Both were true
sentences about the wrong fact, and both are guarded below.

TWO APPLICATIONS ARE BUILT HERE, ON PURPOSE.

  `admin` / `analyst`   the guarded application, with the real session
                        middleware. Everything on /users is behind it.
  `visitor`             the accept route with the guard installed AND
                        deps.is_public_path patched to allow the invite prefix.
                        That patch IS the two-line change asked for in the
                        handoff at the foot of app/web/views/invite_accept.py;
                        writing it here means these tests exercise the real
                        middleware rather than an application with no wall, and
                        the day the change lands the patch becomes a no-op.

The xfail at the end of the file is the same guard tests/test_sharing.py already
carries for /s/<token>: it flips to XPASS the moment app/web/deps.py lists the
prefix, and until then it says out loud that the feature is not in the product a
reviewer starts.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http drops it in silence.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    MIN_PASSWORD_LENGTH,
    create_user,
    ensure_system_roles,
    user_by_email,
)
from app.state.invites import (
    INVITE_KIND_HANDOFF,
    INVITE_KIND_PROVISION,
    require_justification,
)
from app.state.models import (
    INVITE_ACCEPTED,
    INVITE_PROVISION_TTL_HOURS,
    INVITE_SUPERSEDED,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    AuditEvent,
    Invitation,
    User,
)
from app.web import deps
from app.web.deps import install_auth
from app.web.views import auth as auth_view
from app.web.views import invite_accept, users_admin

COMPANY = "MEP"
RIVAL = "RIVAL"

USERS = users_admin.USERS_URL
PROVISION = users_admin.PROVISION_URL

NEW_PASSWORD = "a-perfectly-ordinary-passphrase"

# A token the product never issued. 43 characters, the shape of a real one, so
# the refusal is not answering "that is the wrong length".
UNKNOWN_TOKEN = "z" * 43


# --------------------------------------------------------------- fixtures


# app/notify is being written in parallel with this screen. Imported HERE and
# guarded, rather than through a module-level pytest.importorskip, because that
# call raises Skipped at collection and would take the whole file with it --
# including the resend test, which is the security requirement and has nothing
# to do with mail. Only the mail tests carry the mark.
try:
    from app import notify
except ImportError:  # pragma: no cover - the module is present in this build
    notify = None

needs_mail = pytest.mark.skipif(
    notify is None,
    reason=(
        "app/notify is not in this build. The screen's behaviour without it is "
        "covered by test_the_mail_layer_being_absent_is_announced_and_stops_nothing."
    ),
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Default tenant, and no relay. This suite opens no sockets.

    THE MAIL VARIABLES ARE CLEARED FOR EVERY TEST, not only the mail ones. Two
    exported settings on a developer's machine would otherwise make every
    provision in this file try to reach a real SMTP server, and the failure
    would be a timeout in a test about something else.
    """
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)
    if notify is not None:
        for name in (
            notify.MAIL_ENV_HOST,
            notify.MAIL_ENV_PORT,
            notify.MAIL_ENV_SENDER,
            notify.MAIL_ENV_USERNAME,
            notify.MAIL_ENV_PASSWORD,
        ):
            monkeypatch.delenv(name, raising=False)


def _email(role: str) -> str:
    """The seeded account holding a role, read from the corpus rather than typed."""
    return next(
        account.email for account in demo_account_list() if account.role == role
    )


@pytest.fixture
def anonymous() -> TestClient:
    """The two screens behind the guard the product installs.

    The corpus is NOT loaded. Nothing on either screen reads a proceeding, a
    change or a claim, and loading it costs the suite more than it proves.
    """
    init_db()
    with session_scope() as session:
        ensure_accounts(session)

    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(users_admin.router)
    app.include_router(invite_accept.router)
    install_auth(app)
    return TestClient(app, base_url="https://testserver")


def _sign_in(client: TestClient, email: str, password: str = DEMO_PASSWORD):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def admin(anonymous: TestClient) -> TestClient:
    """Signed in as the seeded account holding user.manage."""
    assert _sign_in(anonymous, _email(ROLE_ADMIN)).status_code == 303
    return anonymous


@pytest.fixture
def analyst(anonymous: TestClient) -> TestClient:
    """Signed in as somebody who reads everything and may not administer people."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
    return anonymous


@pytest.fixture
def visitor(anonymous: TestClient, monkeypatch) -> TestClient:
    """The accept route, reached with no session, through the real middleware.

    The patch is the change app/web/deps.py needs and this branch does not own.
    Written as the smallest thing that could be added there, so the test and the
    handoff cannot describe two different fixes.
    """
    real = deps.is_public_path

    def public(path: str) -> bool:
        return real(path) or path.startswith(invite_accept.ACCEPT_PATH_PREFIX + "/")

    monkeypatch.setattr(deps, "is_public_path", public)
    return TestClient(anonymous.app, base_url="https://testserver")


# ------------------------------------------------------------------ helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(email: str, company_id: str = COMPANY) -> User:
    with session_scope() as session:
        person = user_by_email(session, company_id, email)
        assert person is not None, f"{email} has no account in {company_id}"
        return person


def _provision(
    client: TestClient,
    *,
    email: str = "nina.okafor@mep.example",
    display_name: str = "Nina Okafor",
    role: str = ROLE_ADMIN,
):
    return client.post(
        PROVISION,
        data={"email": email, "display_name": display_name, "role": role},
        follow_redirects=False,
    )


def _link(body: str) -> str:
    """The one-time acceptance URL the page just rendered.

    Read out of the HTML rather than out of the database, because the raw token
    exists in exactly one place -- this response -- and a test that fetched it
    from a column would be testing something the product never shows.
    """
    found = re.findall(
        rf'{invite_accept.ACCEPT_PATH_PREFIX}/([A-Za-z0-9_-]{{20,}})', body
    )
    assert found, "the page rendered no acceptance link"
    assert len(set(found)) == 1, f"more than one token on the page: {set(found)}"
    return found[0]


def _accept_url(token: str) -> str:
    return f"{invite_accept.ACCEPT_PATH_PREFIX}/{token}"


def _row_for(body: str, email: str) -> str:
    """The one table row that mentions this address."""
    rows = [part for part in body.split("<tr") if escape(email) in part]
    assert rows, f"{email} is not on the page"
    assert len(rows) == 1, f"{email} appears in {len(rows)} rows"
    return rows[0]


def _set_last_login(email: str, moment: datetime | None) -> None:
    with session_scope() as session:
        person = user_by_email(session, COMPANY, email)
        person.last_login_at = moment


def _invitation(invitation_id: str) -> Invitation:
    with session_scope() as session:
        row = session.get(Invitation, invitation_id)
        assert row is not None, f"{invitation_id} was never written"
        return row


def _denials() -> list[AuditEvent]:
    with session_scope() as session:
        return (
            session.query(AuditEvent)
            .filter(AuditEvent.action == ACTION_ACCESS_DENIED)
            .all()
        )


# ---------------------------------------------------------------- the gate


def test_the_user_list_refuses_an_anonymous_request(anonymous):
    """It is a screen about accounts. The wall is in front of it like every other."""
    response = anonymous.get(USERS, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_an_analyst_cannot_read_the_user_list_and_the_refusal_is_recorded(analyst):
    """A hidden button is not a control. The refusal is a 403 and lands in the chain."""
    response = analyst.get(USERS)
    assert response.status_code == 403
    assert users_admin.MANAGE in response.text
    # Nobody else's address is on a page the reader was refused.
    assert _email(ROLE_ADMIN) not in response.text

    denials = _denials()
    assert denials, "the refusal was not recorded"
    assert all(row.company_id == COMPANY for row in denials)


def test_an_analyst_cannot_provision_a_login(analyst):
    """The write path is gated on its own, not by the page hiding the form."""
    response = _provision(analyst, email="anybody@mep.example")
    assert response.status_code == 403
    with session_scope() as session:
        assert user_by_email(session, COMPANY, "anybody@mep.example") is None


def test_an_analyst_cannot_resend_an_invitation(admin, analyst_client_factory):
    """Provision as the admin, then try the resend button as the analyst."""
    body = _provision(admin).text
    invitation_id = _invitation_id_on(body)

    analyst = analyst_client_factory()
    response = analyst.post(
        users_admin.resend_url(invitation_id), follow_redirects=False
    )
    assert response.status_code == 403
    assert _invitation(invitation_id).status != INVITE_SUPERSEDED


@pytest.fixture
def analyst_client_factory(anonymous):
    """Sign the same client in as the analyst, after it has been the admin."""

    def factory() -> TestClient:
        assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
        return anonymous

    return factory


def _invitation_id_on(body: str) -> str:
    found = re.findall(r"INV-\d{4}", body)
    assert found, "no invitation id on the page"
    return found[0]


# ------------------------------------------------- what the list has to say


def test_the_list_shows_every_account_with_its_roles_and_status(admin):
    body = admin.get(USERS).text
    assert admin.get(USERS).status_code == 200
    for account in demo_account_list():
        assert escape(account.email) in body, account.email
        assert escape(account.display_name) in body, account.display_name
    assert ROLE_ADMIN in body and ROLE_ANALYST in body


def test_never_signed_in_is_rendered_as_its_own_fact(admin):
    """Not a blank cell, not a dash. models.py keeps the column nullable for this."""
    seeded = _email(ROLE_ADMIN)
    _set_last_login(seeded, None)

    row = _row_for(admin.get(USERS).text, seeded)
    assert users_admin.NEVER_SIGNED_IN in row, (
        "an account that has never signed in renders as nothing at all"
    )


def test_a_login_that_happened_shows_the_relative_time_and_the_exact_one(admin):
    """"3 months ago" is what a person scans; the date is what they act on."""
    seeded = _email(ROLE_ADMIN)
    moment = _now() - timedelta(days=92)
    _set_last_login(seeded, moment)

    row = _row_for(admin.get(USERS).text, seeded)
    assert "months ago" in row, "no relative time on a login that happened"
    assert moment.strftime("%Y-%m-%d") in row, "no exact timestamp beside it"
    assert users_admin.NEVER_SIGNED_IN not in row


def test_an_open_invitation_and_a_lapsed_one_do_not_read_the_same(admin):
    """The two things an admin is scanning for must not be the same pixel.

    Both accounts have never signed in. One has a link that still works and
    needs no action; the other has a link nobody took up and needs a resend. A
    screen that renders both as "never" has answered neither question.
    """
    open_body = _provision(admin, email="open@mep.example", display_name="Open Invite")
    assert open_body.status_code == 200

    _provision(admin, email="lapsed@mep.example", display_name="Lapsed Invite")
    # Run the second one's clock out where the product would: on the row, by
    # comparison at read time. Nothing sweeps invitations, so this is what an
    # expired invitation looks like in the morning.
    with session_scope() as session:
        row = (
            session.query(Invitation)
            .filter(Invitation.email == "lapsed@mep.example")
            .one()
        )
        row.expires_at = _now() - timedelta(hours=1)

    body = admin.get(USERS).text
    still_open = _row_for(body, "open@mep.example")
    lapsed = _row_for(body, "lapsed@mep.example")

    assert users_admin.NEVER_SIGNED_IN in still_open
    assert users_admin.NEVER_SIGNED_IN in lapsed
    assert users_admin.INVITE_STILL_OPEN in still_open
    assert users_admin.INVITE_STILL_OPEN not in lapsed
    assert users_admin.INVITE_LAPSED in lapsed
    assert users_admin.INVITE_LAPSED not in still_open


def test_an_account_that_works_and_was_never_used_says_that_and_not_the_other_thing(
    admin,
):
    """The sharpest suspension candidate on the page, and it needs its own words.

    A first draft said "no invitation has ever been issued for this account",
    which is true, irrelevant, and reads as a fault in the product rather than
    as a fact about a person.
    """
    seeded = _email(ROLE_ADMIN)
    _set_last_login(seeded, None)

    row = _row_for(admin.get(USERS).text, seeded)
    assert users_admin.NEVER_SIGNED_IN in row
    assert users_admin.NEVER_USED_ACTIVE in row
    assert users_admin.NO_INVITATION not in row


def test_somebody_who_accepted_and_never_signed_in_is_not_called_never_taken_up(
    admin, visitor
):
    """The regression guard on the branch order in _person_row.

    Their invitation IS dead -- it is dead because they used it. Asking the
    invitation before the status printed the opposite of what happened, about
    the one person on the page who did everything they were asked to.
    """
    token = _link(_provision(admin, email="took.it@mep.example", display_name="Took It").text)
    visitor.post(
        _accept_url(token), data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    )
    assert _user("took.it@mep.example").last_login_at is None

    row = _row_for(admin.get(USERS).text, "took.it@mep.example")
    assert users_admin.NEVER_SIGNED_IN in row
    assert users_admin.INVITE_LAPSED not in row, (
        "the screen said an invitation was never taken up about somebody who "
        "took it up"
    )
    assert users_admin.NEVER_USED_ACTIVE in row


def test_a_suspended_account_that_was_never_used_is_not_flagged_as_stranded(admin):
    """"No way in" is the point of a suspension, not a problem to fix.

    Counting it beside the invitations that need resending would put a number on
    the page that shrinks when an administrator does the right thing.
    """
    seeded = _email(ROLE_OBLIGATION_OWNER)
    _set_last_login(seeded, None)
    with session_scope() as session:
        user_by_email(session, COMPANY, seeded).status = STATUS_SUSPENDED

    body = admin.get(USERS).text
    row = _row_for(body, seeded)
    assert users_admin.NEVER_SIGNED_IN in row
    assert users_admin.NEVER_USED_SUSPENDED in row
    assert users_admin.NO_INVITATION not in row
    assert "users-row--stranded" not in row
    assert "have no way in" not in body


def test_a_pending_invitation_shows_when_it_runs_out(admin):
    """The admin decides whether to resend by reading the expiry, not by guessing."""
    _provision(admin, email="clock@mep.example", display_name="Clock Watcher")
    row = _row_for(admin.get(USERS).text, "clock@mep.example")

    invitation = _invitation(_invitation_id_on(row))
    assert invitation.expires_at.strftime("%Y-%m-%d") in row
    # The 24-hour clock, visible rather than only in models.py.
    life = invitation.expires_at - invitation.invited_at
    assert abs(life - timedelta(hours=INVITE_PROVISION_TTL_HOURS)) < timedelta(minutes=1)


def test_the_role_picker_offers_only_what_this_admin_may_hand_on(admin):
    """grantable_roles, rendered. A list that refuses is worse than a short one.

    An ordinary admin holds none of the analyst codes and neither approval code,
    so they may provision another admin and nothing else. That is the honest
    reading of the ceiling rule and the screen says it rather than offering a
    role the write layer will refuse.
    """
    body = admin.get(USERS).text
    assert f'value="{ROLE_ADMIN}"' in body
    assert f'value="{ROLE_ANALYST}"' not in body, (
        "the picker offers a role this admin cannot grant"
    )
    assert users_admin.CEILING_NOTE in body


# --------------------------------------------------------- provisioning


def test_provisioning_writes_an_invited_account_and_one_live_link(admin):
    response = _provision(admin)
    assert response.status_code == 200

    person = _user("nina.okafor@mep.example")
    assert person.status == STATUS_INVITED
    assert person.last_login_at is None

    invitation = _invitation(_invitation_id_on(response.text))
    assert invitation.kind == INVITE_KIND_PROVISION
    assert invitation.subject_type is None and invitation.subject_id is None
    assert invitation.invited_by_user_id == _user(_email(ROLE_ADMIN)).id


def test_the_one_time_link_is_rendered_once_and_never_put_in_a_url(admin):
    """A redirect carrying the token would file it in history, logs and Referer.

    So the page is rendered rather than redirected to, and it says why. A reload
    re-posts and the write layer answers INV_ALREADY_A_USER, which is a refusal
    rather than a second account.
    """
    response = _provision(admin)
    assert response.status_code == 200, "a token must not travel in a redirect"
    assert "location" not in {key.lower() for key in response.headers}

    token = _link(response.text)
    assert response.headers.get("cache-control") == "no-store"
    assert token not in response.headers.get("referrer-policy", "")

    with session_scope() as session:
        for row in session.query(AuditEvent).all():
            assert token not in (row.reason or ""), "the token is in the audit chain"
            assert token not in (row.subject_id or "")


def test_the_second_press_of_provision_refuses_rather_than_making_two_people(admin):
    _provision(admin)
    again = _provision(admin)
    assert again.status_code == 200
    assert users_admin.OUTCOME_HEADINGS["INV_ALREADY_A_USER"] in again.text

    with session_scope() as session:
        count = (
            session.query(User)
            .filter(User.email == "nina.okafor@mep.example")
            .count()
        )
    assert count == 1


@pytest.mark.parametrize(
    "fields,code",
    [
        ({"email": ""}, "INV_NO_EMAIL"),
        ({"email": "not-an-address"}, "INV_EMAIL_MALFORMED"),
        ({"display_name": "   "}, "INV_NO_NAME"),
        ({"role": ""}, "INV_NO_ROLE"),
        ({"role": ROLE_ANALYST}, "INV_GRANT_EXCEEDS"),
    ],
)
def test_every_refusal_is_rendered_in_the_write_layers_own_words(admin, fields, code):
    """One code per fix. "Could not add that person" is a line nobody can act on."""
    call = {
        "email": "new.person@mep.example",
        "display_name": "New Person",
        "role": ROLE_ADMIN,
    }
    call.update(fields)
    response = admin.post(PROVISION, data=call, follow_redirects=False)

    assert response.status_code == 200
    assert users_admin.OUTCOME_HEADINGS[code] in response.text
    with session_scope() as session:
        assert user_by_email(session, COMPANY, "new.person@mep.example") is None


def test_a_refusal_still_renders_the_list_underneath_it(admin):
    """An admin whose form was refused can still read who is already here."""
    response = admin.post(
        PROVISION,
        data={"email": "", "display_name": "Nobody", "role": ROLE_ADMIN},
        follow_redirects=False,
    )
    assert escape(_email(ROLE_ANALYST)) in response.text


# ---------------------------------------------------------------- the mail
#
# The screen reaches app/notify by name at the moment of use, so these skip
# rather than fail when it is absent. A skip says "this was not checked"; a pass
# on an absent module would say the mail path works.


class _Recorder:
    """A transport that keeps what it was handed and never opens a socket."""

    def __init__(self):
        self.sent = []

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})


class _Broken:
    """A relay that takes the connection and refuses the mail."""

    def send(self, **_kwargs) -> None:
        raise OSError("the relay said no")


@pytest.fixture
def post_box(monkeypatch) -> _Recorder:
    box = _Recorder()
    monkeypatch.setattr(notify, "transport_from_environment", lambda *a, **k: box)
    return box


@needs_mail
def test_provisioning_emails_the_same_link_it_puts_on_the_screen(admin, post_box):
    """One link, built once, in both places.

    A mail composing its own URL is a second chance to get the host wrong, and
    the failure would be a mail whose link is not the link the administrator was
    looking at.
    """
    body = _provision(admin).text
    token = _link(body)

    assert len(post_box.sent) == 1
    mail = post_box.sent[0]
    assert mail["to"] == "nina.okafor@mep.example"
    assert token in mail["text"], "the mail carries a different link from the screen"
    assert token in mail["html"]
    assert token not in mail["subject"], "the token is in a mail subject line"


@needs_mail
def test_with_no_relay_configured_the_screen_says_nobody_was_told(admin, monkeypatch):
    """The announced fallback, in the mail layer's own words.

    This is what a reviewer running `make run` sees. The invitation is real, the
    link is on screen, and the page does not pretend an email went out.
    """
    monkeypatch.setattr(notify, "transport_from_environment", lambda *a, **k: None)
    body = _provision(admin).text

    assert notify.ANNOUNCEMENT_NOT_CONFIGURED in body
    assert _link(body), "the link was withheld when the mail could not be sent"
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED


@needs_mail
def test_a_relay_that_refuses_does_not_take_the_account_with_it(admin, monkeypatch):
    """The account is committed before the mail is attempted, and stays."""
    monkeypatch.setattr(notify, "transport_from_environment", lambda *a, **k: _Broken())
    body = _provision(admin).text

    assert "NOT sent" in body
    assert _link(body)
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED


@needs_mail
def test_the_resend_mail_says_the_previous_link_is_dead(admin, post_box):
    """A recipient holding both mails must not be sent to the older one.

    It stopped working when the newer one was minted, not at its own expiry, and
    a credential link that fails with the single sentence a dead invitation gets
    is the moment somebody decides this product is broken.
    """
    _provision(admin)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    admin.post(users_admin.resend_url(invitation_id), follow_redirects=False)

    assert len(post_box.sent) == 2
    first, second = post_box.sent
    # The subjects are templates with the product and company filled in, so the
    # assertion is on the shape either side of the substitution rather than on a
    # rendered string this test would have to keep in step.
    assert first["subject"] != second["subject"]
    assert second["subject"].startswith(notify.SUBJECT_RESEND.split("{")[0])
    assert "stopped working" in second["text"], (
        "the replacement mail does not tell the recipient the older link is dead"
    )
    assert "stopped working" not in first["text"]
    # And the two links are different, which is what "stopped working" is about.
    assert _link(second["text"]) != _link(first["text"])


@needs_mail
def test_a_refused_provision_sends_nothing(admin, post_box):
    """There is no link, so there is nothing to deliver and nobody to deliver to."""
    admin.post(
        PROVISION,
        data={"email": "", "display_name": "Nobody", "role": ROLE_ADMIN},
        follow_redirects=False,
    )
    assert post_box.sent == []


def test_the_mail_layer_being_absent_is_announced_and_stops_nothing(
    admin, monkeypatch
):
    """The parallel-build case, and the screen still does its whole job.

    Without app/notify there is no mail. There is still an account, still a
    link, and still a sentence saying nobody was emailed -- which is the state
    this feature shipped in before the mail layer existed.
    """
    monkeypatch.setattr(users_admin, "NOTIFY_MODULE", "app.notify_that_is_not_here")
    body = _provision(admin).text

    assert users_admin.NO_MAIL_LAYER in body
    assert _link(body)
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED


@needs_mail
def test_a_mail_layer_missing_a_function_is_named_rather_than_raised(
    admin, monkeypatch
):
    """A contract that moved is a sentence on the page, not a 500."""
    monkeypatch.delattr(notify, users_admin.MAIL_FN_RESENT, raising=False)
    body = _provision(admin).text

    assert users_admin.MAIL_FN_RESENT in body
    assert _link(body)


# ------------------------------------------------------------- the resend


def test_a_resend_kills_the_previous_link_the_instant_it_issues_a_new_one(
    admin, visitor
):
    """THE REQUIREMENT. "After that they need a fresh invite" is this test.

    Not a column read. The first link is taken, resend is pressed, and the first
    link is tried again -- which is what the person holding a leaked copy would
    do. It must be dead at that instant rather than at its own expiry, or every
    press of the button buys every copy of the old link another day.
    """
    first = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)

    fresh = admin.post(users_admin.resend_url(invitation_id), follow_redirects=False)
    assert fresh.status_code == 200
    second = _link(fresh.text)
    assert second != first

    # The old link is dead NOW, on both verbs.
    assert visitor.get(_accept_url(first)).status_code == 404
    refused = visitor.post(
        _accept_url(first),
        data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert refused.status_code == 404
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED

    # The new one works.
    taken = visitor.post(
        _accept_url(second),
        data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert taken.status_code == 200
    assert _user("nina.okafor@mep.example").status == STATUS_ACTIVE


def test_a_resend_never_moves_the_expiry_on_the_standing_row(admin):
    """The row that died keeps the life it actually had. Rewriting it erases it."""
    _provision(admin)
    first_id = _invitation_id_on(admin.get(USERS).text)
    before = _invitation(first_id).expires_at

    admin.post(users_admin.resend_url(first_id), follow_redirects=False)

    after = _invitation(first_id)
    assert after.status == INVITE_SUPERSEDED
    assert after.expires_at == before, "the dead row's expiry was rewritten"


def test_an_expired_invitation_is_replaced_rather_than_extended(admin, visitor):
    """"They need a fresh invite" -- and a fresh invite is a new row, not a nudge."""
    first = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    with session_scope() as session:
        session.get(Invitation, invitation_id).expires_at = _now() - timedelta(hours=1)

    assert visitor.get(_accept_url(first)).status_code == 404

    second = _link(
        admin.post(users_admin.resend_url(invitation_id), follow_redirects=False).text
    )
    assert second != first
    assert visitor.get(_accept_url(second)).status_code == 200
    # And the dead one stays dead.
    assert visitor.get(_accept_url(first)).status_code == 404


def test_a_resend_on_an_accepted_invitation_is_refused(admin, visitor):
    """A credential-setting link is not a password reset."""
    token = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    visitor.post(
        _accept_url(token), data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    )

    response = admin.post(users_admin.resend_url(invitation_id), follow_redirects=False)
    assert users_admin.OUTCOME_HEADINGS["INV_ALREADY_ACCEPTED"] in response.text
    assert _invitation(invitation_id).status == INVITE_ACCEPTED


def test_a_handoff_invitation_cannot_be_resent_from_this_screen(admin):
    """Different fix, and the screen prints the write layer's reason for it."""
    with session_scope() as session:
        session.add(
            Invitation(
                id="INV-9001",
                company_id=COMPANY,
                email="handoff@partner.example",
                invited_by_user_id=_user(_email(ROLE_ADMIN)).id,
                invited_at=_now(),
                token_hash="f" * 64,
                expires_at=_now() + timedelta(days=7),
                status="pending",
                kind=INVITE_KIND_HANDOFF,
                subject_type="escalation",
                subject_id="ESC-CLM-MISQUOTE",
            )
        )
    response = admin.post(users_admin.resend_url("INV-9001"), follow_redirects=False)
    assert users_admin.OUTCOME_HEADINGS["INV_NOT_PROVISION"] in response.text


def test_an_unknown_invitation_id_is_refused_without_saying_whose_it_is(admin):
    response = admin.post(users_admin.resend_url("INV-9999"), follow_redirects=False)
    assert users_admin.OUTCOME_HEADINGS["INV_UNKNOWN"] in response.text


# ------------------------------------------------------------ the accept screen


def test_the_accept_screen_shows_the_password_rule_before_anybody_types(
    admin, visitor
):
    token = _link(_provision(admin).text)
    body = visitor.get(_accept_url(token)).text
    assert str(MIN_PASSWORD_LENGTH) in body
    assert "<form" in body


def test_the_accept_screen_carries_no_way_into_the_product(admin, visitor):
    """It does not extend base.html, for the reason share.py gives at length."""
    token = _link(_provision(admin).text)
    body = visitor.get(_accept_url(token)).text
    assert '<nav aria-label="Screens">' not in body
    assert "/proceedings" not in body
    assert "/projects" not in body
    assert "/review" not in body
    assert 'name="viewport"' in body


def test_the_accept_screen_sends_the_three_headers_the_share_page_sends(
    admin, visitor
):
    """The token is in the path, so Referer would otherwise carry a live link."""
    token = _link(_provision(admin).text)
    for response in (
        visitor.get(_accept_url(token)),
        visitor.get(_accept_url(UNKNOWN_TOKEN)),
    ):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "noindex" in response.headers["x-robots-tag"]


def test_a_dead_link_and_a_link_that_never_existed_are_the_same_page(admin, visitor):
    """Four causes, one answer, byte for byte. Anything else is a probe."""
    first = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    admin.post(users_admin.resend_url(invitation_id), follow_redirects=False)

    superseded = visitor.get(_accept_url(first))
    unknown = visitor.get(_accept_url(UNKNOWN_TOKEN))
    empty_shaped = visitor.get(_accept_url("short"))

    assert superseded.status_code == unknown.status_code == 404
    assert superseded.text == unknown.text == empty_shaped.text
    assert "nina" not in superseded.text.lower(), "the refusal named the recipient"
    assert "mep.example" not in superseded.text.lower()


def test_a_revoked_invitation_gets_the_same_dead_page(admin, visitor):
    token = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    with session_scope() as session:
        session.get(Invitation, invitation_id).status = "revoked"

    dead = visitor.get(_accept_url(token))
    assert dead.status_code == 404
    assert dead.text == visitor.get(_accept_url(UNKNOWN_TOKEN)).text


def test_setting_a_password_through_the_screen_makes_a_working_login(admin, visitor):
    token = _link(_provision(admin).text)
    done = visitor.post(
        _accept_url(token),
        data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert done.status_code == 200

    person = _user("nina.okafor@mep.example")
    assert person.status == STATUS_ACTIVE

    fresh = TestClient(visitor.app, base_url="https://testserver")
    assert (
        _sign_in(fresh, "nina.okafor@mep.example", NEW_PASSWORD).status_code == 303
    )


def test_a_token_cannot_be_used_twice(admin, visitor):
    token = _link(_provision(admin).text)
    visitor.post(
        _accept_url(token), data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    )
    second = visitor.post(
        _accept_url(token),
        data={"password": "another-good-passphrase", "confirm": "another-good-passphrase"},
        follow_redirects=False,
    )
    assert second.status_code == 404


def test_a_password_the_product_refuses_leaves_the_link_working(admin, visitor):
    """A short password is not a dead invitation and must not be described as one."""
    token = _link(_provision(admin).text)
    short = visitor.post(
        _accept_url(token), data={"password": "short", "confirm": "short"}
    )
    assert short.status_code == 200
    assert str(MIN_PASSWORD_LENGTH) in short.text
    assert invite_accept.ACCEPT_UNAVAILABLE not in short.text
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED

    good = visitor.post(
        _accept_url(token),
        data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert good.status_code == 200
    assert _user("nina.okafor@mep.example").status == STATUS_ACTIVE


def test_two_boxes_that_disagree_are_caught_before_anything_is_written(admin, visitor):
    token = _link(_provision(admin).text)
    response = visitor.post(
        _accept_url(token),
        data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD + "x"},
    )
    assert response.status_code == 200
    assert invite_accept.PASSWORDS_DIFFER in response.text
    assert _user("nina.okafor@mep.example").status == STATUS_INVITED


def test_the_accept_screen_never_names_the_address_it_was_for(admin, visitor):
    """Confirming an address has an account here is a fact for the account holder."""
    token = _link(_provision(admin).text)
    for body in (
        visitor.get(_accept_url(token)).text,
        visitor.post(
            _accept_url(token),
            data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
        ).text,
    ):
        assert "nina.okafor@mep.example" not in body


# ------------------------------------------------------------- one tenant


def test_another_companys_accounts_are_not_on_this_page(admin):
    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email="somebody@rival.example",
            display_name="A rival person",
            password="a-rival-password-here",
            actor="system:test",
        )
    body = admin.get(USERS).text
    assert "somebody@rival.example" not in body
    assert "A rival person" not in body


def test_another_companys_invitation_cannot_be_resent_through_this_screen(admin):
    with session_scope() as session:
        ensure_system_roles(session)
        rival = create_user(
            session,
            RIVAL,
            email="rival.admin@rival.example",
            display_name="Rival admin",
            password="a-rival-password-here",
            actor="system:test",
        )
        session.add(
            Invitation(
                id="INV-8001",
                company_id=RIVAL,
                email="target@rival.example",
                invited_by_user_id=rival.id,
                invited_at=_now(),
                token_hash="e" * 64,
                expires_at=_now() + timedelta(hours=INVITE_PROVISION_TTL_HOURS),
                status="pending",
                kind=INVITE_KIND_PROVISION,
            )
        )

    response = admin.post(users_admin.resend_url("INV-8001"), follow_redirects=False)
    assert users_admin.OUTCOME_HEADINGS["INV_UNKNOWN"] in response.text
    assert "target@rival.example" not in response.text
    assert _invitation("INV-8001").status == "pending"


# --------------------------------------------------- the control that bent


def test_a_handoff_with_no_item_is_refused_by_the_rule_that_replaced_the_not_nulls():
    """The two NOT NULLs became a kind discriminator, not a hole.

    A provision invitation may name no item. A handoff still must, and this is
    the assertion that the control narrowed rather than went away.
    """
    require_justification(
        kind=INVITE_KIND_PROVISION, subject_type=None, subject_id=None
    )
    with pytest.raises(ValueError):
        require_justification(
            kind=INVITE_KIND_HANDOFF, subject_type=None, subject_id=None
        )
    with pytest.raises(ValueError):
        require_justification(
            kind=INVITE_KIND_HANDOFF, subject_type="escalation", subject_id=""
        )
    with pytest.raises(ValueError):
        require_justification(kind="something-else", subject_type=None, subject_id=None)


# ------------------------------------------------------------ the chain holds


def test_the_audit_chain_still_verifies_after_all_of_it(admin, visitor):
    token = _link(_provision(admin).text)
    invitation_id = _invitation_id_on(admin.get(USERS).text)
    admin.post(users_admin.resend_url(invitation_id), follow_redirects=False)
    visitor.get(_accept_url(token))
    _provision(admin, email="", display_name="Nobody")

    with session_scope() as session:
        # Raises on the first break; True is the whole answer.
        assert verify_chain(session, COMPANY)


# --------------------------------------------------------------- responsive


def test_neither_template_hardcodes_a_width_wider_than_a_phone():
    """tests/test_responsive.py scans inline attributes and the stylesheet.

    These two templates carry rules in their own head, which neither of that
    file's scans reaches, so the same rule is asserted here rather than by
    nobody.
    """
    offenders = []
    for name in users_admin.TEMPLATES_OWNED + invite_accept.TEMPLATES_OWNED:
        text = (users_admin.TEMPLATE_DIR / name).read_text()
        for width in re.findall(r"(?:min-)?width:\s*([0-9]{3,})px", text):
            if int(width) >= 400:
                offenders.append(f"{name}: {width}px")
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_the_user_list_is_a_plain_table_the_stylesheet_can_scroll(admin):
    """The narrow-screen rule in strata.css targets `table`, so ours must be one.

    A grid of divs pretending to be a table opts out of the fix silently, and
    this screen is the widest table in the product.
    """
    body = admin.get(USERS).text
    assert "<table" in body
    assert 'name="viewport"' in body


# ------------------------------------------ the wiring nobody in this branch owns


@pytest.mark.xfail(
    reason=(
        "app/web/deps.py::PUBLIC_PATHS lists exact paths only, so the assembled "
        "application sends an anonymous request for an acceptance link to "
        "/login and nobody can set a password. The change is the same two lines "
        "/s/<token> is waiting for; see the handoff at the foot of "
        "app/web/views/invite_accept.py. This test flips to XPASS the moment it "
        "lands."
    ),
    strict=False,
)
def test_the_acceptance_path_is_reachable_without_a_session():
    assert deps.is_public_path(invite_accept.accept_url("c" * 43))
