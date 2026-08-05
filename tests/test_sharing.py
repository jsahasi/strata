"""The share link, and the one page in this product nobody has to sign in for.

Everything else sits behind the guard in app/web/deps.py. This surface steps
outside it on purpose, so the tests here are written from the attacker's side
rather than the happy path: what can a person holding a link reach, what can a
person holding a stale link learn, and what is in the bytes when the product
refuses.

Four of these are the ones that matter.

**The statement never leaks.** A claim whose citation fails must not put its
statement in the response -- not in the body, not in a header, not in a script
block a template pushed it into. Asserted against the bytes on the wire, because
"it was greyed out" is a property of a stylesheet.

**The claim withdraws itself.** Share a claim that verifies, then edit the
stored source under it, then open the same link again. The page must flip to a
refusal with no job in between. That is the whole reason the page re-verifies at
open time rather than serving a rendered answer, and it is the best thing this
product does.

**Expired, revoked and unknown are one answer.** Same status, same bytes. A
caller who can tell a withdrawn link from one that never existed has been handed
a probe for which links are real.

**The page leads nowhere.** Every link in it points at the sign-in page or at
itself. A share is one artifact, and a reader must not be able to walk from it
into a proceeding, a project or a sibling claim.

The router is mounted on its own here, the way tests/test_change_view.py mounts
the change screen. There is one test at the end about the assembled application,
and it is expected to fail until app/web/deps.py lets the path through: see the
note on it.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from markupsafe import escape

# The module, not the class. tests/test_policy.py reloads app.auth.policy, which
# rebinds every class on it -- so a name bound here at import time would stop
# being the class that policy.require actually raises, and every assertion below
# about a refusal would fail for a reason that has nothing to do with sharing.
from app.auth import policy
from app.seed import load
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role
from app.state.models import (
    ROLE_ADMIN,
    ROLE_ANALYST,
    SHARE_MAX_TTL_DAYS,
    SHARE_SUBJECT_CHANGE,
    SHARE_SUBJECT_CLAIM,
    AuditEvent,
    Claim,
    DocumentVersion,
    ShareLink,
    ShareOpen,
)
from app.state.queries import versions_for_company
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.views import share as share_view

from app.state import sharing

COMPANY = "MEP"
RIVAL = "RIVAL"

# A MOMENT FOR THE MINTS THAT NEVER HAPPEN, AND ONLY FOR THOSE.
#
# Four calls in this file ask create_share_link for a link it must refuse: a
# subject in another company, a stranger, somebody without the read permission,
# and the tenant switch turned off. None of them writes a row. They pass this
# moment so that an omitted clock argument anywhere in this file means somebody
# forgot rather than somebody decided -- see tests/test_clock_pinned.py.
#
# _mint() below does NOT use it, deliberately, and the reason is written there.
T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

# The change carrying both planted claims: one that verifies against v2 and one
# that cites the same offsets with words that are not there.
PAIRED = "CHG-v1-v2-003"
VERIFIED_CLAIM = "CLM-CHG-2"
FAILING_CLAIM = "CLM-MISQUOTE"

# A claim on another change entirely. Used to prove a share of one claim names
# no other claim in the corpus.
OTHER_CLAIM = "CLM-CHG-1"
OTHER_CHANGE = "CHG-v1-v2-004"

# A real change nobody wrote a claim about. Most of the corpus looks like this.
UNCLAIMED = "CHG-v1-v2-000"

DOCKET = "MPUC-2026-0142"

SHARER_EMAIL = "analyst@meridian.example"
SHARER_NAME = "Dana Whitfield"
SHARER_PASSWORD = "share-analyst-password"

OTHER_ANALYST_EMAIL = "second.analyst@meridian.example"
ADMIN_EMAIL = "admin@meridian.example"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """The share router on its own, with no session guard in front of it.

    Deliberately bare. The point of this surface is that it answers a request
    carrying nothing, so a test that wrapped it in the middleware would be
    testing the middleware.
    """
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(share_view.router)
    return app


@pytest.fixture
def corpus():
    """A fresh database, the real corpus, and three accounts to share between."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_system_roles(session)
        sharer = create_user(
            session,
            COMPANY,
            email=SHARER_EMAIL,
            display_name=SHARER_NAME,
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session,
            COMPANY,
            user_id=sharer.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )
        colleague = create_user(
            session,
            COMPANY,
            email=OTHER_ANALYST_EMAIL,
            display_name="A second analyst",
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session,
            COMPANY,
            user_id=colleague.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )
        boss = create_user(
            session,
            COMPANY,
            email=ADMIN_EMAIL,
            display_name="An administrator",
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session,
            COMPANY,
            user_id=boss.id,
            role_name=ROLE_ADMIN,
            actor="system:test",
        )
        return {"sharer": sharer.id, "colleague": colleague.id, "admin": boss.id}


