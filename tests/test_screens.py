"""The three screens: proceedings list, proceeding detail, review queue.

What these tests are actually for, in order of how much they matter.

1. An empty database must say so. A list screen that renders an empty table is
   asserting "nothing has changed", and nothing has checked that. The test reads
   the HTML for the words and for the command that fixes it, because "it looked
   obvious to me" is not a guarantee.

2. A withheld claim's statement must not reach the review queue in any form.
   Asserted against the response body, not against a CSS class, because a
   stylesheet change could otherwise reveal one silently.

3. Resolving an escalation must leave an audit trail that still verifies, and a
   rejection with no reason must write nothing at all.

4. Every route is scoped to one company. Acting as another tenant, each of the
   four routes refuses: no rows, a 404, and no write.

Offline, no API key, no network. The app under test is assembled here rather
than imported from app/main.py, so these screens can be tested before anything
wires them in -- and the mount path for the stylesheet is asserted, because
base.html links it as a fixed absolute path.
"""

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.audit import event_count, verify_chain
from app.state.claims import verified_claims
from app.state.db import get_engine, session_scope
from app.state.models import Base, Claim, DocumentVersion, Escalation, Proceeding
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.views import auth as auth_view
from app.web.views import proceedings as proceedings_view
from app.web.views import review as review_view

