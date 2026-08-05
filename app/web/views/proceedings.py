"""Two screens: the proceedings list, and one proceeding's versions and changes.

Thin, per docs/architecture.html. Every read here goes through the tenant-scoped
functions in app/state/, nothing decides anything, and nothing writes.

Three things in this module are shared with the review screen and live here
rather than in a copy: current_company_id(), chrome(), and the claim-to-change
map. They are imported by app/web/views/review.py for the same reason
app/state/claims.py imports _require_scope instead of restating it -- two copies
of a tenant decision drift, and the point of a chokepoint is that there is one.

The empty case is a feature, not a fallback. A list screen that renders an empty
table when nothing is loaded is asserting "nothing has changed", which is a
claim, and one the product has not checked. Absence is denial applies to the
interface exactly as it applies to a citation: when there is nothing to show,
the screen says which of the two facts it is -- no tables yet, or tables with no
rows -- and how to fix it.
"""

import os
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from app.state.claims import (
    changes_for_proceeding,
    claims_for_change,
    escalations_for_company,
    proceedings_for_company,
)
from app.state.db import session_scope
from app.state.models import Change, DocumentVersion, Proceeding
from app.state.queries import versions_for_company
from app.web.deps import (
    COMPANY_ENV,
    COMPANY_NAME_ENV,
    DEMO_COMPANY_ID,
    current_company,
)
from app.web.templating import build_templates

router = APIRouter()
templates = build_templates()

# The tenant and the masthead name are decided in app/web/deps.py and re-exported
# here under the names these screens already used. One answer, two spellings, no
# second copy to drift.
DEFAULT_COMPANY_ID = DEMO_COMPANY_ID

# What a screen found when it looked. Three different facts, three different
# sentences: the schema is absent, the schema is there and empty, or there is
# something to show.
STATE_NO_TABLES = "no-tables"
STATE_NO_ROWS = "no-rows"
STATE_OK = "ok"

# The remedy, in the words a reviewer types. /healthz says the same thing in its
# own words; both are short enough that a shared constant would cost more than
# it saves.
SEED_COMMAND = "python -m app.seed"
MAKE_COMMAND = "make seed"

_STATUS_BADGE = {"DRAFT": "badge--draft", "FINAL": "badge--final"}


def current_company_id() -> str:
    """The tenant every read on these screens is scoped by.

    Delegates to app.web.deps.current_company() rather than reading the
    environment a second time. The change screen resolves its tenant through
    that same function as a FastAPI dependency, and when this module answered
    the question on its own the two disagreed the moment a deployment named a
    company: these screens showed nothing and the change screen still served the
    demo tenant's source text.
    """
    return current_company()


def company_display_name() -> str:
    """The name shown in the masthead, or "" when nobody configured one.

    Empty is the honest answer: there is no company table in this schema, so a
    name the product has not been told is a name it should not invent. The
    caller falls back to the tenant id, which is a fact.
    """
    return os.environ.get(COMPANY_NAME_ENV, "").strip()


def chrome(
    company_id: str,
    *,
    page_title: str,
    nav_active: str,
    review_count: int = 0,
    nav_proceeding_url: str | None = None,
    nav_change_url: str | None = None,
) -> dict:
    """The context base.html reads. One builder, so every screen agrees.

    When no display name is configured the masthead shows the tenant id as the
    name and drops the coordinate beside it, rather than printing "MEP MEP".
    """
    name = company_display_name()
    return {
        "page_title": page_title,
        "company_name": name or company_id,
        "company_id": company_id if name else None,
        "tenant": company_id,
        "nav_active": nav_active,
        "review_count": review_count,
        "nav_proceeding_url": nav_proceeding_url,
        "nav_change_url": nav_change_url,
        "seed_command": SEED_COMMAND,
        "make_command": MAKE_COMMAND,
    }


def unresolved_escalations(session, company_id: str) -> list:
    """The company's open refusals. Used for the count on every screen's nav."""
    return escalations_for_company(session, company_id, unresolved_only=True)


def claim_owners(session, company_id: str, proceeding_id: str) -> dict[str, str]:
    """claim id -> change id, for one proceeding, through the scoped reads only.

    One query per change rather than one query for the lot. That is an N+1 and
    it is deliberate: claims_for_change() is the audited chokepoint, and a
    second unscoped query written here to save a round trip would be a second
    place tenancy could be got wrong. The corpus has twenty-seven changes, and
    when that number grows the fix belongs in app/state/claims.py where the
    scope is enforced, not here.
    """
    owners: dict[str, str] = {}
    for change in changes_for_proceeding(session, company_id, proceeding_id):
        for claim in claims_for_change(session, company_id, change.id):
            owners[claim.id] = change.id
    return owners


