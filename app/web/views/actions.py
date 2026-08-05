"""The action queue: what this company proposes to do, and who signed it off.

THIS IS THE SCREEN THE APPROVAL CONTROL WAS BUILT FOR. app/auth/policy.py::
can_approve is the segregation-of-duties gate -- four checks, the last of which
refuses anybody the audit chain shows already acting on the claim underneath the
action. It was written, unit-tested and exported, and until this module existed
NOTHING IN THE PRODUCT CALLED IT. An independent review found that before we did.

Two earlier attempts wired it into app/web/views/review.py::resolve_escalation
and both were reverted. The reason is worth keeping, because it is the shape of
the mistake rather than a detail: gate 2 demands `action.approve`, escalation
resolution sits on ROLE_ANALYST, and the analyst does not hold `action.approve`.
Putting the gate there made the analyst's own screen refuse the analyst. The
right home was never the escalation queue. can_approve gates APPROVING AN
ACTION, and there was no screen where an action could be proposed at all --
docs/prd.html said so: "the monitor, comment and comply vocabulary exists in
code and no screen renders it". This module is that screen.

FOUR THINGS THIS FILE DECIDES, and each has a failure behind it.

**The gate is asked at the click, never at the render.** The buttons are drawn
for whoever holds `action.approve` -- policy.has(), the function written for
exactly that question. Holding the code is necessary and never sufficient, so
the answer that matters is taken inside the POST, against the live rows, at the
moment of the decision. A check made when the page was drawn is a statement
about the past.

**A refusal is answered, not raised.** policy.require raises PermissionDenied,
which is not an HTTPException; an uncaught one is a 500 with a traceback where a
decision belongs. Every other screen in this package catches it and answers 403
with the reason, and tests/test_tenancy_derived.py exists because this one did
not always. can_approve does not raise at all -- it returns a Verdict whose
__bool__ is the verdict, so the careless `if verdict:` and the careful
`allowed, reason = verdict` agree.

**A refusal is COMMITTED, which is not free.** can_approve and require both
append their ACCESS_DENIED row through the caller's session. Raising an
HTTPException from inside `with session_scope()` rolls that back: the person is
stopped and the record of stopping them is gone. So every outcome is decided
INSIDE the block and acted on OUTSIDE it, and the transaction closes cleanly
with the refusal in the chain. app/web/views/review.py still has the older
shape; this is the one to copy.

**The refusal is shown on the screen, not returned as a bare 403 body.** The
hardest technical decision in this product is meant to be visible in the
product. A person who tries to approve their own work should read the sentence
the gate produced -- which audit row proved it, and why anybody minds -- on the
page they were already looking at. The status code is still 403.

WHAT THIS SCREEN DOES NOT DO. Approving does not act. It records that the
company decided to comply, or to comment, or to monitor; nothing is filed,
nobody is told, and no project state moves. That is a real limit of a 48-hour
build and the page says so rather than implying otherwise.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

# The MODULE, not the names on it, for the reason app/web/views/review.py gives:
# tests/test_policy.py reloads app.auth.policy, and a reload rebinds every class
# in it -- so an imported PermissionDenied would be the class from before the
# reload while the raise used the one from after, and the except would stop
# catching a refusal.
from app.auth import policy
from app.interpretation.action import action_vocabulary
from app.state import actions as store
from app.state.claims import (
    change_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import session_scope
from app.web.deps import current_user
from app.web.templating import build_templates

# Imported, not restated. The tenant and the page shell are decided in one
# place; a second copy here would drift from the screens either side of it.
from app.web.views.proceedings import (
    STATE_NO_ROWS,
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    claim_owners,
    current_company_id,
    unresolved_escalations,
)

router = APIRouter()
templates = build_templates()

#: One spelling of the path, imported by anything that links here.
ACTIONS_URL = "/actions"

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)

#: The two codes this screen gates on, imported from where they are granted
#: rather than typed. store.PROPOSE sits on ROLE_ANALYST; policy.APPROVE sits on
#: ROLE_OBLIGATION_OWNER and on no other seeded role.
PROPOSE = store.PROPOSE
REJECT = "action.reject"

SIGN_IN = "sign in to act on a proposed action"
NO_SUCH_ACTION = "no such proposed action"
ALREADY_DECIDED = "this action was already decided"
REASON_REQUIRED = (
    "a rejection needs a reason; an unexplained one is not auditable"
)

#: Printed on the page, above the queue. The control has to be legible before
#: somebody meets it, not only in the refusal after they do.
SEGREGATION_NOTE = (
    "Holding the approval permission is not enough. Strata refuses an approval "
    "from anyone the audit chain shows already working on the claim underneath "
    "the action -- proposing it, judging the change, or raising an escalation "
    "against it. The person who interprets a change does not approve what "
    "follows from it."
)

#: The honest limit, on the page rather than only in a docstring.
NOT_AN_ACTION_NOTE = (
    "Approving records the decision. It files nothing, tells nobody and moves "
    "no project state; the integration that would do that is not built."
)


@dataclass(frozen=True, slots=True)
class ActionRow:
    """One proposal, flattened for rendering while the session is still open.

    Frozen and detached, for the reason app/web/deps.py::Principal gives: the
    ORM row belongs to a session that is closed by the time a template renders,
    and a detached row that lazily loads an attribute at render time fails in
    the one place nobody is watching.

    There is no statement field, deliberately. A claim's text is gated behind
    app/state/claims.py::verified_claims so nothing renders an assertion whose
    citation did not verify, and this queue is about the DECISION rather than
    the wording. What it shows instead is the claim id, the citation address and
    the section -- all checkable, none of them an assertion.
    """

    action_id: str
    claim_id: str
    change_id: str
    change_url: str
    section: str
    status: str
    kind: str
    rationale: str
    proposed_by: str
    proposed_at: str
    state: str
    decided_by: str | None
    decided_at: str | None
    decision_reason: str | None


@dataclass(frozen=True, slots=True)
class ProposableClaim:
    """A claim an action could be proposed on, and the words its status allows."""

    claim_id: str
    change_id: str
    change_url: str
    section: str
    status: str
    kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActionsPage:
    waiting: tuple[ActionRow, ...]
    settled: tuple[ActionRow, ...]
    proposable: tuple[ProposableClaim, ...]
    may_propose: bool
    may_approve: bool
    review_count: int
    state: str


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _row(session, company_id: str, action) -> ActionRow:
    change = change_for_company(session, company_id, action.change_id)
    return ActionRow(
        action_id=action.id,
        claim_id=action.claim_id,
        change_id=action.change_id,
        change_url=f"/changes/{action.change_id}",
        section=(change.section if change and change.section else "not numbered"),
        status=(change.status if change else "unknown"),
        kind=action.kind,
        rationale=action.rationale,
        proposed_by=action.proposed_by,
        proposed_at=_stamp(action.proposed_at) or "",
        state=action.state,
        decided_by=action.decided_by,
        decided_at=_stamp(action.decided_at),
        decision_reason=action.decision_reason,
    )


def _proposable(session, company_id: str, taken: set[str]) -> tuple[ProposableClaim, ...]:
    """Claims with no proposal on them yet, with the words each status allows.

    Read through app/state/claims.py's scoped functions, and it walks the
    company's proceedings rather than querying claims directly, so the tenant
    decision is made in the one place that already makes it.

    A claim whose citation does not verify is left out. Proposing that the
    company comply with a statement the product refuses to assert would be the
    product asserting it through the back door -- the escalation queue is where
    that claim goes, and it says so on the page.
    """
    out: list[ProposableClaim] = []
    for proceeding in proceedings_for_company(session, company_id):
        owners = claim_owners(session, company_id, proceeding.id)
        for change_id in sorted(set(owners.values())):
            change = change_for_company(session, company_id, change_id)
            if change is None:
                continue
            verified, _held = verified_claims(session, company_id, change_id)
            for claim in verified:
                if claim.claim_id in taken:
                    continue
                out.append(
                    ProposableClaim(
                        claim_id=claim.claim_id,
                        change_id=change_id,
                        change_url=f"/changes/{change_id}",
                        section=change.section or "not numbered",
                        status=change.status,
                        kinds=action_vocabulary(change.status),
                    )
                )
    return tuple(out)


def _page(session, company_id: str, user_id: str) -> ActionsPage:
    rows = store.proposed_actions_for_company(session, company_id)

    waiting = tuple(
        _row(session, company_id, row)
        for row in rows
        if row.state == store.STATE_PROPOSED
    )
    settled = tuple(
        _row(session, company_id, row)
        for row in rows
        if row.state != store.STATE_PROPOSED
    )

    # has(), never require(). This decides what to draw and gates nothing; the
    # gate is in the POST. app/web/templating.py argues the same split at
    # length for the admin menu.
    may_propose = bool(user_id) and policy.has(session, company_id, user_id, PROPOSE)
    may_approve = bool(user_id) and policy.has(
        session, company_id, user_id, policy.APPROVE
    )

    proposable = (
        _proposable(session, company_id, {row.claim_id for row in rows})
        if may_propose
        else ()
    )

    state = STATE_OK if (rows or proposable) else STATE_NO_ROWS
    return ActionsPage(
        waiting=waiting,
        settled=settled,
        proposable=proposable,
        may_propose=may_propose,
        may_approve=may_approve,
        review_count=len(unresolved_escalations(session, company_id)),
        state=state,
    )


def _context(company_id: str, page: ActionsPage, error: dict | None = None) -> dict:
    context = chrome(
        company_id,
        page_title="Actions",
        nav_active="actions",
        review_count=page.review_count,
    )
    context.update(
        {
            "waiting": page.waiting,
            "settled": page.settled,
            "proposable": page.proposable,
            "may_propose": page.may_propose,
            "may_approve": page.may_approve,
            "state": page.state,
            "error": error,
            "actions_url": ACTIONS_URL,
            "decision_approve": DECISION_APPROVE,
            "decision_reject": DECISION_REJECT,
            "segregation_note": SEGREGATION_NOTE,
            "not_an_action_note": NOT_AN_ACTION_NOTE,
            "approval_mode": policy.approval_mode(),
            "demo_mode": policy.approval_mode() != policy.SEGREGATED,
        }
    )
    return context


def _empty_page() -> ActionsPage:
    return ActionsPage(
        waiting=(),
        settled=(),
        proposable=(),
        may_propose=False,
        may_approve=False,
        review_count=0,
        state=STATE_NO_TABLES,
    )


def _render(request: Request, company_id: str, page: ActionsPage, error, status: int):
    return templates.TemplateResponse(
        request, "actions.html", _context(company_id, page, error), status_code=status
    )


@router.get(ACTIONS_URL, response_class=HTMLResponse)
def action_queue(request: Request) -> HTMLResponse:
    """What this company proposes to do about its changes, and what was decided."""
    company_id = current_company_id()
    principal = current_user(request)
    user_id = principal.user_id if principal else ""
    try:
        with session_scope() as session:
            page = _page(session, company_id, user_id)
    except SQLAlchemyError:
        page = _empty_page()

    return _render(request, company_id, page, None, 200)


@router.post(f"{ACTIONS_URL}/propose")
def propose(
    request: Request,
    claim_id: str = Form(default=""),
    kind: str = Form(default=""),
    rationale: str = Form(default=""),
):
    """Propose one action on the change under a claim. Gated on action.propose.

    Refusals answer with the sentence the gate or the store produced, and the
    transaction still commits, so the ACCESS_DENIED row policy.require appended
    survives the request. See the module docstring.
    """
    company_id = current_company_id()
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status_code=403, detail=SIGN_IN)
    actor = f"person:{principal.email}"

    refusal: str | None = None
    bad_request: str | None = None
    missing = False
    page: ActionsPage | None = None

    try:
        with session_scope() as session:
            try:
                policy.require(session, company_id, principal.user_id, PROPOSE)
            except policy.PermissionDenied as denied:
                refusal = denied.reason
            else:
                try:
                    store.propose_action(
                        session,
                        company_id,
                        claim_id=claim_id,
                        kind=kind,
                        rationale=rationale,
                        actor=actor,
                        actor_user_id=principal.user_id,
                    )
                except LookupError:
                    missing = True
                except ValueError as refused:
                    bad_request = str(refused)
            if refusal or bad_request:
                page = _page(session, company_id, principal.user_id)
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail=NO_SUCH_ACTION) from None

    if missing:
        raise HTTPException(status_code=404, detail=NO_SUCH_ACTION)
    if refusal is not None:
        return _render(
            request,
            company_id,
            page or _empty_page(),
            {"title": "You may not propose an action.", "body": refusal},
            403,
        )
    if bad_request is not None:
        return _render(
            request,
            company_id,
            page or _empty_page(),
            {"title": "That action was not written.", "body": bad_request},
            400,
        )

    return RedirectResponse(url=ACTIONS_URL, status_code=303)


@router.post(ACTIONS_URL + "/{action_id}/decide")
def decide(
    request: Request,
    action_id: str,
    decision: str = Form(default=""),
    reason: str = Form(default=""),
):
    """Approve or reject one proposed action. THE APPROVAL GATE IS HERE.

    Approving asks app/auth/policy.py::can_approve, which is the whole control:
    the user exists and is active, they hold action.approve, the id resolves to
    a claim in this company, and no audit row shows them already acting on that
    claim, the change beneath it, or an escalation raised against it. The
    Verdict is honoured rather than discarded -- a security check whose answer
    is thrown away fails open, and tests/test_approval_segregation_wired.py
    fails the suite on a bare-statement call for exactly that reason.

    REJECTING GOES THROUGH require(action.reject) AND NOT THROUGH can_approve,
    and that asymmetry is a decision rather than an oversight. The danger the
    control guards is somebody waving their own work through; refusing your own
    proposal takes nothing forward and is closer to withdrawing it. The cost is
    real and is conceded here: a person who holds action.reject can knock out a
    colleague's proposal and leave their own standing, and nothing in this
    build stops that. Widening the gate to cover rejection would need the
    argument written down first.

    THE ORDER: the proposal is resolved before the gate is asked, so another
    company's id is a 404 that reveals nothing. Within a tenant that means a
    colleague without action.approve learns the id exists -- which the queue
    above already shows to anybody signed in, so it is not a secret this route
    is keeping.
    """
    company_id = current_company_id()
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status_code=403, detail=SIGN_IN)
    if decision not in DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"decision must be {DECISION_APPROVE} or {DECISION_REJECT}",
        )

    actor = f"person:{principal.email}"
    note = " ".join((reason or "").split())

    refusal: str | None = None
    missing = False
    conflict = False
    needs_reason = False
    page: ActionsPage | None = None

    try:
        with session_scope() as session:
            row = store.proposed_action_for_company(session, company_id, action_id)
            if row is None:
                missing = True
            elif row.state != store.STATE_PROPOSED:
                conflict = True
            elif decision == DECISION_APPROVE:
                # The gate. Asked against the claim, because that is what
                # can_approve resolves: the claim, the change beneath it and
                # every escalation raised against it are all places a person's
                # judgement could already be written down.
                verdict = policy.can_approve(
                    session, company_id, principal.user_id, row.claim_id
                )
                if not verdict:
                    refusal = verdict.reason
                else:
                    store.record_decision(
                        session,
                        company_id,
                        row.id,
                        approved=True,
                        actor=actor,
                        actor_user_id=principal.user_id,
                        # The gate's own sentence, kept rather than paraphrased.
                        # Under DEMO_SELF_APPROVAL it is the waiver notice, so
                        # the record says the separation was waived rather than
                        # reading like a clean approval a year later.
                        reason=(f"{note} -- {verdict.reason}" if note else verdict.reason),
                    )
            else:
                try:
                    policy.require(session, company_id, principal.user_id, REJECT)
                except policy.PermissionDenied as denied:
                    refusal = denied.reason
                else:
                    if not note:
                        needs_reason = True
                    else:
                        store.record_decision(
                            session,
                            company_id,
                            row.id,
                            approved=False,
                            actor=actor,
                            actor_user_id=principal.user_id,
                            reason=note,
                        )
            if refusal or needs_reason:
                page = _page(session, company_id, principal.user_id)
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail=NO_SUCH_ACTION) from None

    if missing:
        raise HTTPException(status_code=404, detail=NO_SUCH_ACTION)
    if conflict:
        raise HTTPException(status_code=409, detail=ALREADY_DECIDED)
    if refusal is not None:
        return _render(
            request,
            company_id,
            page or _empty_page(),
            {
                "title": "Strata refused that approval.",
                "body": refusal,
                "action_id": action_id,
            },
            403,
        )
    if needs_reason:
        return _render(
            request,
            company_id,
            page or _empty_page(),
            {
                "title": "That rejection needs a reason.",
                "body": f"{action_id} was left as it was. {REASON_REQUIRED.capitalize()}.",
                "action_id": action_id,
            },
            400,
        )

    # 303, so a reload does not repost the decision.
    return RedirectResponse(url=ACTIONS_URL, status_code=303)


__all__ = [
    "ACTIONS_URL",
    "ALREADY_DECIDED",
    "DECISIONS",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "NOT_AN_ACTION_NOTE",
    "NO_SUCH_ACTION",
    "PROPOSE",
    "REASON_REQUIRED",
    "REJECT",
    "SEGREGATION_NOTE",
    "ActionRow",
    "ActionsPage",
    "ProposableClaim",
    "router",
    "templates",
]