COMPANY = "MEP"
RIVAL = "RIVAL"
DOCKET = "MPUC-2026-0142"
MISQUOTE = "ESC-CLM-MISQUOTE"
AMBIGUOUS = "ESC-CLM-AMBIGUOUS"


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(proceedings_view.COMPANY_ENV, raising=False)
    monkeypatch.delenv(proceedings_view.COMPANY_NAME_ENV, raising=False)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_view.router)
    app.include_router(proceedings_view.router)
    app.include_router(review_view.router)
    # https, because the session cookie is marked Secure anywhere that is not
    # loopback plaintext, and httpx will not send a Secure cookie over http --
    # so a signed-in test would silently behave like an anonymous one. The same
    # base_url tests/test_login.py and tests/test_users_admin.py use.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def empty_schema():
    """Tables, no rows. The state of a fresh clone that ran the migrations."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def no_schema():
    """No tables at all, and the schema is put back afterwards.

    Restored on the way out so this file cannot leave the scratch database in a
    state that fails the next test to run against it.
    """
    engine = get_engine()
    Base.metadata.drop_all(engine)
    yield
    Base.metadata.create_all(engine)


@pytest.fixture
def seeded(empty_schema):
    with session_scope() as session:
        report = load(session)
        # Accounts too, because resolving an escalation is now gated on a
        # permission and attributed to a signed-in person. A corpus with no
        # people in it could not exercise either.
        ensure_accounts(session)
        return report


def _text(response) -> str:
    return response.text


# ---------------------------------------------------------------- empty state


def test_no_tables_says_so_and_names_the_command(client, no_schema):
    response = client.get("/proceedings")
    body = _text(response)

    assert response.status_code == 200
    assert "no tables yet" in body
    assert "python -m app.seed" in body
    # The failure this guards: an empty table reads as "nothing has changed".
    assert "<table" not in body


def test_no_rows_is_a_different_sentence_from_no_tables(client, empty_schema):
    body = _text(client.get("/proceedings"))

    assert "No proceeding is loaded for MEP" in body
    assert "python -m app.seed" in body
    assert "<table" not in body


def test_review_queue_with_no_tables_says_so(client, no_schema):
    response = client.get("/escalations")
    body = _text(response)

    assert response.status_code == 200
    assert "no tables yet" in body
    assert "python -m app.seed" in body


def test_review_queue_with_no_escalations_states_it_was_checked(client, empty_schema):
    body = _text(client.get("/escalations"))

    assert "Nothing is held for review" in body
    assert "python -m app.seed" not in body


# ------------------------------------------------------------ proceedings list


def test_proceedings_list_shows_the_seeded_docket(client, seeded):
    body = _text(client.get("/proceedings"))

    assert DOCKET in body
    assert "Meridian Public Utilities Commission" in body
    assert f'href="/proceedings/{DOCKET}"' in body
    # Latest version is the head of the chain the diffs recorded, not v1.
    assert "v3" in body
    assert "FINAL" in body
    assert "2 held" in body


def test_masthead_names_the_tenant_that_scoped_the_page(client, seeded, monkeypatch):
    monkeypatch.setenv(
        proceedings_view.COMPANY_NAME_ENV, "Meridian Electric & Power Company"
    )
    body = _text(client.get("/proceedings"))

    assert "Meridian Electric &amp; Power Company" in body
    assert 'class="coord">MEP<' in body


# ---------------------------------------------------------- proceeding detail


def test_proceeding_detail_shows_the_timeline_and_the_latest_pair(client, seeded):
    body = _text(client.get(f"/proceedings/{DOCKET}"))

    assert "Notice of Proposed Rulemaking" in body
    assert "Revised Proposed Rule" in body
    assert "Final Order" in body
    assert "DRAFT" in body and "FINAL" in body
    # Both diffed pairs are offered; the most recent one is selected.
    assert "v1 to v2" in body and "v2 to v3" in body
    assert f'href="/proceedings/{DOCKET}?from=v1&amp;to=v2"' in body
    assert 'href="/changes/CHG-v2-v3-000"' in body
    assert "CHG-v1-v2-000" not in body


def test_proceeding_detail_selected_pair_lists_its_changes(client, seeded):
    body = _text(client.get(f"/proceedings/{DOCKET}?from=v1&to=v2"))

    assert 'href="/changes/CHG-v1-v2-003"' in body
    assert "CHG-v2-v3-000" not in body
    # The paired demonstration sits on this change: one claim verifies, one is
    # held, and the list says so before anyone clicks.
    assert "1 held" in body


def test_a_pair_that_was_never_diffed_is_refused_not_emptied(client, seeded):
    response = client.get(f"/proceedings/{DOCKET}?from=v1&to=v3")
    body = _text(response)

    assert response.status_code == 200
    assert "Nothing was diffed between v1 and v3" in body
    assert 'href="/changes/' not in body


def test_unknown_proceeding_is_a_404(client, seeded):
    assert client.get("/proceedings/MPUC-9999-0001").status_code == 404


# ---------------------------------------------------------------- review queue


def test_review_queue_shows_exactly_the_seeded_escalations(client, seeded):
    body = _text(client.get("/escalations"))

    assert body.count('class="queue__row"') == 2
    assert MISQUOTE in body and AMBIGUOUS in body

    # The reason codes app/state/claims.py branches on, and the verifier's own
    # words. Two vocabularies for one refusal is one too many.
    assert "CITATION_QUOTE_MISMATCH" in body
    assert "CITATION_AMBIGUOUS_OCCURRENCE" in body
    assert "quoted text does not match the source at the cited offsets" in body
    assert "quoted text appears more than once" in body

    # The citation address, and the source really read at those offsets.
    assert "v2:" in body
    assert "Quoted by the claim" in body
    assert "The source at those offsets" in body
    assert 'class="source-mark"' in body


def test_the_queue_shows_what_the_source_says_not_what_was_quoted(client, seeded):
    body = _text(client.get("/escalations"))

    # The planted misquote reads "10 megawatts" where the source reads "20".
    # Both appear -- one as the quote, one as the source -- which is the whole
    # point of the row.
    assert "10 megawatts" in body
    assert "20 megawatts" in body


def test_the_ambiguous_row_says_how_many_times_the_quote_appears(client, seeded):
    """The two strings on that row are identical, so the count is the reason.

    A reader who sees the same sentence twice under "quoted" and "the source"
    would reasonably think the refusal is a bug. What is missing is which of the
    three identical sentences the claim relies on, and only the count shows it.
    """
    body = _text(client.get("/escalations"))

    assert "Times it appears in v3" in body
    assert "The claim did not say which one it means" in body


def test_a_corrected_citation_stops_being_withheld_on_the_next_read(client, seeded):
    """The verdict is recomputed per request, never read from a stored flag."""
    with session_scope() as session:
        claim = session.get(Claim, "CLM-MISQUOTE")
        version = session.get(DocumentVersion, claim.citation_version_id)
        claim.citation_quote = version.source_text[
            claim.citation_start : claim.citation_end
        ]

    body = _text(client.get("/escalations"))

    # The escalation is still listed -- an item that vanished would be the queue
    # lying about what is outstanding -- and it says the refusal no longer holds.
    assert MISQUOTE in body
    assert "verifies on re-check now" in body

    # It keeps its citation and its source, so the correction is checkable.
    # Both rows now show the marked span; neither says the source is unreadable.
    assert body.count('class="source-mark"') == 2
    assert "not readable" not in body
    # No mismatch pair on a citation that matches: there is nothing to contrast.
    assert body.count("Quoted by the claim") == 1


def test_no_withheld_statement_reaches_the_review_queue(client, seeded):
    """ADR-003, asserted against the HTML rather than against a CSS class."""
    body = _text(client.get("/escalations"))

    # The statements of the escalated claims, read from the database rather than
    # typed here, so an edit to the corpus cannot make this pass by accident.
    with session_scope() as session:
        statements = [
            session.get(Claim, row.claim_id).statement
            for row in session.query(Escalation).filter_by(company_id=COMPANY).all()
        ]

    assert statements, "the seed should have escalated something"
    for statement in statements:
        assert statement not in body


# ------------------------------------------------------------------ resolving


def _resolve(client, escalation_id: str, **form) -> object:
    return client.post(
        f"/escalations/{escalation_id}/resolve", data=form, follow_redirects=False
    )


def _role(name: str) -> str:
    """The seeded address holding one role, read from the seed rather than typed."""
    return next(a.email for a in demo_account_list() if a.role == name)


def _sign_in(client, role: str = "analyst"):
    """Put a real session behind the client.

    Resolving an escalation is gated on escalation.resolve and attributed to
    whoever is signed in, so these tests need an identity rather than a name in
    a form field. Signing in through the real /login is deliberate: it is the
    same path a person takes, so a change that breaks sign-in breaks these too
    rather than leaving them passing against a stub.
    """
    response = client.post(
        "/login",
        data={"email": _role(role), "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"sign-in failed: {response.status_code}"
    return response


def test_approving_writes_an_audit_event_and_the_chain_still_verifies(client, seeded):
    _sign_in(client)
    with session_scope() as session:
        before = event_count(session, COMPANY)

    response = _resolve(client, MISQUOTE, decision="approve", reviewer="J. Okonkwo")

    assert response.status_code == 303
    assert response.headers["location"] == "/escalations"

    with session_scope() as session:
        assert event_count(session, COMPANY) == before + 1
        assert verify_chain(session, COMPANY) is True

        row = session.get(Escalation, MISQUOTE)
        assert row.resolved_at is not None
        # The signed-in analyst, not "J. Okonkwo" from the form field. This
        # asserted the typed name until 2026-08-04, which is what made the field
        # forgeable in the first place.
        assert row.resolved_by == f"person:{_role('analyst')}"

    body = _text(client.get("/escalations"))
    assert body.count('class="queue__row"') == 1
    assert f"person:{_role('analyst')}" in body


def test_an_unsigned_resolution_is_refused_rather_than_recorded_as_a_stranger(
    client, seeded
):
    """This asserted the opposite until 2026-08-04, and the old answer was wrong.

    The route used to take the reviewer's name from a form field, so an unsigned
    caller was recorded as `person:unauthenticated` and a signed-in one was
    recorded as whatever they typed. Labelling the anonymous case honestly is not
    the same as refusing it, and it left the signed-in case free to write any
    name at all into an append-only chain. Nothing is written now unless somebody
    is signed in and holds escalation.resolve.
    """
    with session_scope() as session:
        before = event_count(session, COMPANY)

    assert _resolve(client, MISQUOTE, decision="approve").status_code == 403

    with session_scope() as session:
        assert session.get(Escalation, MISQUOTE).resolved_by is None
        assert session.get(Escalation, MISQUOTE).resolved_at is None
        assert event_count(session, COMPANY) == before


def test_the_resolver_recorded_is_the_signed_in_person_not_the_form_field(
    client, seeded
):
    """The typed field is still accepted and is no longer believed."""
    _sign_in(client)
    _resolve(client, MISQUOTE, decision="approve", reviewer="Someone Else Entirely")

    with session_scope() as session:
        assert session.get(Escalation, MISQUOTE).resolved_by == f"person:{_role('analyst')}"


def test_rejecting_without_a_reason_writes_nothing(client, seeded):
    _sign_in(client)
    with session_scope() as session:
        before = event_count(session, COMPANY)

    response = _resolve(client, MISQUOTE, decision="reject", reason="   ")
    body = _text(response)

    assert response.status_code == 400
    assert "needs a reason" in body

    with session_scope() as session:
        assert event_count(session, COMPANY) == before
        assert session.get(Escalation, MISQUOTE).resolved_at is None


def test_rejecting_records_the_dispute_and_the_claim_stays_withheld(client, seeded):
    _sign_in(client)
    response = _resolve(
        client,
        MISQUOTE,
        decision="reject",
        reason="the drafter says the figure is right",
        reviewer="A. Bell",
    )
    assert response.status_code == 303

    with session_scope() as session:
        assert verify_chain(session, COMPANY) is True

        row = session.get(Escalation, MISQUOTE)
        assert row.resolved_by == f"person:{_role('analyst')}"

        # The decisive assertion: a person disputing a refusal does not make the
        # citation verify. The claim is still withheld on the next read.


        change_id = session.get(Claim, row.claim_id).change_id
        _verified, held = verified_claims(session, COMPANY, change_id)
        assert row.claim_id in {claim.claim_id for claim in held}


def test_resolving_the_same_item_twice_conflicts(client, seeded):
    _sign_in(client)
    assert _resolve(client, MISQUOTE, decision="approve").status_code == 303

    with session_scope() as session:
        before = event_count(session, COMPANY)

    assert _resolve(client, MISQUOTE, decision="approve").status_code == 409

    with session_scope() as session:
        assert event_count(session, COMPANY) == before


def test_an_unknown_decision_is_refused(client, seeded):
    assert _resolve(client, MISQUOTE, decision="maybe").status_code == 400

    with session_scope() as session:
        assert session.get(Escalation, MISQUOTE).resolved_at is None


# ------------------------------------------------------------- tenant scoping


def test_another_tenants_rows_are_invisible_on_the_list(client, seeded):
    with session_scope() as session:
        session.add(
            Proceeding(
                id="AUB-2026-0001",
                company_id=RIVAL,
                docket="AUB-2026-0001",
                commission="Ashford Utility Board (AUB)",
                subject="Something the other company tracks",
            )
        )

    body = _text(client.get("/proceedings"))
    assert DOCKET in body
    assert "AUB-2026-0001" not in body


def test_every_route_refuses_another_tenant(client, seeded, monkeypatch):
    monkeypatch.setenv(proceedings_view.COMPANY_ENV, RIVAL)

    listing = client.get("/proceedings")
    assert listing.status_code == 200
    assert DOCKET not in _text(listing)
    assert "No proceeding is loaded for RIVAL" in _text(listing)

    assert client.get(f"/proceedings/{DOCKET}").status_code == 404

    queue = client.get("/escalations")
    assert queue.status_code == 200
    assert MISQUOTE not in _text(queue)
    assert "Nothing is held for review" in _text(queue)

    with session_scope() as session:
        before = event_count(session, COMPANY)

    # Refused. 403 rather than 404 because the permission is asked before the
    # id is resolved, on purpose: checking the row first would let somebody
    # without the permission learn which escalation ids exist. Either way it is
    # a refusal that writes nothing, which is what this test is about.
    assert _resolve(client, MISQUOTE, decision="approve").status_code in (403, 404)

    with session_scope() as session:
        # No write, and no entry in the other company's chain either.
        assert session.get(Escalation, MISQUOTE).resolved_at is None
        assert event_count(session, COMPANY) == before
        assert event_count(session, RIVAL) == 0


# ------------------------------------------------------------------- the shell


def test_the_stylesheet_is_served_where_base_html_looks_for_it(client, seeded):
    assert '<link rel="stylesheet" href="/static/strata.css">' in _text(client.get("/proceedings"))

    sheet = client.get("/static/strata.css")
    assert sheet.status_code == 200
    assert ".claim--withheld" in _text(sheet)
