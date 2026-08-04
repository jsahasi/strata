"""The share and invitation registry: the screen a buyer's security team reads.

WHY THESE TESTS ARE SHAPED LIKE THIS. Enterprise security teams block products
that share invisibly and permit products that share visibly. The registry is the
control that makes the second sentence true, so the questions below are the ones
a reviewer asks in the room, in the order they ask them.

1. **Does the gate hold?** Both screens and all three write paths sit behind
   user.manage. An analyst -- who may read everything the product shows and may
   act on nothing that costs money -- is refused on every one of them, and the
   refusal lands in the audit chain. A hidden button is not a control.

2. **What is shared right now, and can it be stopped?** Live before expired
   before revoked, an open count that comes from the opens rather than from the
   counter beside them, and revocation in one click, singly and in bulk.

3. **Did it still verify when they opened it?** The strongest evidence this
   product does what it claims. A link opened three times where the third open
   showed a refusal is a real compliance event, and the row for that open is
   rendered in the alarm treatment the app already uses for withheld.

4. **Is it one tenant?** Another company's link is not on the page and cannot be
   revoked through it, which matters more here than anywhere: a share link is
   readable with no account at all.

5. **Does the screen admit what it cannot do?** Sharing has no per-company
   switch in this build, and approving an invitation does not yet create the
   account the invited person needs. Both are stated on the screen rather than
   left for a reviewer to discover. A registry that overstates its coverage is
   worse than none, because it is believed.

WHAT THESE TESTS DO NOT DO. They do not lay out a page, so they cannot prove the
registry is readable on a handset -- tests/test_responsive.py guards the classes
of defect that made it unreadable before, and the last test here holds these two
templates to the same rules. They do not exercise the share OPEN path: nothing
here mints a token or renders /s/<token>, which is another module's work. They
write share_opens rows directly, which is the seam the registry reads.

Offline, no API key, no network. The application is assembled here rather than
imported from app/main.py, following tests/test_screens.py, so the registry can
be tested before anything wires it in. https://testserver, because the session
cookie is marked Secure and a client on http drops it in silence.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import user_by_email
from app.state.invites import invitations_for_company as real_invitations_for_company
from app.state.routing import ensure_obligation, map_change_to_obligation
from app.state.sharing import sharing_enabled
from app.state.models import (
    INVITE_ACCEPTED,
    INVITE_AWAITING_APPROVAL,
    INVITE_KIND_HANDOFF,
    INVITE_KIND_PROVISION,
    INVITE_PENDING,
    INVITE_REVOKED,
    ROLE_ADMIN,
    ROLE_ANALYST,
    SHARE_SUBJECT_CHANGE,
    SHARE_SUBJECT_CLAIM,
    AuditEvent,
    Invitation,
    ShareLink,
    ShareOpen,
)
from app.web import deps
from app.web.deps import install_auth
from app.web.views import auth as auth_view
from app.web.views import shares_admin as registry

COMPANY = "MEP"
RIVAL = "RIVAL"

# Real subjects from the seeded corpus, so a share points at something that
# resolves. The registry links to what was shared, and a test built on invented
# ids would never notice that link breaking.
CHANGE = "CHG-v1-v2-003"
CLAIM = "CLM-CHG-2"

SHARES = registry.SHARES_URL
INVITES = registry.INVITES_URL
REVOKE = registry.REVOKE_URL


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


def _email(role: str) -> str:
    """The seeded account holding a role, read from the corpus rather than typed."""
    return next(
        account.email for account in demo_account_list() if account.role == role
    )


@pytest.fixture
def anonymous() -> TestClient:
    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(registry.router)
    # The same guard the product installs. A registry tested without it would
    # prove nothing about the application a reviewer starts.
    install_auth(app)

    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    return TestClient(app, base_url="https://testserver")


def _sign_in(client: TestClient, email: str):
    return client.post(
        "/login",
        data={"email": email, "password": DEMO_PASSWORD},
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


def _user_id(email: str, company_id: str = COMPANY) -> str:
    with session_scope() as session:
        user = user_by_email(session, company_id, email)
        assert user is not None, f"{email} has no account in {company_id}"
        return user.id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _share(
    share_id: str,
    *,
    company_id: str = COMPANY,
    subject_type: str = SHARE_SUBJECT_CLAIM,
    subject_id: str = CLAIM,
    created_by: str | None = None,
    expires_in_days: float = 7,
    revoked: bool = False,
    open_count: int = 0,
    last_opened_at: datetime | None = None,
) -> str:
    """One share link, written straight to the row the registry reads."""
    creator = created_by or _user_id(_email(ROLE_ANALYST), company_id)
    with session_scope() as session:
        session.add(
            ShareLink(
                id=share_id,
                company_id=company_id,
                # Not a token. Nothing in this suite mints one, and the registry
                # must never render this column either way.
                token_hash=f"{'a' * 40}{share_id}",
                subject_type=subject_type,
                subject_id=subject_id,
                created_by_user_id=creator,
                created_at=_now() - timedelta(days=1),
                expires_at=_now() + timedelta(days=expires_in_days),
                revoked_at=_now() if revoked else None,
                revoked_by_user_id=creator if revoked else None,
                open_count=open_count,
                last_opened_at=last_opened_at,
            )
        )
    return share_id


def _open(share_id: str, *, verified: bool, ip: str = "203.0.113.9", minutes: int = 0):
    with session_scope() as session:
        session.add(
            ShareOpen(
                share_id=share_id,
                opened_at=_now() - timedelta(minutes=minutes),
                ip=ip,
                verified=verified,
            )
        )


def _invitation(
    invitation_id: str,
    *,
    company_id: str = COMPANY,
    email: str = "priya.nandakumar@partner.example",
    status: str = INVITE_AWAITING_APPROVAL,
    subject_id: str | None = "ESC-CLM-MISQUOTE",
    invited_user_id: str | None = None,
    invited_by: str | None = None,
    kind: str = INVITE_KIND_HANDOFF,
) -> str:
    asker = invited_by or (
        _user_id(_email(ROLE_ANALYST)) if company_id == COMPANY else "usr-rival"
    )
    with session_scope() as session:
        session.add(
            Invitation(
                id=invitation_id,
                company_id=company_id,
                email=email,
                invited_by_user_id=asker,
                invited_at=_now() - timedelta(days=2),
                token_hash=f"{'b' * 40}{invitation_id}",
                expires_at=_now() + timedelta(days=5),
                accepted_at=None,
                status=status,
                approved_by_user_id=None,
                kind=kind,
                # A provision invitation names no item, and the schema's check
                # constraint refuses a handoff that names none.
                subject_type="escalation" if subject_id else None,
                subject_id=subject_id,
                invited_user_id=invited_user_id,
            )
        )
    return invitation_id


def _row(share_id: str) -> ShareLink:
    with session_scope() as session:
        row = session.get(ShareLink, share_id)
        assert row is not None
        session.expunge(row)
        return row


def _invite_row(invitation_id: str) -> Invitation:
    with session_scope() as session:
        row = session.get(Invitation, invitation_id)
        assert row is not None
        session.expunge(row)
        return row


def _events(action: str, company_id: str = COMPANY) -> list[AuditEvent]:
    with session_scope() as session:
        rows = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == company_id)
            .filter(AuditEvent.action == action)
            .order_by(AuditEvent.seq)
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _counts(response) -> dict[str, str]:
    """The outcome the revoke endpoint reports back on its redirect."""
    location = response.headers["location"]
    return dict(re.findall(r"[?&]([a-z_]+)=([^&]*)", location))


def _page(response) -> str:
    """The body with its `<style>` blocks removed.

    Both templates define their own rules in the head, so every class name they
    use appears in the page whether or not a row carries it. Counting
    `share-open--refused` over the whole body would count the rule that paints
    it, and asserting a class is ABSENT would fail on every page. Stripping the
    block is what makes both assertions mean what they say.
    """
    return re.sub(r"<style>.*?</style>", "", response.text, flags=re.DOTALL)


# ------------------------------------------------------------------ the gate


def test_both_screens_refuse_somebody_without_user_manage(analyst):
    """A hidden link is not a control, and neither is an empty page.

    The refusal names the permission, because "you cannot open this" with no
    noun leaves the reader nothing to ask their administrator for.
    """
    for url in (SHARES, INVITES):
        response = analyst.get(url)
        assert response.status_code == 403, url
        assert "user.manage" in response.text


def test_the_refusal_says_nothing_about_what_is_on_the_screen(analyst):
    """A refusal that leaked the count would tell a refused reader how much."""
    share_id = _share("SHL-0001")
    body = analyst.get(SHARES).text
    assert share_id not in body
    assert CLAIM not in body


def test_a_reader_who_may_not_administer_people_may_not_revoke(analyst):
    """The pricing boundary, applied to the one screen that could break it.

    Reading is free and acting is paid. Every write path here is an act, so the
    same refusal covers all three, and the row is untouched afterwards.
    """
    share_id = _share("SHL-0001")
    invitation_id = _invitation("INV-0001")

    assert analyst.post(REVOKE, data={"only": share_id}).status_code == 403
    assert analyst.post(f"{INVITES}/{invitation_id}/approve").status_code == 403
    assert analyst.post(f"{INVITES}/{invitation_id}/reject").status_code == 403

    assert _row(share_id).revoked_at is None
    assert _invite_row(invitation_id).status == INVITE_AWAITING_APPROVAL


def test_every_refusal_is_written_into_the_audit_chain(analyst):
    """A denial nobody recorded is a denial nobody can review."""
    _share("SHL-0001")
    analyst.get(SHARES)

    denials = _events(ACTION_ACCESS_DENIED)
    assert denials, "the refusal wrote nothing to the chain"
    assert any(row.subject_id == "user.manage" for row in denials)
    with session_scope() as session:
        assert verify_chain(session, COMPANY)


# ------------------------------------------------------- what is shared now


def test_live_shares_come_first_then_expired_then_revoked(admin):
    """Live is the answer to "what is out there"; the rest is history.

    Order is asserted against the rendered page rather than against a helper,
    because the ordering is the product decision and a helper could be right
    while the template ignored it.
    """
    live = _share("SHL-0001", expires_in_days=5)
    expired = _share("SHL-0002", expires_in_days=-1)
    revoked = _share("SHL-0003", revoked=True)

    body = admin.get(SHARES).text
    assert body.index(live) < body.index(expired) < body.index(revoked)


def test_a_revoked_link_that_also_expired_reads_as_revoked(admin):
    """Revocation is a decision somebody took. Expiry is a clock running out.

    Both are dead, and the one worth showing is the one a person did.
    """
    share_id = _share("SHL-0001", expires_in_days=-3, revoked=True)
    body = admin.get(SHARES).text
    section = body.split(share_id)[0]
    assert section.rfind("Revoked") > section.rfind("Expired")


def test_a_link_nobody_opened_says_so_rather_than_showing_a_date(admin):
    """Absence stays absence. Never the creation time dressed as an open."""
    _share("SHL-0001")
    body = admin.get(SHARES).text
    assert "Not opened" in body


def test_the_registry_never_renders_the_token_hash(admin):
    """Reading the whole registry must not hand anybody anything that opens a link.

    The hash is not the token and would not open anything, which is exactly why
    printing it is all cost and no use: it is a secret-shaped string in a screen
    people screenshot for their security review.
    """
    share_id = _share("SHL-0001")
    body = admin.get(SHARES).text
    assert _row(share_id).token_hash not in body
    assert "token" not in body.lower() or "token_hash" not in body


def test_the_screen_names_who_shared_it_and_what_was_shared(admin):
    """"Who shared what" is the first question in the room, and it is one row."""
    share_id = _share("SHL-0001", subject_type=SHARE_SUBJECT_CHANGE, subject_id=CHANGE)
    body = admin.get(SHARES).text
    assert share_id in body
    assert CHANGE in body
    assert _email(ROLE_ANALYST) in body


def test_a_share_pointing_at_a_row_this_company_does_not_have_says_so(admin):
    """Absence is denial, applied to the thing the link claims to show."""
    _share("SHL-0001", subject_id="CLM-NOT-A-CLAIM")
    body = admin.get(SHARES).text
    assert "CLM-NOT-A-CLAIM" in body
    assert "could not find that row in this company" in body
    # And says the honest second half: the link is still a live door.
    assert "still" in body and "opens for whoever holds it" in body


# --------------------------------------------- did it verify when they opened


def test_each_open_shows_whether_verification_passed(admin):
    """The one thing a reviewer actually wants, and the reason the table exists.

    Three opens, the third of which showed the refusal. All three are on the
    page with their own verdict, and the failure carries the alarm treatment
    rather than sitting in a list of identical grey rows.
    """
    share_id = _share("SHL-0001", open_count=3, last_opened_at=_now())
    _open(share_id, verified=True, minutes=90, ip="203.0.113.1")
    _open(share_id, verified=True, minutes=60, ip="203.0.113.2")
    _open(share_id, verified=False, minutes=5, ip="198.51.100.7")

    body = _page(admin.get(SHARES))
    assert body.count("share-open--refused") == 1, (
        "exactly one open refused, and the page must mark exactly that one"
    )
    assert "198.51.100.7" in body
    assert "203.0.113.1" in body
    # The word, not only the colour. Meaning never rests on colour alone.
    assert "Refused" in body
    assert "Verified" in body


def test_a_link_whose_last_open_refused_is_flagged_on_its_own_row(admin):
    """A compliance officer scanning the register must not have to open a row."""
    share_id = _share("SHL-0001", open_count=1, last_opened_at=_now())
    _open(share_id, verified=False)

    body = _page(admin.get(SHARES))
    assert "share-row--refused" in body
    assert "refused" in body.split(share_id)[0].lower()


def test_the_open_count_comes_from_the_rows_and_a_disagreement_is_reported(admin):
    """share_opens is the record; open_count is a convenience that can be wrong.

    app/state/models.py says a reader that finds the two disagreeing has found a
    defect in the writer and must believe the rows. The registry does that, and
    says so on the page rather than quietly showing the larger number.
    """
    share_id = _share("SHL-0001", open_count=0)
    _open(share_id, verified=True)
    _open(share_id, verified=True)

    body = _page(admin.get(SHARES))
    assert "2 open(s)" in body, "the page must show the number the rows hold"
    assert "counter" in body.lower(), "and must say the stored one disagrees"
    assert "share_opens holds 2" in body


# ------------------------------------------------------------------ revoking


def test_revoking_one_link_is_immediate_and_audited(admin):
    """One click, one row, one entry in the chain naming the link."""
    share_id = _share("SHL-0001")
    response = admin.post(REVOKE, data={"only": share_id}, follow_redirects=False)
    assert response.status_code == 303

    row = _row(share_id)
    assert row.revoked_at is not None
    assert row.revoked_by_user_id == _user_id(_email(ROLE_ADMIN))

    written = _events(registry.ACTION_SHARE_REVOKED)
    assert [event.subject_id for event in written] == [share_id]
    assert written[0].actor_user_id == _user_id(_email(ROLE_ADMIN))
    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_revoking_in_bulk_revokes_every_link_named_and_audits_each_one(admin):
    """One click for a list, but the chain still answers per link.

    One row per click would leave "when was THIS link stopped" unanswerable
    without parsing prose out of a reason field.
    """
    first = _share("SHL-0001")
    second = _share("SHL-0002")
    untouched = _share("SHL-0003")

    response = admin.post(
        REVOKE, data={"share_ids": [first, second]}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _counts(response)["revoked"] == "2"

    assert _row(first).revoked_at is not None
    assert _row(second).revoked_at is not None
    assert _row(untouched).revoked_at is None
    assert {e.subject_id for e in _events(registry.ACTION_SHARE_REVOKED)} == {
        first,
        second,
    }


def test_revoking_a_link_that_is_already_revoked_writes_nothing(admin):
    """Re-stating a fact is not a decision, and a chain of them is unreadable.

    set_user_status takes the same line for the same reason.
    """
    share_id = _share("SHL-0001", revoked=True)
    was = _row(share_id).revoked_at

    response = admin.post(REVOKE, data={"only": share_id}, follow_redirects=False)
    assert _counts(response)["already"] == "1"
    assert _row(share_id).revoked_at == was
    assert _events(registry.ACTION_SHARE_REVOKED) == []


def test_an_expired_link_can_still_be_revoked(admin):
    """An admin who decides a link should never have existed gets that on record.

    Expiry is a clock running out; revocation is a person deciding. The second
    is worth recording even after the first has happened.
    """
    share_id = _share("SHL-0001", expires_in_days=-2)
    admin.post(REVOKE, data={"only": share_id}, follow_redirects=False)

    assert _row(share_id).revoked_at is not None
    assert [e.subject_id for e in _events(registry.ACTION_SHARE_REVOKED)] == [share_id]


def test_the_screen_does_not_revoke_anything_itself(admin):
    """One rule for who may stop a link, and this screen is not a second copy.

    The sharer's own revoke button and this register call the same function in
    app/state/sharing.py. A bulk revoke that wrote the rows here would be a
    second answer to who may stop a link and to what the log says when they do.
    """
    calls = []
    share_id = _share("SHL-0001")

    real = registry.revoke_share_link

    def _watch(session, **kwargs):
        calls.append(kwargs)
        return real(session, **kwargs)

    registry.revoke_share_link = _watch
    try:
        admin.post(REVOKE, data={"only": share_id}, follow_redirects=False)
    finally:
        registry.revoke_share_link = real

    assert [call["share_id"] for call in calls] == [share_id]
    assert calls[0]["company_id"] == COMPANY
    assert calls[0]["user_id"] == _user_id(_email(ROLE_ADMIN))
    assert _row(share_id).revoked_at is not None


def test_a_revoke_naming_nothing_at_all_changes_nothing_and_says_so(admin):
    """An empty post is a client bug, not an instruction to revoke everything."""
    share_id = _share("SHL-0001")
    response = admin.post(REVOKE, data={}, follow_redirects=False)

    assert _counts(response)["revoked"] == "0"
    assert _row(share_id).revoked_at is None


# ------------------------------------------------------------------ one tenant


def test_another_company_s_link_is_not_on_the_page(admin):
    """A share link opens with no account at all, so this matters most here."""
    _share("SHL-0001")
    theirs = _share("SHL-9001", company_id=RIVAL, created_by="usr-rival")

    body = admin.get(SHARES).text
    assert "SHL-0001" in body
    assert theirs not in body


def test_another_company_s_link_cannot_be_revoked_through_this_screen(admin):
    """Not refused with a different message either: it resolves to nothing here."""
    theirs = _share("SHL-9001", company_id=RIVAL, created_by="usr-rival")

    response = admin.post(REVOKE, data={"only": theirs}, follow_redirects=False)
    assert _counts(response)["unknown"] == "1"
    assert _row(theirs).revoked_at is None
    assert _events(registry.ACTION_SHARE_REVOKED, RIVAL) == []


def test_another_company_s_invitation_is_not_in_the_queue(admin):
    theirs = _invitation("INV-9001", company_id=RIVAL)
    body = admin.get(INVITES).text
    assert theirs not in body

    response = admin.post(f"{INVITES}/{theirs}/approve", follow_redirects=False)
    assert response.status_code == 404
    assert _invite_row(theirs).status == INVITE_AWAITING_APPROVAL


# --------------------------------------------------------------- invitations


def test_the_queue_shows_who_asked_and_what_they_asked_for(admin):
    """An admin approving an account must read the work that justified it."""
    _invitation("INV-0001")
    body = admin.get(INVITES).text

    assert "INV-0001" in body
    assert "priya.nandakumar@partner.example" in body
    assert _email(ROLE_ANALYST) in body
    assert "ESC-CLM-MISQUOTE" in body


def test_an_invitation_with_no_item_says_what_justified_it_instead(admin):
    """Every invitation is justified; the kind says by what.

    A provision invitation is a login an administrator added, so it names no
    item. The cell must not be blank: an empty column on half the rows reads as
    a gap in the record rather than as the other half of that rule.
    """
    _invitation(
        "INV-0001",
        status=INVITE_PENDING,
        kind=INVITE_KIND_PROVISION,
        subject_id=None,
    )
    body = admin.get(INVITES).text
    assert INVITE_KIND_PROVISION in body
    assert "a login added by" in body
    assert _email(ROLE_ANALYST) in body


def test_the_queue_separates_the_four_states(admin):
    _invitation("INV-0001", status=INVITE_AWAITING_APPROVAL)
    _invitation("INV-0002", status=INVITE_PENDING, email="colleague@meridian.example")
    _invitation("INV-0003", status=INVITE_ACCEPTED, email="already@meridian.example")

    body = admin.get(INVITES).text
    for invitation_id in ("INV-0001", "INV-0002", "INV-0003"):
        assert invitation_id in body
    assert body.index("INV-0001") < body.index("INV-0002")


def _no_write_layer(monkeypatch, why: str = "not in this build"):
    """Stand the invite write layer down, however this build happens to be wired.

    The module landed while this screen was being written. The fallback still
    has to be tested: half of what these two screens are for is saying plainly
    when they cannot do something, and a path only exercised by an accident of
    build order is a path nobody has checked.
    """
    monkeypatch.setattr(
        registry,
        "invite_writer",
        lambda name: (None, f"{registry.INVITES_MODULE} is {why}."),
        raising=True,
    )


def _stub_decision(monkeypatch, decide):
    """Put a stand-in behind the two decision functions, and only those.

    The queue's list read comes from the same module through the same seam, so
    a stub answering to every name would also replace the read and prove
    nothing about the decision. The real reader stays.
    """
    monkeypatch.setattr(
        registry,
        "invite_writer",
        lambda name: (
            (real_invitations_for_company, "")
            if name == registry.LIST_FN
            else (decide, "")
        ),
        raising=True,
    )


def test_a_decision_with_no_invite_layer_changes_nothing_and_names_what_is_missing(
    admin, monkeypatch
):
    """A fallback must announce itself, and this is the one that matters most.

    Approving is not a status change. It creates the account the invited person
    needs, and that rule lives in app/state/invites.py beside the same-domain
    path that writes the same account. With that module unreachable the tempting
    thing is to flip the status here so the button looks like it worked, leaving
    an invitation marked approved with nobody behind it and an item routed
    nowhere. The screen answers 501 and names the module instead.
    """
    _no_write_layer(monkeypatch)
    invitation_id = _invitation("INV-0001")

    for verb in ("approve", "reject"):
        response = admin.post(f"{INVITES}/{invitation_id}/{verb}")
        assert response.status_code == 501, verb
        assert registry.INVITES_MODULE in response.text
        assert _invite_row(invitation_id).status == INVITE_AWAITING_APPROVAL

    # The list comes from the same module, so with it unreachable there is
    # nothing to show -- and the page must not fill that silence with the
    # empty state, which says something else and says it falsely.
    assert "Nobody has been invited" not in response.text


def test_the_queue_says_it_cannot_be_decided_before_anybody_presses_a_button(
    admin, monkeypatch
):
    """A control that cannot work must say so on the way in, not on the way out."""
    _no_write_layer(monkeypatch)
    _invitation("INV-0001")
    body = admin.get(INVITES).text
    assert registry.INVITES_MODULE in body
    assert "cannot be decided" in body


def test_the_queue_in_this_build_can_be_decided(admin):
    """Not vacuous: the seam above resolves against the module that shipped.

    Without this, every assertion about the refusal path would still pass on a
    build where the write layer had been renamed and nobody noticed.
    """
    _invitation("INV-0001")
    body = admin.get(INVITES).text
    assert "cannot be decided" not in body

    writer, blocked = registry.invite_writer(registry.APPROVE_FN)
    assert writer is not None and blocked == ""


def test_a_decision_is_handed_to_the_invite_layer_whole(admin, monkeypatch):
    """The screen calls that module; it does not reimplement approval.

    A stub stands in for app/state/invites.py and records the call. What is
    asserted is the call shape tests/test_invites.py fixes -- the session, the
    company, the invitation, the approver's ACCOUNT ID rather than their name --
    and that the screen reports the row rather than what the stub returned. A
    screen that believed the return value could say a decision landed while the
    row disagreed.
    """
    invitation_id = _invitation("INV-0001")
    seen = {}

    class _Outcome:
        reason_code = "invite.ok"

    def _approve(session, company_id, **kwargs):
        seen.update({"company_id": company_id, **kwargs})
        row = session.get(Invitation, kwargs["invitation_id"])
        row.status = "approved"
        row.approved_by_user_id = kwargs["approver_user_id"]
        return _Outcome()

    _stub_decision(monkeypatch, _approve)

    response = admin.post(f"{INVITES}/{invitation_id}/approve", follow_redirects=False)
    assert response.status_code == 303
    assert _counts(response) == {"decided": "yes", "code": "invite.ok"}

    assert seen["company_id"] == COMPANY
    assert seen["invitation_id"] == invitation_id
    assert seen["approver_user_id"] == _user_id(_email(ROLE_ADMIN))
    assert seen["actor"] == _email(ROLE_ADMIN)
    assert seen["now"].tzinfo is not None

    assert _invite_row(invitation_id).status == "approved"


def test_a_write_layer_that_says_yes_while_the_row_says_no_is_believed_by_the_row(
    admin, monkeypatch
):
    """The return value is a claim; the row is the fact.

    A screen that trusted the outcome object would report a decision that
    landed while the invitation sat exactly where it was. This one re-reads.
    """
    invitation_id = _invitation("INV-0001")

    class _Outcome:
        reason_code = "invite.ok"
        reason_text = "this module says it worked and changed nothing"

    _stub_decision(monkeypatch, lambda *args, **kwargs: _Outcome())

    response = admin.post(f"{INVITES}/{invitation_id}/approve", follow_redirects=False)
    assert response.status_code == 200, "nothing moved, so nothing is reported as moved"
    assert "Nothing was changed" in response.text
    assert "invite.ok" in response.text
    assert _invite_row(invitation_id).status == INVITE_AWAITING_APPROVAL


def test_a_write_layer_that_does_not_accept_this_call_is_reported_not_raised(
    admin, monkeypatch
):
    """Two sides of a contract drifting apart is a sentence, not a 500.

    A stack trace on an admin screen says nothing about which side moved. This
    names the function and the mismatch, changes nothing, and still fails --
    loudly and checkably.
    """
    invitation_id = _invitation("INV-0001")

    def _wrong_shape(session, company_id, invitation_id):
        raise AssertionError("must not be reached")

    _stub_decision(monkeypatch, _wrong_shape)

    response = admin.post(f"{INVITES}/{invitation_id}/approve")
    assert response.status_code == 501
    assert registry.APPROVE_FN in response.text
    assert _invite_row(invitation_id).status == INVITE_AWAITING_APPROVAL


def _routing_gap():
    """The dead end an invitation exists to close: a duty with nobody to do it.

    An obligation with no owner account, mapped to the change the escalation
    hangs off. Without it the invite layer refuses -- correctly -- because an
    invitation whose item is refused for some other reason would create an
    account nothing is waiting for. Built through that module's own functions
    rather than by writing rows, so this test cannot pass on a shape the
    product does not produce.
    """
    with session_scope() as session:
        ensure_obligation(
            session,
            COMPANY,
            obligation_id="OBL-REGISTRY-1",
            title="Post security for new large-load interconnection work.",
            owner_user_id=None,
            actor="system:test",
        )
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=CHANGE,
            obligation_id="OBL-REGISTRY-1",
            mapped_by="system:test",
        )


def test_approving_through_the_real_write_layer_creates_the_account(admin):
    """The whole point of handing the decision over, proved end to end.

    Not a stub. The screen calls app/state/invites.py, that module writes the
    account at STATUS_INVITED, and the flag on the row -- somebody the waiting
    work cannot be routed to -- goes quiet by itself. Nothing in this view knows
    how an account is made, which is why there is only one way to make one.
    """
    _routing_gap()
    invitation_id = _invitation("INV-0001")
    # Queued and waiting on an administrator is not a gap: no account is due
    # yet, and the flag is for the two states that claim one and have none.
    assert "invite-row--unrouted" not in _page(admin.get(INVITES))

    response = admin.post(f"{INVITES}/{invitation_id}/approve", follow_redirects=False)
    assert response.status_code == 303, response.text
    assert _counts(response)["decided"] == "yes"

    row = _invite_row(invitation_id)
    assert row.status == "approved"
    assert row.approved_by_user_id == _user_id(_email(ROLE_ADMIN))
    assert row.invited_user_id, "approval must leave an account to route to"

    body = _page(admin.get(INVITES))
    assert "invite-row--unrouted" not in body
    assert row.email in body

    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_rejecting_through_the_real_write_layer_withdraws_the_invitation(admin):
    """Reject is revoke: the vocabulary has one word for withdrawn-before-accepted."""
    _routing_gap()
    invitation_id = _invitation("INV-0001")
    response = admin.post(f"{INVITES}/{invitation_id}/reject", follow_redirects=False)

    assert response.status_code == 303, response.text
    assert _counts(response)["decided"] == "yes"
    assert _invite_row(invitation_id).status == INVITE_REVOKED
    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_a_refusal_from_the_write_layer_is_rendered_in_its_own_words(admin):
    """This screen does not decide that, and does not paraphrase it either.

    A pending invitation needed nobody's yes. The refusal comes back from the
    module that owns the rule, its sentence is what the administrator reads,
    the row does not move, and the queue is still under it.
    """
    invitation_id = _invitation("INV-0001", status=INVITE_PENDING)
    response = admin.post(f"{INVITES}/{invitation_id}/approve", follow_redirects=False)

    assert response.status_code == 200, "a refusal is rendered, never redirected"
    assert "Nothing was changed" in response.text
    assert "INV_NOT_QUEUED" in response.text
    assert invitation_id in response.text
    assert _invite_row(invitation_id).status == INVITE_PENDING


def test_the_registry_writes_no_invitation_action_code_of_its_own(admin, monkeypatch):
    """One vocabulary for one event. The write layer audits what it performs.

    A code declared here for an act this module hands to another would be a
    second spelling, and a row written under it is invisible to every query for
    the first.
    """
    assert not [name for name in dir(registry) if name.startswith("ACTION_INVITE")]


def test_an_invitation_with_no_account_behind_it_is_flagged(admin):
    """"Waiting on somebody who cannot be reached" must never render as waiting."""
    _invitation("INV-0001", status=INVITE_PENDING, invited_user_id=None)
    assert "invite-row--unrouted" in _page(admin.get(INVITES))


def test_an_invitation_whose_account_exists_names_the_person(admin):
    invited = _user_id(_email(ROLE_ADMIN))
    _invitation("INV-0001", status=INVITE_PENDING, invited_user_id=invited)
    body = _page(admin.get(INVITES))
    assert "invite-row--unrouted" not in body
    assert _email(ROLE_ADMIN) in body


# ------------------------------------------------- what the screen admits to


def test_the_register_shows_the_tenant_switch_and_where_its_answer_came_from(admin):
    """The value alone would be a lie by omission.

    A missing setting is not off and not on, it is nobody having decided.
    sharing_enabled() answers with the value, the source and a sentence, and all
    three reach the page -- so an administrator reading "sharing is on" can tell
    whether somebody decided that or whether it is what the product does when
    nobody has.
    """
    body = admin.get(SHARES).text
    with session_scope() as session:
        switch = sharing_enabled(session, company_id=COMPANY)

    assert ("ON" if switch.value else "OFF") in body
    # Escaped the way the template escapes it. The note has an apostrophe in it,
    # and comparing the raw string would fail for the one reason that is not a
    # defect.
    assert str(escape(switch.note)) in body
    assert switch.source == "default", (
        "this test describes the build where nobody can set the switch; when "
        "company_settings lands it should assert the tenant answer too"
    )
    assert str(escape(registry.SWITCH_SOURCE_WORDS[switch.source])) in body


# --------------------------------------------------------------- responsive


def test_neither_template_hardcodes_a_width_the_stylesheet_cannot_override():
    """The rule tests/test_responsive.py enforces, applied to a `<style>` block.

    That file scans inline style attributes and the stylesheet. These two
    templates carry their own rules in the head, which neither scan reaches, so
    the same rule is asserted here rather than left to nobody.
    """
    offenders = []
    for template in registry.TEMPLATES_OWNED:
        text = (registry.TEMPLATE_DIR / template).read_text()
        for width in re.findall(r"(?:min-)?width:\s*([0-9]{3,})px", text):
            if int(width) >= 400:
                offenders.append(f"{template}: {width}px")
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_both_screens_render_a_plain_table_the_stylesheet_can_scroll(admin):
    """The narrow-screen rule in strata.css targets `table`, so ours must be one.

    A table wrapped in a div with its own overflow, or a grid of divs pretending
    to be a table, opts out of the fix without anybody noticing.
    """
    _share("SHL-0001")
    _invitation("INV-0001")
    for url in (SHARES, INVITES):
        body = admin.get(url).text
        assert "<table" in body, url
        assert 'name="viewport"' in body, url
