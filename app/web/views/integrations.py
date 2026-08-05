"""The integrations registry: every place this workspace draws documents from.

WHAT THIS SCREEN IS FOR, AND IT IS NOT WHAT IT LOOKS LIKE. A regulated buyer
asks two questions of a product that reads their filings: where does your data
come from, and who can change that. This page is the answer to both. It is a
control surface -- add, edit, enable, disable, remove, each one audited -- and
an evidence surface: who registered each source, when anything last reached it,
and what happened. It is not a settings page, and it is read by a security
reviewer more often than by the administrator who filled it in.

THE HARD PART IS TELLING THE TRUTH, AND THE TRUTH IS AWKWARD. NOTHING IN THIS
PRODUCT FETCHES ANYTHING. There is no crawler, no poller, no scheduler;
ScheduledRun is a table of cadences with no fetcher behind it. A registry that
rendered rows as if they were live feeds would be the exact defect this
repository keeps finding, so this screen is built to make that impossible:

* app/state/sources.FETCHES_NOTHING is printed at the top of the page, in words
  a buyer can quote, not in a comment.
* Every row shows STATUS and ENABLED as two separate facts, because they are
  two columns and neither implies the other. Today every honest row reads
  "enabled" and "this build cannot fetch from that kind".
* The screen asks sources.can_fetch() rather than assuming, and asks it in one
  place. It answers no for every kind, because FETCHABLE_SOURCE_KINDS is empty,
  so every row says no fetcher exists for its kind. The day one lands, that
  tuple gains an entry and these rows change without this file being edited.
* last_scanned_at is NULL on every row and renders as NEVER SCANNED rather than
  as an empty cell, because a blank reads as a quiet week.

THERE IS NO CONNECTION TEST, AND THE PAGE SAYS SO WITH ITS REASON. A button
that has this server fetch a URL an administrator typed is a server-side request
forgery surface: the cloud metadata address, the loopback interface, any
neighbour a name resolves to. A safe one needs the resolved address checked
against every private range, redirects refused, the socket pinned to the checked
address and a timeout, and none of that is built. Refusing is the better answer.
What is offered instead is a preflight that reads the text of an endpoint and
says whether a fetcher would be permitted to reach it -- it opens no socket,
resolves no name, and says so in its own words on every verdict.

THE ROWS ARE REAL, OR THE PAGE SAYS THERE ARE NONE. data/real holds 102 public
filings from eight commissions, each with a provenance file naming the URL it
came from. One button registers those eight as public dockets, idempotently, so
the registry describes where this workspace corpus actually came from. Each row
then carries two counts that are deliberately not merged: how many filings on
disk that commission accounts for, and how many document versions in this
database point at the registration. They disagree today, because
scripts/ingest_real.py drops every provenance field, and the page says that
rather than hiding it behind one tidy number.

A KEY IS NEVER SHOWN BACK, because a key is never held. The row stores the NAME
of an environment variable. This screen shows the name, and whether that
variable is set in this process, and nothing else -- not the value, not its
length, not a prefix. The allow-list on which names may be referenced lives in
app/state/sources.py, which is also where a refusal is worded so that it never
repeats what was submitted: the most likely wrong value in that box is the key
itself, and an error message that echoes its input is how a key reaches a page.

WHERE THE WRITES HAPPEN. Not here. Every act is a function in
app/state/sources.py, which gates on app/auth/policy.py and writes the audit row
itself. This module resolves the tenant, checks the reader may administer
sources so it can render a refusal rather than a stack trace, and renders what
the write layer decided in the write layer own words.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

# THE MODULE, NOT THE NAMES. app/web/views/admin.py gives the reasoning: policy
# resolves its approval mode at import and the tests reload it, so an `except`
# bound to a class captured at import time stops catching the refusal the
# reloaded module raises, and a 403 becomes a 500.
from app.auth import policy
from app.state import sources
from app.state.db import session_scope
from app.state.identity import users_for_company
from app.state.models import (
    SOURCE_REGISTRATION_KINDS,
    SOURCE_REGISTRY_PERMISSION,
)
from app.web import TEMPLATES_DIR
from app.web.deps import current_user
from app.web.templating import build_templates
from app.web.views.proceedings import chrome, current_company_id, unresolved_escalations

router = APIRouter()
templates = build_templates()

# /admin is already where the gated screens live -- workflows, shares, invites,
# users -- and a second prefix for the same kind of thing would be a menu
# nobody can guess. The path is `sources` and not `integrations` because that is
# the word the rows use.
SOURCES_URL = "/admin/sources"
ADD_URL = SOURCES_URL + "/add"
CORPUS_URL = SOURCES_URL + "/register-corpus"

# The permission every path here sits behind. Taken from the constant rather
# than spelt, so the day source.manage lands in app/state/identity.py one line
# changes and no call site does.
MANAGE = SOURCE_REGISTRY_PERMISSION

REFUSED = (
    f"{MANAGE} is required to read or change where this workspace gets its "
    "documents. Ask an administrator. This screen is the record of what the "
    "product is allowed to read and who decided that, which is why it sits with "
    "the people who manage accounts."
)

# Read by the responsive test in tests/test_sources.py, which holds this file to
# the rules tests/test_responsive.py enforces on every other template. Its
# `<style>` block is reached by neither of that file scans.
TEMPLATE = "integrations.html"
TEMPLATE_DIR = TEMPLATES_DIR

PAGE_TITLE = "Integrations"

# What the last click did, chosen from an allow-list. The query string arrives
# from a redirect and is display only: a forged one can pick a sentence that
# exists and nothing else, and the table under it is read from the rows.
_NOTES = {
    "registered": "The source was registered. Nothing has fetched from it, because nothing here fetches.",
    "updated": "The source was updated.",
    "enabled": "The source is enabled. That is what this company wants of it, not a statement that it can be reached.",
    "disabled": "The source is disabled. Its status is unchanged, because status is what is true and enabled is what was wanted.",
    "removed": "The source was removed. No document version pointed at it.",
    "corpus": "The commissions the filings in data/real came from are registered below.",
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One place a source names, and what can be said about it without contact."""

    url: str
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One registration, with everything an administrator has to judge it.

    `status_words` and `enabled` are separate fields on purpose and are rendered
    apart. Folding them into one word is the failure app/state/models.py names.
    """

    source_id: str
    name: str
    kind: str
    kind_words: str
    status: str
    status_words: str
    status_badge: str
    enabled: bool
    fetchable: bool
    endpoints: tuple[Endpoint, ...]
    docket: str
    credential_ref: str
    credential_state: str
    created_by: str
    created_at: str
    last_scanned: str
    last_result: str
    documents_here: int | None
    documents_by_address: int | None
    documents_on_disk: int | None
    corpus_origins: tuple[str, ...]
    corpus_dockets: tuple[str, ...]
    corpus_span: str


# The assistant owns the pinned model id and the variable name it reads.
# Imported rather than restated: a second spelling of ANTHROPIC_API_KEY here
# would report on a variable nothing actually reads.
from app.chat.agent import ENV_API_KEY, MODEL_ID  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _user_id(request: Request) -> str:
    principal = current_user(request)
    return principal.user_id if principal is not None else ""


def _may_manage(session, company_id: str, user_id: str) -> str | None:
    """None when allowed, else the reason. The refusal is audited on the way out.

    require() writes the audit row and then raises. The raise is caught HERE,
    inside the caller session scope, so the row commits with the response.
    Catching it outside would roll the refusal back with everything else, and a
    denial nobody recorded is a denial nobody can review.
    """
    try:
        policy.require(session, company_id, user_id, MANAGE)
    except policy.PermissionDenied as denied:
        return denied.reason
    return None


def _waiting(session, company_id: str) -> int:
    """The nav held-for-review count. Zero on a database with no tables.

    Read on the refusal path too: a masthead that drops its count on one screen
    tells the reader the queue emptied, and it did not.
    """
    try:
        return len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        return 0


def _people(session, company_id: str) -> dict[str, str]:
    """user id -> the address a person reads a year later, not the display name."""
    try:
        return {user.id: user.email for user in users_for_company(session, company_id)}
    except SQLAlchemyError:
        return {}


def _rows(session, company_id: str) -> tuple[list[SourceRow], str, bool]:
    """The registry, a sentence naming what could not be read, and whether it was.

    THE BANNER IS THE FALLBACK ANNOUNCING ITSELF. Two things here can fail on a
    deployment whose schema is behind the code: the registrations table may not
    exist, and the provenance columns may not be on document_versions. Either
    one silently rendering as an empty registry or a confident zero would be
    this screen making a false statement about where the data comes from.

    THE THIRD RETURN VALUE IS WHY AN EMPTY LIST IS EMPTY, and it is not a
    nicety. "No source is registered for this company" and "the table this page
    reads is not in this database" are different statements, and the second one
    rendering as the first is precisely the failure this screen exists to refuse
    -- an absence presented as a fact.
    """
    try:
        registrations = sources.source_registrations_for_company(
            session, company_id=company_id
        )
    except SQLAlchemyError as unreadable:
        return [], (
            "The source registry could not be read from this database "
            f"({unreadable.__class__.__name__}). That is a schema behind the "
            "code, not an empty registry: this page cannot say where the "
            "documents come from until deploy/entrypoint.sh runs the migration "
            "that creates the source_registrations table. Nothing below is a "
            "statement about this company."
        ), False

    counted: sources.ProvenanceCounts | None
    warning = ""
    try:
        counted = sources.provenance_counts(session, company_id=company_id)
    except SQLAlchemyError as unreadable:
        counted = None
        warning = (
            "How many documents each source accounts for could not be read "
            f"({unreadable.__class__.__name__}). The provenance columns are not "
            "on document_versions in this database, so the counts below are "
            "blank rather than zero: this screen does not know, and will not "
            "print a number it did not read."
        )

    on_disk = {entry.key: entry for entry in sources.corpus_sources()}
    people = _people(session, company_id)

    rows: list[SourceRow] = []
    for row in registrations:
        config = row.config or {}
        corpus = on_disk.get(config.get(sources.CORPUS_KEY))
        rows.append(
            SourceRow(
                source_id=row.id,
                name=row.name,
                kind=row.kind,
                kind_words=sources.KIND_WORDS.get(row.kind, row.kind),
                status=row.status,
                status_words=sources.STATUS_WORDS.get(row.status, row.status),
                status_badge=sources.STATUS_BADGE.get(row.status, "badge--draft"),
                enabled=bool(row.enabled),
                # Asked, never assumed, and asked in one place. See the head
                # of this module and sources.can_fetch().
                fetchable=sources.can_fetch(row.kind),
                endpoints=tuple(
                    Endpoint(url=url, allowed=verdict.allowed, reason=verdict.reason)
                    for url, verdict in (
                        (url, sources.endpoint_preflight(url))
                        for url in sources.endpoints(config)
                    )
                ),
                docket=str(config.get("docket") or ""),
                credential_ref=row.credential_ref or "",
                credential_state=_credential_state(row.credential_ref),
                created_by=people.get(
                    row.created_by_user_id, row.created_by_user_id or "unknown"
                ),
                created_at=_stamp(row.created_at),
                # NULL on every row, and it says so in words. A blank cell here
                # would read as a scan that found nothing.
                last_scanned=_stamp(row.last_scanned_at) or sources.NEVER_SCANNED,
                last_result=row.last_result or "",
                documents_here=(
                    None if counted is None else counted.by_registration.get(row.id, 0)
                ),
                # Derived from the URL each version already carries, and
                # labelled as derived wherever it is shown. It is the only
                # count that can be non-zero today, because nothing writes the
                # stored link yet -- and a number with no note saying how it
                # was reached is how a derivation becomes a claim.
                documents_by_address=(
                    None
                    if counted is None
                    else sum(
                        counted.by_origin.get(place, 0)
                        for place in sources.endpoints(config)
                    )
                ),
                documents_on_disk=corpus.documents if corpus else None,
                corpus_origins=corpus.origins if corpus else (),
                corpus_dockets=corpus.dockets if corpus else (),
                corpus_span=(
                    f"{corpus.earliest_filing} to {corpus.latest_filing}"
                    if corpus and corpus.earliest_filing
                    else ""
                ),
            )
        )
    return rows, warning, True


def _credential_state(credential_ref: str | None) -> str:
    """Whether the named variable is set. Never anything about its value."""
    known = sources.credential_is_set(credential_ref)
    if known is None:
        return ""
    return sources.CREDENTIAL_RESOLVES if known else sources.CREDENTIAL_MISSING


def _model_status() -> dict:
    """Whether a model is reachable, said as a boolean and a variable name.

    THE SAME RULE app/state/sources.py::credential_is_set MAKES FOR A SOURCE
    CREDENTIAL, applied to the one secret this product reads directly. It reports
    that a variable is SET. Never its value, never its length, never a prefix,
    never a masked form with the last four showing. Two different answers to "how
    much of a secret may a screen show" is how the looser one ends up in front of
    somebody, so there is one answer and this is it.

    WHY THIS SCREEN AND NOT A SETTINGS FORM. An administrator cannot type a key
    in. A provider key held in the database is a key in every backup and inside
    the blast radius of any read bug, and this database deliberately holds no
    recoverable secret -- passwords and share tokens are one-way hashes. Worse,
    whoever sets the key chooses the endpoint, and every prompt carries obligation
    titles, owner names and account ids; that would quietly turn user.manage into
    "read this company's context off the wire". So the value stays in the
    environment and this screen reports what the environment says.

    READ ON EVERY RENDER, never cached at import. A key added to a running
    container has to show without a restart, and one removed has to stop showing.
    Deciding once at start and never looking again is the exact failure
    app/config.py exists because of.
    """
    configured = bool(os.environ.get(ENV_API_KEY, "").strip())
    return {
        "variable": ENV_API_KEY,
        "configured": configured,
        "model": MODEL_ID,
        # The literal line to add, so nobody has to guess the spelling of a
        # variable name that fails silently when it is wrong.
        "example_line": f"{ENV_API_KEY}=sk-ant-...",
    }


def _shell(company_id: str, waiting: int) -> dict:
    """base.html context plus what every answer this screen gives has to carry.

    The refusal page and the registry both name the permission and both link
    back here, so those live in one builder rather than in two branches that
    could come to disagree about which code opens the screen.
    """
    context = chrome(
        company_id,
        page_title=PAGE_TITLE,
        # Not one of the six screens in the masthead. Marking one of them as
        # current while an administrator is here would be a small lie in an
        # aria-current attribute.
        nav_active="",
        review_count=waiting,
    )
    context.update(
        {
            "model_status": _model_status(),
            "permission": MANAGE,
            "refused": REFUSED,
            "reason": None,
            "sources_url": SOURCES_URL,
            "add_url": ADD_URL,
            "corpus_url": CORPUS_URL,
        }
    )
    return context


def _forbidden(
    request: Request, company_id: str, reason: str, waiting: int
) -> HTMLResponse:
    """The refusal page, rendered from a decision already made and already audited.

    IT ASKS NOTHING FURTHER OF THE DATABASE, and that is the point. An earlier
    version of this file re-ran the permission check while rendering the refusal
    for a POST, so one refused click wrote TWO access.denied rows and one
    decision read as two in the chain. policy.require() audits at the moment it
    refuses; anything after that is rendering.
    """
    context = _shell(company_id, waiting)
    context["reason"] = reason
    return templates.TemplateResponse(request, TEMPLATE, context, status_code=403)


def _screen(
    request: Request,
    *,
    status_code: int = 200,
    refusal: str = "",
    note: str = "",
) -> HTMLResponse:
    """The whole page, in one place, whatever brought the reader here.

    ONE TEMPLATE, THREE ANSWERS -- the registry, a refusal from the write layer
    rendered above it, and the permission refusal instead of it -- following
    shares_admin.html. A separate page for each would be three files describing
    one screen, and the sentence a refused reader most needs is the same in all
    of them.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, rows, waiting, warning = None, [], 0, ""
    readable = True
    provenance = None
    scan = sources.corpus_scan()

    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, user_id)
            if reason is None:
                rows, warning, readable = _rows(session, company_id)
                provenance = _provenance(session, company_id)
    except SQLAlchemyError:
        # A database that cannot be read is a permission check that did not run,
        # and a check that did not run is not a check that passed. On a clone
        # with no tables this screen would otherwise open for anybody, empty --
        # which shows no data and is still the gate failing open.
        reason = (
            "this company roles could not be read, so nothing confirmed that "
            f"you hold {MANAGE}"
        )
        provenance = None

    if reason is not None:
        return _forbidden(request, company_id, reason, waiting)

    context = _shell(company_id, waiting)
    context.update(
        {
            "rows": rows,
            "warning": warning,
            # Why an empty table is empty. See _rows().
            "registry_readable": readable,
            "refusal": refusal,
            "note": note,
            "provenance": provenance,
            "kinds": [
                {
                    "code": kind,
                    "words": sources.KIND_WORDS[kind],
                    "fetchable": sources.can_fetch(kind),
                    "takes_credential": kind in sources.KINDS_TAKING_A_CREDENTIAL,
                }
                for kind in SOURCE_REGISTRATION_KINDS
            ],
            "corpus": scan,
            "corpus_registered": sum(1 for row in rows if row.documents_on_disk is not None),
            # Every sentence the screen makes a claim with comes from the write
            # layer, so the page and the module cannot come to disagree.
            "fetches_nothing": sources.FETCHES_NOTHING,
            "never_scanned": sources.NEVER_SCANNED,
            "connection_test_refusal": sources.CONNECTION_TEST_REFUSAL,
            "handshake_note": sources.HANDSHAKE_IS_NOT_A_FETCH,
            "corpus_caveat": sources.CORPUS_COUNT_CAVEAT,
            "provenance_gap": sources.PROVENANCE_GAP,
            "form_not_refilled": sources.FORM_NOT_REFILLED,
            "credential_prefix": sources.CREDENTIAL_REF_PREFIX,
            "matched_by_address": sources.MATCHED_BY_ADDRESS,
            "points_at_registration": sources.POINTS_AT_REGISTRATION,
        }
    )
    return templates.TemplateResponse(request, TEMPLATE, context, status_code=status_code)


