"""The integrations registry: where the documents come from, and who may change it.

WHY THIS FILE IS SHAPED LIKE THIS. A regulated buyer asks two questions of any
product that reads their filings: where does your data come from, and who can
change that. The registry screen is the answer to both, so the tests below are
the questions a security reviewer asks in the room, in the order they ask them.

1. **Does the gate hold?** The screen and all six write paths sit behind
   SOURCE_REGISTRY_PERMISSION. An analyst is refused on every one of them and
   the refusal lands in the audit chain. A hidden button is not a control.

2. **Does the screen tell the truth about what this build does?** NOTHING IN
   THIS PRODUCT FETCHES ANYTHING. There is no crawler, no poller and no
   scheduler. The single defect this repository keeps finding is a row that
   reads like a live feed when nothing polls it, so the sentence "nothing in
   this build fetches on a schedule" is asserted to be ON THE PAGE, and the four
   statuses are asserted to be distinguishable from each other and from
   `enabled` -- which is a different fact and a different column.

3. **Is the key safe?** It is never stored, so it can never be shown back. The
   row holds the NAME of an environment variable. The tests put a real secret in
   the environment, register a source that names it, and then sweep every route
   this router defines and every column of the row looking for that value.

4. **Is the connection test honest?** There is none, on purpose: a button that
   makes this server fetch a URL an administrator typed is a server-side request
   forgery surface. The refusal is asserted to be on the page with its reason,
   and no request through this router is allowed to open a socket.

5. **Are the rows real?** The 102 filings in data/real came from eight
   commissions and each provenance file names the URL it was fetched from. Those
   eight register as public dockets, so the registry describes where the real
   corpus actually came from rather than demonstrating an empty table.

6. **Is it one tenant?** Another company's source is not on the page and cannot
   be edited, disabled or removed through it.

WHAT THESE TESTS DO NOT DO. They do not lay out a page, so they cannot prove the
registry is readable on a handset; tests/test_responsive.py guards the classes of
defect that made it unreadable before and the last section here holds this
template to the same rules. They do not prove the endpoint preflight is a
sufficient SSRF defence -- it is not one, it opens no socket and is not allowed
to, and that is the whole of its claim.

Offline, no API key, no network. The application is assembled here rather than
imported from app/main.py, following tests/test_share_registry.py, so the screen
can be tested before anything wires it in. https://testserver, because the
session cookie is marked Secure and a client on http drops it in silence.
"""

import re
import socket
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state import sources
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import user_by_email
from app.state.models import (
    FETCHABLE_SOURCE_KINDS,
    ROLE_ADMIN,
    ROLE_ANALYST,
    SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
    SOURCE_REGISTRATION_KIND_MCP_SERVER,
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    SOURCE_REGISTRATION_KIND_REST_API,
    SOURCE_REGISTRATION_KINDS,
    SOURCE_REGISTRY_PERMISSION,
    SOURCE_STATUS_NOT_IMPLEMENTED,
    SOURCE_STATUSES,
    AuditEvent,
    DocumentVersion,
    SourceRegistration,
)
from app.web import deps
from app.web.deps import install_auth
from app.web.views import auth as auth_view
from app.web.views import integrations

COMPANY = "MEP"
RIVAL = "RIVAL"

SOURCES = integrations.SOURCES_URL
ADD = integrations.ADD_URL
CORPUS = integrations.CORPUS_URL

# A value that must never come back out of the product. It is put in the
# environment, named by a registration, and then hunted for.
SECRET_NAME = "STRATA_SOURCE_ACME_KEY"
SECRET_VALUE = "sk-live-9f3a-do-not-render-this-anywhere"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


def _email(role: str) -> str:
    """The seeded account holding a role, read from the corpus rather than typed."""
    return next(account.email for account in demo_account_list() if account.role == role)