def version_chain(
    versions: list[DocumentVersion], changes: list[Change]
) -> list[DocumentVersion]:
    """Versions in the order the stored diffs chain them, newest last.

    The order comes from the edges the diff actually recorded, not from sorting
    the ids. Sorting ids reads correctly for v1, v2, v3 and then puts v10 before
    v2 on the day a docket runs to ten filings, which would quietly relabel the
    wrong version "latest" on the front screen.

    A history that does not form one chain -- a fork, a gap, an id in an edge
    that is not a stored version -- falls back to id order rather than guessing
    at a sequence. The fallback is the honest answer and the caller renders the
    same table either way; what it must not do is invent an ordering.
    """
    by_id = {version.id: version for version in versions}
    by_id_order = sorted(by_id.values(), key=lambda version: version.id)

    successor: dict[str, str] = {}
    predecessor: dict[str, str] = {}
    for change in changes:
        if change.from_version_id not in by_id or change.to_version_id not in by_id:
            return by_id_order
        successor[change.from_version_id] = change.to_version_id
        predecessor[change.to_version_id] = change.from_version_id

    heads = [vid for vid in by_id if vid not in predecessor]
    if len(heads) != 1:
        return by_id_order

    chain: list[DocumentVersion] = []
    seen: set[str] = set()
    cursor: str | None = heads[0]
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        chain.append(by_id[cursor])
        cursor = successor.get(cursor)

    return chain if len(chain) == len(by_id) else by_id_order


@dataclass(frozen=True, slots=True)
class ProceedingRow:
    """One line on the front screen. Plain data: the templates see no ORM rows."""

    id: str
    docket: str
    commission: str
    subject: str
    url: str
    versions: int
    latest_id: str | None
    latest_label: str | None
    latest_status: str | None
    latest_badge: str | None
    held: int


@dataclass(frozen=True, slots=True)
class VersionStop:
    id: str
    label: str
    status: str
    badge: str | None
    arrivals: int
    is_latest: bool


@dataclass(frozen=True, slots=True)
class PairOption:
    from_id: str
    to_id: str
    label: str
    url: str
    count: int
    is_current: bool


@dataclass(frozen=True, slots=True)
class ChangeRow:
    id: str
    url: str
    change_type: str
    section: str
    status: str
    badge: str | None
    claims: int
    held: int


def _badge(status: str) -> str | None:
    """The badge class for a status, or None for a word we were not taught.

    None renders the status as a plain badge with its own text. An unknown
    status must not borrow the look of a final order.
    """
    return _STATUS_BADGE.get(status)


def _rows(session, company_id: str) -> list[ProceedingRow]:
    proceedings = proceedings_for_company(session, company_id)
    if not proceedings:
        return []

    versions = versions_for_company(session, company_id)
    by_docket: dict[str, list[DocumentVersion]] = {}
    for version in versions:
        by_docket.setdefault(version.docket, []).append(version)

    held_claim_ids = {row.claim_id for row in unresolved_escalations(session, company_id)}

    rows: list[ProceedingRow] = []
    for proceeding in proceedings:
        changes = changes_for_proceeding(session, company_id, proceeding.id)
        chain = version_chain(by_docket.get(proceeding.docket, []), changes)
        latest = chain[-1] if chain else None

        # Only pay for the claim map when something is actually waiting.
        held = 0
        if held_claim_ids:
            owners = claim_owners(session, company_id, proceeding.id)
            held = sum(1 for claim_id in held_claim_ids if claim_id in owners)

        rows.append(
            ProceedingRow(
                id=proceeding.id,
                docket=proceeding.docket,
                commission=proceeding.commission,
                subject=proceeding.subject,
                url=f"/proceedings/{proceeding.id}",
                versions=len(chain),
                latest_id=latest.id if latest else None,
                latest_label=latest.label if latest else None,
                latest_status=latest.status if latest else None,
                latest_badge=_badge(latest.status) if latest else None,
                held=held,
            )
        )
    return rows


def _pair_counts(
    changes: list[Change], chain: list[DocumentVersion]
) -> list[tuple[tuple[str, str], int]]:
    """The version pairs Strata actually diffed, in chain order, with counts.

    Derived from the stored changes rather than from every combination of two
    versions. A pair nothing was diffed against is not a view of this
    proceeding; offering it would promise an answer that was never computed.
    """
    counts: dict[tuple[str, str], int] = {}
    for change in changes:
        key = (change.from_version_id, change.to_version_id)
        counts[key] = counts.get(key, 0) + 1

    position = {version.id: index for index, version in enumerate(chain)}
    ordered = sorted(counts, key=lambda key: (position.get(key[0], len(chain)), key))
    return [(key, counts[key]) for key in ordered]


@router.get("/proceedings", response_class=HTMLResponse)
def proceedings_index(request: Request) -> HTMLResponse:
    """What this company tracks, and where something has moved."""
    company_id = current_company_id()

    try:
        with session_scope() as session:
            rows = _rows(session, company_id)
            waiting = len(unresolved_escalations(session, company_id))
        state = STATE_OK if rows else STATE_NO_ROWS
    except SQLAlchemyError:
        # A clone that has never been seeded has no tables. That is not a fault
        # and it is not an empty list either, so it gets its own sentence.
        rows, waiting, state = [], 0, STATE_NO_TABLES

    context = chrome(
        company_id,
        page_title="Proceedings",
        nav_active="proceedings",
        review_count=waiting,
    )
    context.update({"rows": rows, "state": state})
    return templates.TemplateResponse(request, "proceedings.html", context)