def _provenance(session, company_id: str):
    """The three provenance figures, or None when they could not be read."""
    try:
        return sources.provenance_counts(session, company_id=company_id)
    except SQLAlchemyError:
        return None


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


@router.get(SOURCES_URL, response_class=HTMLResponse)
def registry(request: Request, did: str = "") -> HTMLResponse:
    """Every source this workspace draws documents from, and what is true of it."""
    return _screen(request, note=_NOTES.get(did, ""))


def _write(request: Request, act, *, done: str) -> HTMLResponse | RedirectResponse:
    """Run one act from app/state/sources.py and render whatever it decided.

    Four outcomes, and they are four different HTTP answers on purpose.

    * The reader may not administer sources: 403, audited by policy.require().
    * The id names nothing in this company: 404, and an id belonging to another
      tenant is indistinguishable from one that was never issued.
    * The write layer refused: 400, or 409 when the refusal is that something
      still points at the row. Its own sentence is rendered, not paraphrased --
      this screen did not make that decision and must not explain it in words of
      its own.
    * It landed: 303, so a reload cannot post it twice.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason = refusal = ""
    missing = False
    status = 400
    waiting = 0

    with session_scope() as session:
        # Read on the refusal path too: a masthead that drops its count on one
        # screen tells the reader the queue emptied, and it did not.
        waiting = _waiting(session, company_id)
        denied = _may_manage(session, company_id, user_id)
        if denied is not None:
            reason = denied
        else:
            try:
                act(session, company_id, user_id)
            except LookupError:
                missing = True
            except sources.SourceInUse as in_use:
                refusal, status = in_use.reason, 409
            except sources.SourceRefused as refused:
                refusal, status = refused.reason, 400
            except policy.PermissionDenied as late:
                # Unreachable through this screen, which already required the
                # permission -- and caught rather than assumed unreachable,
                # because the day the two rules disagree the honest answer is
                # the refusal and not a 500.
                reason = late.reason

    if reason:
        # The refusal page, not another pass over the gate. policy.require()
        # already wrote the access.denied row inside the scope above, and
        # re-checking here would write a second one for one refused click.
        return _forbidden(request, company_id, reason, waiting)
    if missing:
        raise HTTPException(status_code=404, detail="no such source")
    if refusal:
        return _screen(request, status_code=status, refusal=refusal)
    return RedirectResponse(url=f"{SOURCES_URL}?did={done}", status_code=303)


@router.post(ADD_URL)
def add_source(
    request: Request,
    name: str = Form(default=""),
    kind: str = Form(default=""),
    endpoint: str = Form(default=""),
    docket: str = Form(default=""),
    credential_ref: str = Form(default=""),
):
    """Register a place documents could come from. Fetches nothing, ever."""

    def act(session, company_id, user_id):
        sources.register_source(
            session,
            company_id=company_id,
            user_id=user_id,
            name=name,
            kind=kind,
            config={"endpoint": endpoint, "docket": docket},
            credential_ref=credential_ref,
            now=_now(),
        )

    return _write(request, act, done="registered")


@router.post(SOURCES_URL + "/{source_id}/edit")
def edit_source(
    request: Request,
    source_id: str,
    name: str = Form(default=""),
    endpoint: str = Form(default=""),
    docket: str = Form(default=""),
    credential_ref: str = Form(default=""),
):
    """Change what a source says about itself. Not its kind -- see update_source."""

    def act(session, company_id, user_id):
        sources.update_source(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            name=name,
            config={"endpoint": endpoint, "docket": docket},
            credential_ref=credential_ref,
            now=_now(),
        )

    return _write(request, act, done="updated")


@router.post(SOURCES_URL + "/{source_id}/enable")
def enable_source(request: Request, source_id: str):
    """What this company wants of the source. Never a claim that it is reachable."""

    def act(session, company_id, user_id):
        sources.set_source_enabled(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            enabled=True,
            now=_now(),
        )

    return _write(request, act, done="enabled")


@router.post(SOURCES_URL + "/{source_id}/disable")
def disable_source(request: Request, source_id: str):
    """Stop using a source. Its status is not touched, because status is fact."""

    def act(session, company_id, user_id):
        sources.set_source_enabled(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            enabled=False,
            now=_now(),
        )

    return _write(request, act, done="disabled")


@router.post(SOURCES_URL + "/{source_id}/remove")
def remove_source(request: Request, source_id: str):
    """Delete a registration nothing points at. Refused, with a count, otherwise."""

    def act(session, company_id, user_id):
        sources.remove_source(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            now=_now(),
        )

    return _write(request, act, done="removed")


@router.post(CORPUS_URL)
def register_corpus(request: Request):
    """Register the eight commissions the filings in data/real actually came from.

    Idempotent. The provenance file beside each filing names the URL it was
    fetched from, so these rows are read out of the corpus rather than typed,
    and the registry then describes where this workspace documents came from
    instead of demonstrating an empty table.
    """

    def act(session, company_id, user_id):
        sources.register_corpus_sources(
            session, company_id=company_id, user_id=user_id, now=_now()
        )

    return _write(request, act, done="corpus")


# ---------------------------------------------------------------------------
# HANDOFF -- what this module needs from files it does not own
#
# THE FIRST LINE, BECAUSE IT IS THE ONE THAT MATTERS: THIS ROUTER IS MOUNTED
# NOWHERE. app/main.py is not owned by this change, so the screen is built and
# not connected, which are different facts. Two lines are needed there, and
# tests/test_app_wiring.py::test_every_router_in_the_views_package_is_mounted
# fails until they land, because it derives its answer from this package rather
# than from a hand-written list:
#
#     from app.web.views import ( ... integrations, ... )
#     app.include_router(integrations.router)
#
# app/web/templates/base.html
#                     Nothing is required, following shares_admin.py: neither
#                     that registry nor this one is in the six-item masthead,
#                     and base.html own note says a nav item nobody can reach
#                     renders as dead text. The cost is real and should be said
#                     plainly: with no admin menu, /admin/sources is reachable
#                     only by typing the URL. When somebody adds that menu, this
#                     screen belongs in it beside /admin/shares, /admin/users
#                     and /admin/workflows.
#
# app/state/audit.py  The five ACTION_SOURCE_* constants declared in
#                     app/state/sources.py belong there beside every other
#                     action code. MOVE them rather than restate them: two
#                     spellings of one code is a row that verifies perfectly and
#                     that no query for that action ever returns.
#
# app/state/queries.py
#                     The four scoped reads in app/state/sources.py belong
#                     there beside versions_for_company, unchanged --
#                     company_id is already keyword-only with no default.
#
# app/state/identity.py
#                     source.manage does not exist. This screen gates on
#                     user.manage, which admin holds and which describes
#                     managing accounts rather than registering data sources.
#                     Adding the exact code needs PERMISSION_CODES,
#                     PERMISSION_DESCRIPTIONS and SYSTEM_ROLE_PERMISSIONS
#                     together, or tests/test_identity.py fails on a code no
#                     role grants. app/state/sources.SOURCE_REGISTRY_PERMISSION
#                     is the single name to change when it lands.
#
# scripts/ingest_real.py
#                     It reads each provenance file for a display label and
#                     throws away source_url, filer, filing_date and
#                     retrieved_at. The columns now exist. Until it writes them
#                     and points source_registration_id at the row this screen
#                     registers, every count of "documents in this workspace"
#                     here is honestly zero, and the page says so.
# ---------------------------------------------------------------------------


__all__ = [
    "ADD_URL",
    "CORPUS_URL",
    "MANAGE",
    "PAGE_TITLE",
    "REFUSED",
    "SOURCES_URL",
    "TEMPLATE",
    "TEMPLATE_DIR",
    "Endpoint",
    "SourceRow",
    "router",
    "templates",
]