@pytest.fixture
def anonymous() -> TestClient:
    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(integrations.router)
    # The same guard the product installs. A screen tested without it would
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
    """Signed in as somebody who may read the product and administer nothing."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
    return anonymous


def _admin_id() -> str:
    with session_scope() as session:
        return user_by_email(session, COMPANY, _email(ROLE_ADMIN)).id


def _rows(company_id: str = COMPANY) -> list[SourceRegistration]:
    with session_scope() as session:
        return sources.source_registrations_for_company(session, company_id=company_id)


def _add(client: TestClient, **fields):
    """Post the add form with sensible defaults for anything not named."""
    data = {
        "name": "Acme filings API",
        "kind": SOURCE_REGISTRATION_KIND_REST_API,
        "endpoint": "https://filings.acme.example/v1",
        "docket": "",
        "credential_ref": "",
    }
    data.update(fields)
    return client.post(ADD, data=data, follow_redirects=False)


def _actions(company_id: str = COMPANY) -> list[str]:
    with session_scope() as session:
        return [
            row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
            .all()
        ]


def _one_source(client: TestClient, **fields) -> SourceRegistration:
    """Register one source through the screen and hand back the row it wrote."""
    assert _add(client, **fields).status_code == 303
    rows = _rows()
    assert len(rows) == 1, "the add form wrote something other than one row"
    return rows[0]


# ------------------------------------------------------------------ the gate


def test_nobody_signed_out_reaches_the_registry_or_changes_it(anonymous):
    """Redirects off. A client that follows them turns a refusal into a 200."""
    for method, path in _every_route():
        response = anonymous.request(method, path, data={}, follow_redirects=False)
        assert response.status_code == 303, f"{method} {path} answered a stranger"
        assert response.headers["location"].startswith("/login")


def test_an_analyst_is_refused_the_screen_and_told_which_permission_opens_it(analyst):
    response = analyst.get(SOURCES)
    assert response.status_code == 403
    assert SOURCE_REGISTRY_PERMISSION in response.text
    # A refusal must not leak the thing it refuses.
    assert "Georgia Public Service Commission" not in response.text


def test_an_analyst_is_refused_every_write_path_and_changes_nothing(analyst):
    for method, path in _every_route():
        if method == "GET":
            continue
        response = analyst.request(method, path, data=_form_for(path))
        assert response.status_code == 403, f"{method} {path} let an analyst through"
    assert _rows() == [], "a refused write still wrote a row"


def test_the_refusal_is_in_the_audit_chain_and_the_chain_still_verifies(analyst):
    analyst.get(SOURCES)
    assert ACTION_ACCESS_DENIED in _actions()
    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_one_refused_click_writes_one_refusal_and_not_two(analyst):
    """A screen that re-checks the gate while rendering the refusal audits twice.

    This file caught exactly that: the write path refused, then built the
    refusal page by asking policy.require() again, and one decision read as two
    in a chain that is supposed to be the record of decisions.
    """
    before = _actions().count(ACTION_ACCESS_DENIED)
    assert _add(analyst).status_code == 403
    assert _actions().count(ACTION_ACCESS_DENIED) - before == 1


def test_the_administrator_gets_the_screen(admin):
    assert admin.get(SOURCES).status_code == 200


# ------------------------------------------------ the truth about this build


def test_the_page_says_nothing_here_fetches_on_a_schedule(admin):
    """The sentence, on the page, in words a buyer can quote.

    Not in a docstring, not in a comment. A row that reads like a live feed when
    nothing polls it is the defect this repository keeps finding, and the only
    cure that survives a screenshot is the sentence being visible.
    """
    body = admin.get(SOURCES).text
    assert sources.FETCHES_NOTHING in body


def test_nothing_in_this_build_can_fetch_and_the_page_asks_rather_than_assumes(admin):
    """FETCHABLE_SOURCE_KINDS is empty, and the screen has to read it.

    If a fetcher lands for one kind, that tuple gains one entry and this test
    starts asserting the other half of the sentence. A screen that hardcoded
    "no fetcher exists" would keep saying it after one did.
    """
    row = _one_source(admin)
    body = admin.get(SOURCES).text
    if row.kind in FETCHABLE_SOURCE_KINDS:
        assert "no fetcher exists for this kind" not in body
    else:
        assert "no fetcher exists for this kind" in body


def test_status_and_enabled_are_shown_as_two_different_facts(admin):
    """The pair every honest row carries today: enabled, and cannot be fetched.

    A screen that folds them into one word renders an administrator's intent as
    if it were a statement about reachability. app/state/models.py says a
    registry that cannot show that pair has not been built to the design.
    """
    _one_source(admin)
    body = admin.get(SOURCES).text
    assert sources.STATUS_WORDS[SOURCE_STATUS_NOT_IMPLEMENTED] in body
    assert "Enabled" in body


def test_a_source_that_has_never_been_scanned_says_so_rather_than_going_blank(admin):
    _one_source(admin)
    assert sources.NEVER_SCANNED in admin.get(SOURCES).text


@pytest.mark.parametrize("status", SOURCE_STATUSES)
def test_the_screen_tells_all_four_statuses_apart_including_the_three_it_never_sees(
    admin, status
):
    """The brief asks for four states, at a glance and in words a buyer can quote.

    Only one of them can occur in this build, because nothing fetches. A screen
    written for the one state it happens to meet would render the other three as
    a bare code the day a fetcher lands, which is when somebody is least able to
    read it. So the rows are written directly and the page is asked for its
    words -- and for the error, which is what makes `unreachable` useful at all.
    """
    scanned = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)
    with session_scope() as session:
        session.add(
            SourceRegistration(
                id=f"SRC-{status}",
                company_id=COMPANY,
                name=f"A source that is {status}",
                kind=SOURCE_REGISTRATION_KIND_REST_API,
                status=status,
                config={"endpoint": "https://filings.acme.example/v1"},
                credential_ref=None,
                created_by_user_id=_admin_id(),
                created_at=scanned,
                last_scanned_at=scanned,
                last_result="the docket returned 403",
                enabled=True,
            )
        )

    body = admin.get(SOURCES).text
    assert sources.STATUS_WORDS[status] in body
    assert "2026-08-01 09:30 UTC" in body
    assert "the docket returned 403" in body


def test_every_kind_this_product_names_is_offered_and_described(admin):
    """Four kinds, and the screen must not quietly know only two."""
    body = admin.get(SOURCES).text
    for kind in SOURCE_REGISTRATION_KINDS:
        assert sources.KIND_WORDS[kind] in body


# ----------------------------------------------------- the connection test


def test_the_page_refuses_a_connection_test_and_says_why(admin):
    """Refusing is the answer when the guard is not there, and it is said aloud."""
    body = admin.get(SOURCES).text
    assert sources.CONNECTION_TEST_REFUSAL in body
    # The other half of the honest answer: reaching an endpoint is not the same
    # as being able to fetch a filing from it.
    assert sources.HANDSHAKE_IS_NOT_A_FETCH in body


def test_no_request_through_this_router_opens_a_socket(admin, monkeypatch):
    """The refusal above, enforced rather than promised.

    Anything that reached out -- a connection test somebody added later, a
    favicon, a metadata probe -- fails here rather than on somebody network.

    socket.socket itself is NOT patched, and that is a limit rather than an
    oversight: the test client builds its own event loop out of a socket pair,
    so patching the constructor breaks the harness instead of the product. The
    three seams below are the ones every outbound HTTP client in Python goes
    through -- urllib, httpx and requests all resolve a name and connect.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("this router reached the network; nothing here may fetch")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)

    for method, path in _every_route():
        admin.request(method, path, data=_form_for(path))