@router.get("/proceedings/{proceeding_id}", response_class=HTMLResponse)
def proceeding_detail(
    request: Request,
    proceeding_id: str,
    from_version: str | None = Query(default=None, alias="from"),
    to_version: str | None = Query(default=None, alias="to"),
) -> HTMLResponse:
    """One proceeding: its versions in order, and the changes between a pair.

    Another company's proceeding id is a 404, not a redirect and not an empty
    page. The message says nothing about whether the id exists elsewhere.
    """
    company_id = current_company_id()
    requested = (from_version, to_version) if from_version and to_version else None

    try:
        with session_scope() as session:
            proceeding = _find(
                proceedings_for_company(session, company_id), proceeding_id
            )
            if proceeding is None:
                raise HTTPException(status_code=404, detail="no such proceeding")

            docket = proceeding.docket
            detail = {
                "id": proceeding.id,
                "docket": docket,
                "commission": proceeding.commission,
                "subject": proceeding.subject,
                "url": f"/proceedings/{proceeding.id}",
            }

            changes = changes_for_proceeding(session, company_id, proceeding.id)
            versions = [
                version
                for version in versions_for_company(session, company_id)
                if version.docket == docket
            ]
            chain = version_chain(versions, changes)
            counted = _pair_counts(changes, chain)

            known = {key for key, _count in counted}
            unknown_pair = requested is not None and requested not in known
            if requested in known:
                selected = requested
            elif requested is None and counted:
                # The most recent diff, because "what moved last" is the
                # question the analyst arrives with.
                selected = counted[-1][0]
            else:
                selected = None

            pairs = [
                PairOption(
                    from_id=from_id,
                    to_id=to_id,
                    label=f"{from_id} to {to_id}",
                    url=f"{detail['url']}?from={from_id}&to={to_id}",
                    count=count,
                    is_current=selected == (from_id, to_id),
                )
                for (from_id, to_id), count in counted
            ]
            arrivals = {pair.to_id: pair.count for pair in pairs}
            stops = [
                VersionStop(
                    id=version.id,
                    label=version.label,
                    status=version.status,
                    badge=_badge(version.status),
                    arrivals=arrivals.get(version.id, 0),
                    is_latest=index == len(chain) - 1,
                )
                for index, version in enumerate(chain)
            ]

            rows = (
                _change_rows(session, company_id, changes, selected) if selected else []
            )
            waiting = len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail="no such proceeding") from None

    context = chrome(
        company_id,
        page_title=detail["docket"],
        nav_active="proceeding",
        review_count=waiting,
        nav_proceeding_url=detail["url"],
    )
    context.update(
        {
            "proceeding": detail,
            "stops": stops,
            "pairs": pairs,
            "selected": selected,
            "rows": rows,
            "unknown_pair": unknown_pair,
            "requested": requested,
        }
    )
    return templates.TemplateResponse(request, "proceeding.html", context)


def _find(proceedings: list[Proceeding], proceeding_id: str) -> Proceeding | None:
    """Pick one out of the scoped list rather than writing a second query.

    proceedings_for_company() is the guard. Adding a by-id query here would add
    a second place to enforce the same scope, and one of the two would be the
    one nobody audited.
    """
    for proceeding in proceedings:
        if proceeding.id == proceeding_id:
            return proceeding
    return None


def _change_rows(
    session,
    company_id: str,
    changes: list[Change],
    selected: tuple[str, str],
) -> list[ChangeRow]:
    """The changes between one pair, with how many claims each carries.

    The held count says how many of those claims the product is currently
    refusing to assert. It is read from the open escalations rather than
    recomputed here, so this screen and the review queue cannot disagree about
    how many refusals there are.
    """
    held_claim_ids = {row.claim_id for row in unresolved_escalations(session, company_id)}
    from_id, to_id = selected

    rows: list[ChangeRow] = []
    for change in changes:
        if (change.from_version_id, change.to_version_id) != (from_id, to_id):
            continue
        claims = claims_for_change(session, company_id, change.id)
        rows.append(
            ChangeRow(
                id=change.id,
                url=f"/changes/{change.id}",
                change_type=change.change_type,
                section=change.section or "not numbered",
                status=change.status,
                badge=_badge(change.status),
                claims=len(claims),
                held=sum(1 for claim in claims if claim.id in held_claim_ids),
            )
        )
    return rows


__all__ = [
    "COMPANY_ENV",
    "COMPANY_NAME_ENV",
    "DEFAULT_COMPANY_ID",
    "MAKE_COMMAND",
    "SEED_COMMAND",
    "STATE_NO_ROWS",
    "STATE_NO_TABLES",
    "STATE_OK",
    "ChangeRow",
    "PairOption",
    "ProceedingRow",
    "VersionStop",
    "chrome",
    "claim_owners",
    "company_display_name",
    "current_company_id",
    "router",
    "templates",
    "unresolved_escalations",
    "version_chain",
]