@pytest.fixture
def client(corpus) -> TestClient:
    return TestClient(_build_app())


def _mint(user_id: str, subject_id: str, subject_type: str = SHARE_SUBJECT_CLAIM, **kw):
    """Mint a real link at the real now, and that is a decision, not an oversight.

    THIS IS THE ONE PLACE IN THIS FILE THAT MUST NOT PIN ITS CLOCK, and it is
    listed in ALLOWED in tests/test_clock_pinned.py for exactly this reason.

    Nearly every link minted here is then opened over HTTP --
    `client.get(minted.url)` -- and the share route has no injectable clock. It
    reads the real now, and it will keep reading the real now until somebody
    changes app/web/views/share.py, which no test may do from the outside.

    So the two ends of every one of those tests are: a mint I could pin, and an
    open I cannot. Pinning only the mint would leave a row stamped
    2026-08-04 with a default seven-day life, read by a route that thinks it is
    whatever day it actually is. That passes today, passes for a week, and then
    fails for somebody else on a clean clone with nothing wrong in the product.
    It is not merely the same class of defect as the invitation that fired on
    2026-08-05 -- it is the identical mechanism, a fixed write read by a moving
    clock, and pinning half of it would have introduced it rather than removed
    it.

    The alternative is to leave both ends on the real clock, where they move
    together and the seven days never elapse between two lines of one test.
    That is what this does. `now` is still accepted through **kw, so a test that
    controls both ends -- there are none today -- can pass one.

    The rule this illustrates, and it is worth stating once: pinning is not the
    goal. Agreement between the write and the read is the goal. A pinned clock
    on one side of a pair is worse than no pinning at all.
    """
    with session_scope() as session:
        return sharing.create_share_link(
            session,
            company_id=COMPANY,
            user_id=user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            **kw,
        )


def _statement(claim_id: str) -> str:
    """Read the statement from the corpus rather than typing it here."""
    with session_scope() as session:
        claim = session.get(Claim, claim_id)
        assert claim is not None, f"{claim_id} is not in the seeded corpus"
        return claim.statement


def _html(text: str) -> str:
    return str(escape(text))


def _break_the_source(version_id: str, needle: str, replacement: str) -> None:
    """Edit a stored source under a claim that already cites it."""
    with session_scope() as session:
        version = session.get(DocumentVersion, version_id)
        assert needle in version.source_text
        version.source_text = version.source_text.replace(needle, replacement, 1)


def _opens(share_id: str):
    with session_scope() as session:
        return [
            (row.verified, row.ip)
            for row in session.query(ShareOpen)
            .filter(ShareOpen.share_id == share_id)
            .order_by(ShareOpen.id)
            .all()
        ]