def test_neither_the_screen_nor_its_write_layer_can_reach_the_network_at_all(
):
    """The class, not the line: a fetch has to be imported before it can happen.

    The test above proves this request did not reach out. This one proves the
    two modules hold no way to. Whoever adds a connection test has to import
    something here, and this fails before it is wired to a button.
    """
    import pathlib

    forbidden = ("urllib.request", "httpx", "requests", "aiohttp", "http.client")
    for module in (
        pathlib.Path(sources.__file__),
        pathlib.Path(integrations.__file__),
    ):
        text = module.read_text()
        for name in forbidden:
            assert f"import {name}" not in text, f"{module.name} imports {name}"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/internal",
        "http://192.168.0.9/",
        "http://[::1]/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "https://",
        "not a url at all",
    ],
)
def test_the_preflight_refuses_the_addresses_that_make_this_dangerous(url):
    verdict = sources.endpoint_preflight(url)
    assert not verdict.allowed, f"{url} would have been allowed"
    assert verdict.reason, "a refusal with no reason is not a refusal"


def test_the_preflight_allows_an_ordinary_public_endpoint_and_claims_nothing_more():
    verdict = sources.endpoint_preflight("https://filings.acme.example/v1")
    assert verdict.allowed
    # The sentence matters as much as the verdict: this is a check on text.
    assert "contact" in verdict.reason.lower()


# --------------------------------------------------------------- the writes


def test_registering_a_source_writes_one_row_with_the_honest_status(admin):
    row = _one_source(admin, name="Acme filings API")
    assert row.name == "Acme filings API"
    assert row.kind == SOURCE_REGISTRATION_KIND_REST_API
    assert row.status == SOURCE_STATUS_NOT_IMPLEMENTED
    assert row.enabled is True
    assert row.last_scanned_at is None
    assert row.last_result is None
    assert row.created_by_user_id == _admin_id()


def test_every_change_to_the_registry_is_audited_under_its_own_action(admin):
    row = _one_source(admin)
    admin.post(f"{SOURCES}/{row.id}/edit", data={
        "name": "Acme filings API v2",
        "endpoint": "https://filings.acme.example/v2",
        "docket": "",
        "credential_ref": "",
    })
    admin.post(f"{SOURCES}/{row.id}/disable", data={})
    admin.post(f"{SOURCES}/{row.id}/enable", data={})
    admin.post(f"{SOURCES}/{row.id}/remove", data={})

    written = _actions()
    for action in (
        sources.ACTION_SOURCE_REGISTERED,
        sources.ACTION_SOURCE_UPDATED,
        sources.ACTION_SOURCE_DISABLED,
        sources.ACTION_SOURCE_ENABLED,
        sources.ACTION_SOURCE_REMOVED,
    ):
        assert written.count(action) == 1, f"{action} was not written exactly once"

    with session_scope() as session:
        assert verify_chain(session, COMPANY)
    assert _rows() == []


def test_disabling_a_source_changes_what_the_admin_wants_and_not_what_is_true(admin):
    """enabled is intent. status is fact. Disabling must not touch the second."""
    row = _one_source(admin)
    before = row.status
    landed = admin.post(f"{SOURCES}/{row.id}/disable", data={}, follow_redirects=False)
    assert landed.status_code == 303

    after = _rows()[0]
    assert after.enabled is False
    assert after.status == before, "disabling a source rewrote a fact about it"

    body = admin.get(SOURCES).text
    assert "Disabled" in body
    assert sources.STATUS_WORDS[before] in body


def test_a_source_documents_point_at_cannot_be_removed_and_the_screen_says_why(admin):
    """Removal is refused rather than made to work by cutting the provenance.

    A registration deleted out from under the versions it brought leaves rows
    that came from somewhere with nothing saying where. Disabling is the act
    that was wanted; the screen says so instead of guessing.
    """
    row = _one_source(admin)
    with session_scope() as session:
        session.add(
            DocumentVersion(
                id="DOC-FROM-ACME",
                company_id=COMPANY,
                docket="ACME-1",
                label="A filing this registration brought",
                status="FINAL",
                source_text="text",
                source_sha256="0" * 64,
                source_url="https://filings.acme.example/v1/1",
                source_registration_id=row.id,
            )
        )

    response = admin.post(f"{SOURCES}/{row.id}/remove", data={})
    assert response.status_code == 409
    assert len(_rows()) == 1, "a refused removal removed the row anyway"
    assert sources.ACTION_SOURCE_REMOVED not in _actions()
    assert "1" in response.text and "disable" in response.text.lower()