def _actions() -> list[str]:
    with session_scope() as session:
        return [
            row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq)
            .all()
        ]


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_the_token_is_never_written_anywhere(corpus):
    """Read the whole table and you hold hashes, not working links."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    with session_scope() as session:
        rows = session.query(ShareLink).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.token_hash != minted.token
        assert minted.token not in row.token_hash
        # Nothing else on the row carries it either.
        stored = " ".join(
            str(getattr(row, column.name)) for column in ShareLink.__table__.columns
        )
        assert minted.token not in stored

    # Nor does the audit entry that recorded the link being made.
    with session_scope() as session:
        for event in session.query(AuditEvent).all():
            assert minted.token not in f"{event.reason} {event.subject_id} {event.citation}"


def test_the_token_carries_at_least_the_floor_of_randomness(corpus):
    """32 bytes, urlsafe. Anything shorter is a link somebody can walk."""
    first = _mint(corpus["sharer"], VERIFIED_CLAIM)
    second = _mint(corpus["sharer"], VERIFIED_CLAIM)
    assert first.token != second.token
    # base64url of 32 bytes is 43 characters with no padding.
    assert len(first.token) >= 43


# ---------------------------------------------------------------------------
# Minting a link
# ---------------------------------------------------------------------------


def test_a_link_is_audited_when_it_is_made(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        events = (
            session.query(AuditEvent)
            .filter(AuditEvent.action == sharing.ACTION_SHARE_CREATED)
            .all()
        )
        assert len(events) == 1
        assert events[0].subject_id == minted.share_id
        assert verify_chain(session, COMPANY)


def test_a_link_cannot_outlive_the_maximum(corpus):
    with pytest.raises(ValueError) as refused:
        _mint(corpus["sharer"], VERIFIED_CLAIM, ttl_days=SHARE_MAX_TTL_DAYS + 1)
    assert str(SHARE_MAX_TTL_DAYS) in str(refused.value)


def test_a_link_with_no_life_at_all_is_refused(corpus):
    with pytest.raises(ValueError):
        _mint(corpus["sharer"], VERIFIED_CLAIM, ttl_days=0)


def test_the_default_life_is_seven_days(corpus):
    before = datetime.now(timezone.utc)
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    assert timedelta(days=6) < minted.expires_at - before < timedelta(days=8)


def test_a_subject_that_is_not_this_company_s_cannot_be_shared(corpus):
    """The tenant guard, at the point a link is minted rather than at the open.

    The rival analyst holds claim.read in their own company, so the permission
    gate lets them past. What stops them is that MEP's claim id does not exist
    inside RIVAL, and the mint refuses to make an unauthenticated door onto
    nothing.
    """
    with session_scope() as session:
        ensure_system_roles(session)
        rival = create_user(
            session,
            RIVAL,
            email="analyst@rival.example",
            display_name="A rival analyst",
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session, RIVAL, user_id=rival.id, role_name=ROLE_ANALYST,
            actor="system:test",
        )
        rival_id = rival.id

    with session_scope() as session:
        with pytest.raises(ValueError) as refused:
            sharing.create_share_link(
                session,
                company_id=RIVAL,
                user_id=rival_id,
                subject_type=SHARE_SUBJECT_CLAIM,
                subject_id=VERIFIED_CLAIM,
                now=T0,
            )
        assert VERIFIED_CLAIM in str(refused.value)

    with session_scope() as session:
        assert session.query(ShareLink).count() == 0


def test_a_stranger_to_the_company_cannot_share_its_claims(corpus):
    """Authorisation is asked before existence, so nobody can probe for ids."""
    with session_scope() as session:
        with pytest.raises(policy.PermissionDenied):
            sharing.create_share_link(
                session,
                company_id=RIVAL,
                user_id=corpus["sharer"],
                subject_type=SHARE_SUBJECT_CLAIM,
                subject_id=VERIFIED_CLAIM,
                now=T0,
            )


def test_a_scope_wider_than_one_artifact_is_refused(corpus):
    """Never a project, never a proceeding, never a list."""
    for word in ("project", "proceeding", "claims", ""):
        with pytest.raises(ValueError):
            _mint(corpus["sharer"], "PRJ-anything", subject_type=word)


def test_a_subject_that_does_not_exist_is_refused(corpus):
    with pytest.raises(ValueError):
        _mint(corpus["sharer"], "CLM-NOT-A-CLAIM")


def test_somebody_who_may_not_read_the_claim_may_not_share_it(corpus):
    """A share is a read handed onward, so it needs the read permission."""
    with session_scope() as session:
        stranger = create_user(
            session,
            COMPANY,
            email="nobody@meridian.example",
            display_name="No roles at all",
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        stranger_id = stranger.id

    with session_scope() as session:
        with pytest.raises(policy.PermissionDenied):
            sharing.create_share_link(
                session,
                company_id=COMPANY,
                user_id=stranger_id,
                subject_type=SHARE_SUBJECT_CLAIM,
                subject_id=VERIFIED_CLAIM,
                now=T0,
            )


# ---------------------------------------------------------------------------
# The tenant switch, and the fallback that has to announce itself
# ---------------------------------------------------------------------------


def test_sharing_enabled_says_where_its_answer_came_from(corpus):
    with session_scope() as session:
        answer = sharing.sharing_enabled(session, company_id=COMPANY)
    assert answer.value is True
    assert answer.source == sharing.SETTING_SOURCE_DEFAULT
    assert answer.note, "a default that does not announce itself is the failure"
    assert "default" in answer.note.lower()


def test_the_setting_is_asked_at_one_place_and_turning_it_off_closes_the_door(
    corpus, monkeypatch
):
    """Every sharing path asks one function. Stub it and the whole feature stops."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    monkeypatch.setattr(
        sharing,
        "sharing_enabled",
        lambda session, *, company_id: sharing.Setting(
            value=False, source="test", note="switched off for this test"
        ),
    )

    with session_scope() as session:
        with pytest.raises(ValueError):
            sharing.create_share_link(
                session,
                company_id=COMPANY,
                user_id=corpus["sharer"],
                subject_type=SHARE_SUBJECT_CLAIM,
                subject_id=VERIFIED_CLAIM,
                now=T0,
            )
        # An existing link stops opening too. A switch that only stops new
        # links leaves every link already out there working.
        assert sharing.open_share(session, token=minted.token) is None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_the_sharer_may_revoke_their_own_link(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        sharing.revoke_share_link(
            session, company_id=COMPANY, share_id=minted.share_id,
            user_id=corpus["sharer"],
        )
        assert sharing.open_share(session, token=minted.token) is None
    assert sharing.ACTION_SHARE_REVOKED in _actions()


def test_an_admin_may_revoke_somebody_else_s_link(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        sharing.revoke_share_link(
            session, company_id=COMPANY, share_id=minted.share_id,
            user_id=corpus["admin"],
        )
    with session_scope() as session:
        assert sharing.open_share(session, token=minted.token) is None


def test_a_colleague_who_is_neither_may_not(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        with pytest.raises(policy.PermissionDenied):
            sharing.revoke_share_link(
                session, company_id=COMPANY, share_id=minted.share_id,
                user_id=corpus["colleague"],
            )
    with session_scope() as session:
        assert sharing.open_share(session, token=minted.token) is not None


def test_another_company_cannot_revoke_a_link_it_cannot_see(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        with pytest.raises(ValueError):
            sharing.revoke_share_link(
                session, company_id=RIVAL, share_id=minted.share_id,
                user_id=corpus["admin"],
            )


# ---------------------------------------------------------------------------
# The page: what it shows
# ---------------------------------------------------------------------------


def test_the_page_shows_the_claim_and_the_source_at_its_offsets(client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    response = client.get(minted.url)

    assert response.status_code == 200
    body = response.text
    assert _html(_statement(VERIFIED_CLAIM)) in body

    # The marked span is the real bytes from the stored source, not the quote
    # the claim supplied.
    with session_scope() as session:
        source = {v.id: v.source_text for v in versions_for_company(session, COMPANY)}
        claim = session.get(Claim, VERIFIED_CLAIM)
        actual = source[claim.citation_version_id][
            claim.citation_start : claim.citation_end
        ]
    assert _html(actual) in body
    assert f'<mark class="source-mark">{_html(actual)}</mark>' in body


def test_the_watermark_names_the_sharer_the_time_and_the_expiry(client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    body = client.get(minted.url).text

    assert SHARER_NAME in body
    assert "Shared view" in body
    assert str(minted.expires_at.year) in body
    # And it offers the one way in.
    assert 'href="/login"' in body


def test_the_page_is_one_artifact_and_leads_nowhere_else(client, corpus):
    """A reader must not be able to walk from a share into the product."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    body = client.get(minted.url).text

    assert FAILING_CLAIM not in body, "the sibling claim on the same change"
    assert OTHER_CLAIM not in body
    assert OTHER_CHANGE not in body
    assert DOCKET not in body
    assert "PRJ-" not in body

    hrefs = set(re.findall(r'href="([^"]+)"', body))
    allowed = {"/login", "/static/strata.css"}
    walkable = {
        href
        for href in hrefs
        if href not in allowed and not href.startswith("#")
    }
    assert not walkable, f"the page links somewhere else: {sorted(walkable)}"


def test_the_page_carries_no_way_to_act(client, corpus):
    """Reading is free, acting is paid. There is nothing here to act with."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    body = client.get(minted.url).text
    assert "<form" not in body.lower()
    assert "<input" not in body.lower()


def test_the_router_serves_reads_and_nothing_else(corpus):
    """Not one mutating route on the only surface with no session in front of it."""
    for route in share_view.router.routes:
        assert set(route.methods) <= {"GET", "HEAD"}, route.path


# ---------------------------------------------------------------------------
# The page: what it refuses
# ---------------------------------------------------------------------------


def test_a_claim_whose_citation_fails_shows_the_refusal_and_never_the_statement(
    client, corpus
):
    """The rule every surface follows, on the surface where breaking it is worst."""
    minted = _mint(corpus["sharer"], FAILING_CLAIM)
    response = client.get(minted.url)

    assert response.status_code == 200
    statement = _statement(FAILING_CLAIM)

    assert statement not in response.text
    assert _html(statement) not in response.text
    # Not in a header either, and not in a data attribute or script block a
    # template pushed it into.
    assert statement not in str(response.headers)
    # Nor in part. The opening of the sentence is distinctive and is taken from
    # the corpus rather than typed here, so the corpus and the test cannot
    # drift into a passing assertion about a statement nobody stores.
    assert _html(statement[:40]) not in response.text

    # There is no script and no JSON on this page at all, so there is nowhere
    # else a statement could be hiding.
    assert "<script" not in response.text.lower()

    assert "No claim made" in response.text
    assert "Withheld" in response.text


def test_the_refusal_says_what_was_quoted_and_what_the_source_reads(client, corpus):
    """A refusal with no evidence is an apology. This one shows the mismatch."""
    minted = _mint(corpus["sharer"], FAILING_CLAIM)
    body = client.get(minted.url).text

    with session_scope() as session:
        claim = session.get(Claim, FAILING_CLAIM)
        quoted = claim.citation_quote
        source = {v.id: v.source_text for v in versions_for_company(session, COMPANY)}
        actual = source[claim.citation_version_id][
            claim.citation_start : claim.citation_end
        ]

    assert _html(quoted) in body
    assert _html(actual) in body


def test_the_page_sends_the_three_headers_that_keep_the_token_in_one_place(
    client, corpus
):
    """The token is in the path, so a stray Referer is a working link handed on."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    for response in (client.get(minted.url), client.get(sharing.share_url("d" * 43))):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "noindex" in response.headers["x-robots-tag"]


def test_the_claim_withdraws_itself_when_the_source_moves(client, corpus):
    """One link, two opens, and the product changes its mind in between.

    Nothing runs between the two requests. The page re-reads the stored source
    at the cited offsets every time it is opened, so an edit under a citation
    takes the statement off the page on the very next open.
    """
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    statement = _statement(VERIFIED_CLAIM)

    first = client.get(minted.url)
    assert _html(statement) in first.text

    _break_the_source("v2", "20 megawatts (MW)", "25 megawatts (MW)")

    second = client.get(minted.url)
    assert second.status_code == 200
    assert statement not in second.text
    assert _html(statement) not in second.text
    assert statement not in str(second.headers)


# ---------------------------------------------------------------------------
# The dead link: one answer for three questions
# ---------------------------------------------------------------------------


def test_expired_revoked_and_unknown_are_indistinguishable(client, corpus):
    expired = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        row = session.get(ShareLink, expired.share_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    revoked = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        sharing.revoke_share_link(
            session, company_id=COMPANY, share_id=revoked.share_id,
            user_id=corpus["sharer"],
        )

    unknown = sharing.share_url("a" * 43)

    answers = [
        client.get(expired.url),
        client.get(revoked.url),
        client.get(unknown),
    ]
    statuses = {response.status_code for response in answers}
    bodies = {response.text for response in answers}

    assert len(statuses) == 1, f"three different statuses: {statuses}"
    assert len(bodies) == 1, "the bodies differ, so a caller can tell them apart"
    assert answers[0].status_code == 404


def test_a_dead_link_says_nothing_about_the_claim_behind_it(client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        sharing.revoke_share_link(
            session, company_id=COMPANY, share_id=minted.share_id,
            user_id=corpus["sharer"],
        )
    body = client.get(minted.url).text
    assert _statement(VERIFIED_CLAIM) not in body
    assert VERIFIED_CLAIM not in body
    assert minted.share_id not in body


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


def test_every_open_records_what_the_recipient_was_shown(client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    client.get(minted.url)
    assert _opens(minted.share_id) == [(True, "testclient")]

    _break_the_source("v2", "20 megawatts (MW)", "25 megawatts (MW)")
    client.get(minted.url)

    recorded = _opens(minted.share_id)
    assert [verified for verified, _ in recorded] == [True, False]

    with session_scope() as session:
        row = session.get(ShareLink, minted.share_id)
        assert row.open_count == 2
        assert row.last_opened_at is not None


def test_an_unopened_link_says_it_has_never_been_opened(corpus):
    """Absence stays absence: last_opened_at is NULL, not the creation time."""
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        row = session.get(ShareLink, minted.share_id)
        assert row.last_opened_at is None
        assert row.open_count == 0


def test_every_open_is_audited_with_the_address_and_the_verdict(client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    client.get(minted.url)

    with session_scope() as session:
        opened = (
            session.query(AuditEvent)
            .filter(AuditEvent.action == sharing.ACTION_SHARE_OPENED)
            .all()
        )
        assert len(opened) == 1
        assert opened[0].subject_id == minted.share_id
        assert opened[0].ip == "testclient"
        assert verify_chain(session, COMPANY)


def test_an_open_of_a_dead_link_is_audited_and_files_no_measurement(client, corpus):
    """A refused open is a refusal, not a verification that failed.

    ShareOpen.verified records whether the check passed at that open. A dead
    link never reaches the check, so writing False there would file "never ran"
    as "ran and failed" -- which is the exact thing the column's NOT NULL was
    written to stop. The refusal goes in the audit chain instead, where there is
    a word for it.
    """
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    with session_scope() as session:
        sharing.revoke_share_link(
            session, company_id=COMPANY, share_id=minted.share_id,
            user_id=corpus["sharer"],
        )

    client.get(minted.url)

    assert _opens(minted.share_id) == []
    assert ACTION_ACCESS_DENIED in _actions()


def test_an_unknown_token_writes_no_row_at_all(client, corpus):
    """There is no tenant to attribute it to, and inventing one would be a lie."""
    before = len(_actions())
    client.get(sharing.share_url("b" * 43))
    with session_scope() as session:
        assert session.query(ShareOpen).count() == 0
    assert len(_actions()) == before


# ---------------------------------------------------------------------------
# A change, rather than a claim
# ---------------------------------------------------------------------------


def test_a_change_can_be_shared_and_shows_both_of_its_claims(client, corpus):
    minted = _mint(corpus["sharer"], PAIRED, subject_type=SHARE_SUBJECT_CHANGE)
    response = client.get(minted.url)

    assert response.status_code == 200
    assert _html(_statement(VERIFIED_CLAIM)) in response.text
    # The failing one is on the page as a refusal, and its statement is not.
    assert _statement(FAILING_CLAIM) not in response.text
    assert _html(_statement(FAILING_CLAIM)) not in response.text


def test_a_change_share_records_the_open_as_unverified_when_any_claim_fails(
    client, corpus
):
    """One page, one verdict, and the honest one is the pessimistic one."""
    minted = _mint(corpus["sharer"], PAIRED, subject_type=SHARE_SUBJECT_CHANGE)
    client.get(minted.url)
    assert [verified for verified, _ in _opens(minted.share_id)] == [False]


def test_a_change_nobody_wrote_a_claim_about_asserts_nothing_and_says_so(
    client, corpus
):
    """An empty page is not a verified page. Absence is not a measurement."""
    minted = _mint(corpus["sharer"], UNCLAIMED, subject_type=SHARE_SUBJECT_CHANGE)
    response = client.get(minted.url)

    assert response.status_code == 200
    assert "No claim made" in response.text
    assert [verified for verified, _ in _opens(minted.share_id)] == [False]


# ---------------------------------------------------------------------------
# A live link onto something that has gone
# ---------------------------------------------------------------------------


def test_a_live_link_whose_artifact_has_gone_refuses_rather_than_guesses(
    client, corpus
):
    """The link is real, so it is not the dead-link answer. There is just nothing.

    This is the difference between "you may not see this" and "there is nothing
    to see". Conflating them would either tell a stranger which tokens are real
    or tell a colleague their link was withdrawn when it was not.
    """
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    statement = _statement(VERIFIED_CLAIM)

    with session_scope() as session:
        session.delete(session.get(Claim, VERIFIED_CLAIM))

    response = client.get(minted.url)
    assert response.status_code == 200
    assert statement not in response.text
    assert _html(statement) not in response.text
    assert "No claim made" in response.text
    # The watermark still names the sender: the recipient's next move is to ask
    # them, and a page that forgot who sent it cannot suggest that.
    assert SHARER_NAME in response.text
    assert [verified for verified, _ in _opens(minted.share_id)] == [False]


# ---------------------------------------------------------------------------
# The reads a revoke button will need
# ---------------------------------------------------------------------------


def test_the_registry_reads_are_scoped_to_the_company_that_asks(corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    with session_scope() as session:
        mine = sharing.share_links_for_subject(
            session,
            company_id=COMPANY,
            subject_type=SHARE_SUBJECT_CLAIM,
            subject_id=VERIFIED_CLAIM,
        )
        assert [row.id for row in mine] == [minted.share_id]

        theirs = sharing.share_links_for_subject(
            session,
            company_id=RIVAL,
            subject_type=SHARE_SUBJECT_CLAIM,
            subject_id=VERIFIED_CLAIM,
        )
        assert theirs == []

        # share_opens carries no company_id of its own, so the join is the only
        # thing scoping it. Another tenant asking for this link's opens gets
        # nothing rather than the rows.
        sharing.open_share(session, token=minted.token, ip="10.0.0.1")
        assert len(
            sharing.opens_for_share(
                session, company_id=COMPANY, share_id=minted.share_id
            )
        ) == 1
        assert (
            sharing.opens_for_share(
                session, company_id=RIVAL, share_id=minted.share_id
            )
            == []
        )


def test_an_unscoped_read_is_refused_rather_than_answered(corpus):
    with session_scope() as session:
        for call in (
            lambda: sharing.sharing_enabled(session, company_id=""),
            lambda: sharing.share_links_for_subject(
                session, company_id="", subject_type=SHARE_SUBJECT_CLAIM,
                subject_id=VERIFIED_CLAIM,
            ),
            lambda: sharing.opens_for_share(session, company_id="", share_id="SHL-x"),
            lambda: sharing.claim_for_company(session, "", VERIFIED_CLAIM),
        ):
            with pytest.raises(ValueError):
                call()


def test_an_empty_token_resolves_to_nothing(corpus):
    with session_scope() as session:
        assert sharing.open_share(session, token="") is None
        assert sharing.open_share(session, token=None) is None


# ---------------------------------------------------------------------------
# The wiring nobody in this branch owns
# ---------------------------------------------------------------------------


def test_the_share_path_is_reachable_without_a_session():
    """Landed 2026-08-04. The xfail marker this carried is deleted, not flipped.

    It was written strict=False while the wiring belonged to another branch,
    which meant it went quietly green and sat as an XPASS in the summary line
    for a day -- nobody's eye catches one letter in "1714 passed, 2 xfailed, 1
    xpassed". strict=True would have failed the suite the moment the two lines
    landed and forced this deletion the same hour. That is the lesson worth more
    than the test: a non-strict xfail records an intention and guards nothing.

    What it guards now: an anonymous open of a share link must not be redirected
    to /login. The redirect was the defect -- it answered 303 to
    /login?next=%2Fs%2F<token>, copying a live bearer token into a query string,
    an access log and a Referer header.
    """
    from app.web.deps import is_public_path

    assert is_public_path(sharing.share_url("c" * 43))