def test_a_kind_the_product_does_not_define_is_refused(admin):
    response = _add(admin, kind="ftp_drop")
    assert response.status_code == 400
    assert _rows() == []


def test_a_source_with_nowhere_to_reach_is_refused(admin):
    response = _add(admin, endpoint="", docket="")
    assert response.status_code == 400
    assert _rows() == []


# ---------------------------------------------------------------- the secret


def test_a_credential_is_referenced_by_name_and_the_name_is_all_that_is_stored(
    admin, monkeypatch
):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    row = _one_source(admin, credential_ref=SECRET_NAME)
    assert row.credential_ref == SECRET_NAME
    assert SECRET_VALUE not in str(row.config)


def test_the_key_cannot_be_read_back_through_any_endpoint_this_router_defines(
    admin, monkeypatch
):
    """The sweep is derived from the router, so a route added later is covered."""
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    row = _one_source(admin, credential_ref=SECRET_NAME)

    for method, path in _every_route(source_id=row.id):
        body = admin.request(method, path, data=_form_for(path)).text
        assert SECRET_VALUE not in body, f"{method} {path} rendered the key"

    # And it is not in the database either, which is the reason it cannot be
    # rendered: the product never held it.
    with session_scope() as session:
        for stored in session.query(SourceRegistration).all():
            assert SECRET_VALUE not in "".join(
                str(getattr(stored, column.name)) for column in stored.__table__.columns
            )


def test_the_screen_says_a_key_is_named_and_whether_it_resolves_never_the_key(
    admin, monkeypatch
):
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    row = _one_source(admin, credential_ref=SECRET_NAME)
    body = admin.get(SOURCES).text
    assert SECRET_NAME in body
    assert SECRET_VALUE not in body
    assert sources.CREDENTIAL_RESOLVES in body

    monkeypatch.delenv(SECRET_NAME)
    body = admin.get(SOURCES).text
    assert sources.CREDENTIAL_MISSING in body
    assert row.credential_ref in body


def test_a_pasted_key_is_refused_and_never_echoed_back(admin):
    """The failure mode this rule exists for: somebody pastes the key itself.

    The refusal must not quote what was submitted. An error message that echoes
    its input puts the key on the page and into whatever logs that page.
    """
    response = _add(admin, credential_ref=SECRET_VALUE)
    assert response.status_code == 400
    assert SECRET_VALUE not in response.text
    assert sources.CREDENTIAL_REF_PREFIX in response.text
    assert _rows() == []
    with session_scope() as session:
        rows = session.query(AuditEvent).filter(AuditEvent.company_id == COMPANY).all()
        for event in rows:
            assert SECRET_VALUE not in f"{event.reason} {event.subject_id} {event.citation}"


def test_a_name_outside_the_allowed_prefix_is_refused(admin):
    """models.py conceded this as a capability nothing in the schema can express.

    Whoever writes a row can point it at any secret the process can read, so the
    write layer holds the allow-list. Without it, an administrator could name
    the session-signing key and have the product present it to a third party.
    """
    response = _add(admin, credential_ref="AWS_SECRET_ACCESS_KEY")
    assert response.status_code == 400
    assert _rows() == []


def test_a_public_docket_may_not_carry_a_credential_at_all(admin):
    response = _add(
        admin,
        kind=SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
        endpoint="https://psc.example.gov/dockets",
        credential_ref=SECRET_NAME,
    )
    assert response.status_code == 400
    assert _rows() == []


def test_a_config_that_carries_a_secret_is_refused_with_a_sentence(admin):
    """The check constraint is the backstop; a person gets words, not an error."""
    response = _add(admin, endpoint="https://acme.example/v1?api_key=abcd")
    assert response.status_code == 400
    assert "api_key" in response.text
    assert _rows() == []


def test_the_write_layer_refuses_the_same_words_the_constraint_does():
    """One tuple, checked in both places, so the two cannot come to disagree."""
    from app.state.models import CONFIG_FORBIDDEN_SUBSTRINGS

    for word in CONFIG_FORBIDDEN_SUBSTRINGS:
        assert sources.config_problem({"endpoint": f"https://x.example/{word.upper()}"})


# ---------------------------------------------------------------- the corpus


def test_the_corpus_names_eight_commissions_and_a_hundred_and_two_filings():
    """Read from data/real, not typed here. The count is the corpus's own."""
    found = sources.corpus_sources()
    assert len(found) == 8
    assert sum(entry.documents for entry in found) == 102
    for entry in found:
        assert entry.origins, f"{entry.jurisdiction} names nowhere it was fetched from"
        assert entry.dockets


def test_registering_the_corpus_writes_eight_public_dockets_and_repeats_safely(admin):
    assert admin.post(CORPUS, data={}, follow_redirects=False).status_code == 303
    rows = _rows()
    assert len(rows) == 8
    assert {row.kind for row in rows} == {SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET}
    assert all(row.status == SOURCE_STATUS_NOT_IMPLEMENTED for row in rows)
    assert all(row.enabled for row in rows)
    assert _actions().count(sources.ACTION_SOURCE_REGISTERED) == 8

    # Pressed twice. A second press is not a second registration.
    assert admin.post(CORPUS, data={}, follow_redirects=False).status_code == 303
    assert len(_rows()) == 8
    assert _actions().count(sources.ACTION_SOURCE_REGISTERED) == 8

    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_the_registered_corpus_rows_say_what_they_account_for(admin):
    admin.post(CORPUS, data={})
    body = admin.get(SOURCES).text
    for entry in sources.corpus_sources():
        assert entry.jurisdiction in body
        for host in entry.origins:
            assert host in body
    # The counts are different questions and the page must not merge them:
    # what is on disk, what came from the same address, and what points here.
    assert sources.CORPUS_COUNT_CAVEAT in body


def test_a_derived_count_is_shown_as_derived_and_not_as_a_stored_link(admin):
    """The number that can be non-zero today, and the reason it is weaker.

    Nothing writes source_registration_id, so every stored link is zero. The
    URL on each version can still say a filing came from the same host as a
    registration -- useful, and NOT the same fact. The page prints both with
    the words that tell them apart, because a derivation shown as a link is a
    claim this product did not earn.
    """
    admin.post(CORPUS, data={})
    with session_scope() as session:
        session.add(
            DocumentVersion(
                id="DOC-KY-1",
                company_id=COMPANY,
                docket="KY 2025-00113",
                label="A filing pulled by hand before the registry existed",
                status="FINAL",
                source_text="text",
                source_sha256="1" * 64,
                source_url="https://psc.ky.gov/pscscf/2025%20cases/2025-00113/x.pdf",
                source_registration_id=None,
            )
        )

    body = admin.get(SOURCES).text
    assert f"1 {sources.MATCHED_BY_ADDRESS}" in body
    assert f"0 {sources.POINTS_AT_REGISTRATION}" in body


def test_a_registry_that_cannot_be_read_says_so_rather_than_claiming_to_be_empty(
    admin,
):
    """The live hazard, reproduced: code ahead of the schema it reads.

    The deployment creates tables only when the database file is absent, so a
    table added today is missing on the running site tomorrow. An empty page
    here would tell a security reviewer that this company registered no sources,
    which is a statement, and a false one. The banner names the table instead.
    """
    from app.state.db import get_engine

    SourceRegistration.__table__.drop(get_engine())

    response = admin.get(SOURCES)
    assert response.status_code == 200
    assert "No source is registered" not in response.text
    assert "source_registrations" in response.text


def test_the_page_reports_how_much_of_the_workspace_has_any_provenance_at_all(admin):
    """Honest even when the answer is none, which today it is.

    scripts/ingest_real.py drops every provenance field, so the versions in this
    database carry no source_url and point at no registration. A screen that
    said nothing here would let a reader assume the link exists.
    """
    body = admin.get(SOURCES).text
    assert sources.PROVENANCE_GAP in body


# ---------------------------------------------------------------- one tenant


def test_another_companys_source_is_not_on_the_page_and_cannot_be_touched(admin):
    with session_scope() as session:
        session.add(
            SourceRegistration(
                id="SRC-rival",
                company_id=RIVAL,
                name="A rival's document store",
                kind=SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
                status=SOURCE_STATUS_NOT_IMPLEMENTED,
                config={"endpoint": "/mnt/rival"},
                credential_ref=None,
                created_by_user_id=_admin_id(),
                enabled=True,
            )
        )

    assert "A rival's document store" not in admin.get(SOURCES).text
    for verb in ("edit", "enable", "disable", "remove"):
        response = admin.post(f"{SOURCES}/SRC-rival/{verb}", data=_form_for("edit"))
        assert response.status_code == 404, verb

    with session_scope() as session:
        still = session.get(SourceRegistration, "SRC-rival")
        assert still is not None and still.enabled is True


def test_a_scoped_read_refuses_to_answer_without_a_company():
    with session_scope() as session:
        with pytest.raises(ValueError):
            sources.source_registrations_for_company(session, company_id="")


# --------------------------------------------------------------- responsive


def test_the_template_hardcodes_no_width_the_stylesheet_cannot_override():
    """The rule tests/test_responsive.py enforces, applied to a `<style>` block.

    That file scans inline style attributes and the stylesheet. This template
    carries its own rules in the head, which neither scan reaches, so the same
    rule is asserted here rather than left to nobody.
    """
    text = (integrations.TEMPLATE_DIR / integrations.TEMPLATE).read_text()
    offenders = [
        width
        for width in re.findall(r"(?:min-)?width:\s*([0-9]{3,})px", text)
        if int(width) >= 400
    ]
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_the_screen_renders_a_plain_table_the_stylesheet_can_scroll(admin):
    """The narrow-screen rule in strata.css targets `table`, so ours must be one."""
    _one_source(admin)
    body = admin.get(SOURCES).text
    assert "<table" in body
    assert 'name="viewport"' in body


# ------------------------------------------------------- wiring, not building


def test_this_router_claims_no_path_another_router_already_owns():
    """The collision that left two routers mounted nowhere in this repository.

    A thing that is built and a thing that is connected are different facts, and
    the second one fails here first if the path was never free.
    """
    import importlib
    import pkgutil

    import app.web.views as views

    mine = {
        (method, route.path)
        for route in integrations.router.routes
        for method in sorted(getattr(route, "methods", ()) or ())
    }
    clashes = []
    for info in pkgutil.iter_modules(views.__path__):
        if info.name == "integrations":
            continue
        module = importlib.import_module(f"app.web.views.{info.name}")
        for route in getattr(module, "router", None).routes if hasattr(module, "router") else []:
            for method in sorted(getattr(route, "methods", ()) or ()):
                if (method, route.path) in mine:
                    clashes.append(f"{method} {route.path}: {info.name}")
    assert not clashes, "another router already owns: " + "; ".join(clashes)


# ------------------------------------------------------------------ helpers
#
# Both helpers are derived from the router rather than written by hand. A route
# added next month is swept for a leaked key and checked behind the gate without
# anybody remembering to add it here -- which is the failure the hand-written
# SCREENS list in tests/test_app_wiring.py had.


def _every_route(source_id: str = "SRC-does-not-exist") -> list[tuple[str, str]]:
    out = []
    for route in integrations.router.routes:
        path = route.path.replace("{source_id}", source_id)
        for method in sorted(getattr(route, "methods", ()) or ()):
            if method in ("GET", "POST"):
                out.append((method, path))
    return sorted(set(out))


def _form_for(path: str) -> dict:
    """Every field any form on this screen posts. Extra keys are ignored."""
    return {
        "name": "A source",
        "kind": SOURCE_REGISTRATION_KIND_MCP_SERVER,
        "endpoint": "https://mcp.acme.example/sse",
        "docket": "",
        "credential_ref": "",
    }
