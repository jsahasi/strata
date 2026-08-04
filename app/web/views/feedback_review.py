"""The screen where feedback stops being a button and becomes work.

WHAT A REVIEWER HAS TO SEE, AND WHY THE SCREEN IS SHAPED ROUND IT. A complaint
on its own cannot be judged. "This is wrong" tells a reviewer nothing until they
can see the screen the person was on, the question they asked, the answer Clarke
gave, what it consulted, and how many claims it refused to assert. So every row
carries the frozen exchange beside it, and the triage controls sit under the
evidence rather than in a list of one-line summaries. A queue of headlines would
be quicker to read and impossible to act on honestly.

SUSPECTED ASSERTION DEFECTS COME FIRST, AND THE SCREEN SAYS WHY THEY ARE THERE.
A complaint that the product asserted something its source does not support is
the most serious defect this system can have, so those rows sort to the top. The
sort comes from two places: a person who has already referred a row, and a cheap
deterministic screen over the words (app/state/feedback.py::suspect_of_assertion).
The screen misses, and the page says so in a sentence rather than leaving a
reviewer to infer that an unflagged row is fine. A flag is a reason to look
first. It is not a verdict, and neither is its absence.

NOTHING ON THIS SCREEN EDITS A CLAIM, RESOLVES AN ESCALATION, OR CHANGES A
VERDICT. The store refuses it; this screen has no control that would ask. The
referral button sends a row for a human citation re-check and writes nothing
about the claim. Read app/state/feedback.py for the argument.

THE PERMISSION, AND THE HANDOFF. This screen gates on `user.manage`, and that is
a stand-in. The code this work actually wants is `feedback.triage` --
app/state/models.py names it, in the section that argues against a per-user trust
tier: "if the answer later is 'some people may triage and others may not', that
is a permission code". PERMISSION_CODES lives in a file another agent owns in
this build, and its strings end up in role grants that outlive a release, so a
code is not invented here.

`user.manage` is the interim gate because it is the NARROWEST existing code that
fits, and the direction matters. In the seeded grid it is held by admin alone.
Whoever eventually holds `feedback.triage` will be that set or a wider one, so
nobody gains reach today that has to be taken away later -- the interim gate
fails closed. The two codes that would have been more comfortable are both
worse: `audit.read` would gate a write on a read permission, and
`escalation.resolve` would push product complaints toward the one code whose
real meaning is "may close a refusal on a claim", which is exactly the grant
that must not be widened for convenience. The page names both codes so nobody
reads the stand-in as the decision.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app.auth import policy
from app.state import feedback as store
from app.state.db import session_scope
from app.state.models import (
    CHAT_ROLE_CLERK,
    FEEDBACK_KIND_BUG_REPORT,
    FEEDBACK_RESOLVED,
    FEEDBACK_WONT_FIX,
    IMPROVEMENT_BUG,
    IMPROVEMENT_CATEGORIES,
    IMPROVEMENT_PRIORITIES,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
)
from app.web import TEMPLATES_DIR
from app.web.deps import current_user
from app.web.views.proceedings import (
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    current_company_id,
    unresolved_escalations,
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

FEEDBACK_URL = "/admin/feedback"

# The interim gate, and the code it stands in for. See the module docstring.
PERMISSION = "user.manage"
WANTED_PERMISSION = "feedback.triage"

REFUSED = (
    f"{PERMISSION} is required to read the feedback queue. Ask an administrator. "
    "What people said about the product is not tenant evidence, but it quotes "
    "the workspace back, so it is gated like the rest of it."
)

STAND_IN = (
    f"This screen gates on {PERMISSION}, which is a stand-in. The permission "
    f"this work wants is {WANTED_PERMISSION}, and it is not in the vocabulary "
    "yet. The stand-in is deliberately narrower than the code that will replace "
    "it, so nobody holds a reach today that has to be taken away later."
)

SUSPECT_BANNER = (
    "Possible wrongly-asserted claim. Read the exchange before anything else."
)

SCREEN_IS_NOT_A_VERDICT = (
    "The flag is a word search over what the person wrote. It misses. A row with "
    "no flag has not been cleared by anything, and a row with one has not been "
    "found guilty by anything. Only a person re-reading the source at the cited "
    "offsets settles it."
)

REFERRED_TRIAGE = (
    "This was referred for a citation re-check, so it can only be raised as a "
    "high-priority bug. A claim asserted on a citation that does not verify is "
    "not a nicety, and filing it as one is how it is lost."
)

REFERRAL_NOTE = (
    "Referring records that somebody must re-read the cited source. It writes no "
    "escalation, edits no claim and changes no verdict. A referred complaint can "
    "only ever be raised as a high-priority bug, never as a nicety."
)

NOTHING_YET = "Nobody has sent any feedback yet."

# ---------------------------------------------------------------------------
# A THIN SNAPSHOT, AND THE FALLBACK THAT ANNOUNCES ITSELF
#
# A complaint whose snapshot holds the question and not the answer cannot be
# judged, and it does not look broken -- it looks like a short conversation. So
# this screen recovers the answer from the live transcript and SAYS THAT IT DID.
#
# The guard is here because that state was real. app/web/views/chat.py once
# built Feedback rows itself with a snapshot of the turns BEFORE the rated one,
# so the very turn being complained about was missing, along with what it
# consulted and what it withheld. It calls app/state/feedback.py now and the
# snapshot is whole. The guard stays for the rows written in between, and for
# whoever writes the next one.
#
# A RECOVERED TURN IS NEVER PRESENTED AS PART OF THE SNAPSHOT. A snapshot is a
# statement about a moment; quietly topping it up from today's rows would answer
# a different question with the same confidence. Where the turn cannot be
# recovered either, the screen says the answer is not in the record rather than
# rendering a complaint about nothing.
# ---------------------------------------------------------------------------

SNAPSHOT_THIN = (
    "The answer from Clarke was not in the frozen snapshot. It is shown below "
    "from the live transcript, which is not what this complaint was filed "
    "against."
)

ANSWER_MISSING = (
    "The answer from Clarke is not in the snapshot and the turn cannot be read "
    "now. This complaint cannot be judged from the record. Do not close it on "
    "the strength of the question alone."
)

# A row this screen cannot read. Announced, never skipped: a snapshot quietly
# one turn short is a snapshot that reads as the whole exchange.
UNREADABLE_ROLE = "unreadable"
UNREADABLE_TURN = (
    "A turn in this snapshot is not in a shape this screen can read. It is not "
    "shown, and it is not empty. Open the transcript."
)
UNREADABLE_TOOLS = (
    "recorded in a shape this screen cannot read -- do not read this as no tools"
)

#: How many complaints one page shows. A cap, not a policy -- nothing deletes.
QUEUE_LIMIT = 100


def truncated_words(shown: int, total: int) -> str:
    """The sentence a shortened queue carries, or "" when it is the whole thing.

    A page that stops at the cap and says nothing tells a reviewer they have
    reached the end of the complaints. Best-practices section 26: a fallback
    announces itself.
    """
    if total <= shown:
        return ""
    return (
        f"Showing the newest {shown} of {total}. The rest are still recorded and "
        "still readable; this page is capped, and nothing here deletes."
    )


# What a thumb is called on this screen. A thumbs-down has no title of its own
# and must not be given one -- inventing a heading from the first line of
# somebody's comment puts a sentence nobody wrote at the top of a triage queue.
# So the heading says what the row IS, and the person's own words sit below it
# in a quote where they belong.
THUMB_WORDS = {
    "up": "Thumbs up on a Clarke answer",
    "down": "Thumbs down on a Clarke answer",
}


def _pretty(value: str) -> str:
    """in_progress -> In progress. The column value, made readable.

    A rule and not a lookup table, following app/web/views/workflow.py: a table
    would be a second name for every status, free to disagree with the one the
    store writes, and a person asking about "in progress" would be asking about
    a word the product does not use anywhere else. The raw value still travels
    in every form, so what a person reads and what a POST carries cannot drift.
    """
    words = (value or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _actor(request: Request) -> tuple[str, str]:
    """The name for the audit row and the id beside it.

    Both, because they answer different questions: the id is the identity and
    the address is what a person reads a year later. Nobody signed in gives an
    empty id rather than an invented one -- the middleware makes that
    unreachable through the running application, and the store refuses a
    decision with no deciding account anyway.
    """
    principal = current_user(request)
    if principal is None:
        return "person:unauthenticated", ""
    return principal.email, principal.user_id


def may_review(session, company_id: str, user_id: str) -> str | None:
    """None when this person may open the screen, or the reason they may not.

    The refusal lands in the audit chain, written by policy.require, because a
    denial is a fact about a person and belongs in the same log as the approvals
    it withholds.
    """
    if not user_id:
        return "nobody is signed in"
    try:
        policy.require(session, company_id, user_id, PERMISSION)
    except policy.PermissionDenied as denied:
        return denied.reason
    return None


# ---------------------------------------------------------------------------
# What the template sees. Everything is already in words; the template prints.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """One turn of the frozen exchange, ready to render.

    `withheld` is a sentence, never a number, so a turn nobody counted cannot
    render as a turn that withheld nothing. store.withheld_words owns the
    difference.
    """

    role: str
    is_clerk: bool
    text: str
    tools: str
    withheld: str


@dataclass(frozen=True, slots=True)
class Row:
    """One complaint, with everything a reviewer needs to judge it."""

    feedback_id: str
    kind_words: str
    heading: str
    # What to prefill the backlog title with. A bug report brought its own; a
    # thumb did not, and gets "" rather than a heading this screen made up. The
    # reviewer names the work, which is the one place a title may come from
    # other than the person who reported it.
    suggested_title: str
    comment: str
    surface: str
    when: str
    status: str
    status_words: str
    referred: bool
    flag: str
    item_id: str | None
    resolution: str
    closed: bool
    turns: tuple[Turn, ...]
    # Set when Clarke's answer had to come from the live transcript, or cannot be
    # read at all. "" when the snapshot carried it, which is the ordinary case.
    recovered: str
    no_context: str


def _tools_words(used) -> str:
    """What the turn consulted, or a sentence saying which kind of nothing it is.

    [] and NULL are different facts and get different sentences. [] is a turn
    that ran no tool, which is what a screened refusal looks like. NULL is a
    turn nobody recorded, and reading it as "no tools ran" would put a false
    statement about the system's own working in front of the person judging it.
    """
    if used is None:
        return "not recorded -- this turn did not say what it consulted"
    if not used:
        return "no tool ran"
    if not isinstance(used, list) or not all(
        isinstance(entry, dict) for entry in used
    ):
        # A shape this screen does not know. Saying so beats guessing, and beats
        # a 500 that takes the whole queue down with it.
        return UNREADABLE_TOOLS
    return ", ".join(
        f"{entry.get('tool', '?')} ({entry.get('found', '?')} found)"
        for entry in used
    )


def _turns(captured) -> tuple[Turn, ...]:
    """The frozen exchange, rendered. A shape this screen cannot read says so.

    There is more than one writer of this column (see the note above), so an
    entry that is not a record is possible and must not take the page down. A
    reviewer who loses the whole queue to one malformed row loses every other
    complaint with it, and would have no way to know why.
    """
    if not captured or not isinstance(captured, list):
        return ()
    rendered = []
    for turn in captured:
        if not isinstance(turn, dict):
            rendered.append(
                Turn(
                    role=UNREADABLE_ROLE,
                    is_clerk=False,
                    text=UNREADABLE_TURN,
                    tools="",
                    withheld="",
                )
            )
            continue
        role = turn.get("role", "")
        is_clerk = role == CHAT_ROLE_CLERK
        rendered.append(
            Turn(
                role=role,
                is_clerk=is_clerk,
                text=turn.get("text", ""),
                tools=_tools_words(turn.get("tools_used")) if is_clerk else "",
                withheld=(
                    store.withheld_words(turn.get("withheld_count"))
                    if is_clerk
                    else ""
                ),
            )
        )
    return tuple(rendered)


def _recovered_answer(session, company_id: str, record, turns: tuple[Turn, ...]):
    """Fill in Clarke's answer where the snapshot left it out, and say so.

    Returns the turns to render and the sentence explaining any addition. A
    snapshot that already carries a clerk turn is returned untouched and the
    sentence is empty -- this path costs one query only on the rows that need it.
    """
    if not record.chat_message_id or any(turn.is_clerk for turn in turns):
        return turns, ""
    message = store.chat_message_for_company(
        session, company_id=company_id, message_id=record.chat_message_id
    )
    if message is None or message.role != CHAT_ROLE_CLERK:
        return turns, ANSWER_MISSING
    recovered = Turn(
        role=message.role,
        is_clerk=True,
        text=message.text,
        tools=_tools_words(message.tools_used),
        withheld=store.withheld_words(message.withheld_count),
    )
    return turns + (recovered,), SNAPSHOT_THIN


def _row(session, company_id: str, record) -> Row:
    referred = store.is_referred(record)
    flag = store.suspect_of_assertion(title=record.title, comment=record.comment)
    is_bug = record.kind == FEEDBACK_KIND_BUG_REPORT
    turns, recovered = _recovered_answer(
        session, company_id, record, _turns(record.context)
    )
    return Row(
        feedback_id=record.id,
        kind_words="Bug report" if is_bug else "Chat feedback",
        heading=record.title or THUMB_WORDS.get(record.rating or "", "Feedback"),
        suggested_title=record.title or "",
        comment=record.comment,
        surface=record.surface or "not recorded",
        when=_stamp(record.created_at),
        status=record.status,
        status_words=_pretty(record.status),
        referred=referred,
        flag=flag,
        item_id=record.improvement_item_id,
        resolution=record.resolution or "",
        closed=record.status in store.CLOSED_STATUSES,
        turns=turns,
        recovered=recovered,
        no_context=(
            ""
            if record.context
            else (
                "No exchange was captured with this one. It came from a screen, "
                "not from a conversation."
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class Item:
    """One backlog item, in words. Ids stay raw; vocabularies do not."""

    item_id: str
    title: str
    category: str
    priority: str
    status: str


def _rank(row: Row) -> int:
    """Suspected assertion defects first, then open work, then closed rows.

    A referred row outranks a flagged one: somebody has already looked at it and
    said it needs a citation re-check, which is a stronger signal than a word
    search.
    """
    if row.closed:
        return 3
    if row.referred:
        return 0
    if row.flag:
        return 1
    return 2


def _context() -> dict:
    """Everything both the queue and its refusal page need."""
    return {
        "feedback_url": FEEDBACK_URL,
        "permission": PERMISSION,
        "wanted_permission": WANTED_PERMISSION,
        "refused": REFUSED,
        "stand_in": STAND_IN,
        "suspect_banner": SUSPECT_BANNER,
        "screen_is_not_a_verdict": SCREEN_IS_NOT_A_VERDICT,
        "referral_note": REFERRAL_NOTE,
        "nothing_yet": NOTHING_YET,
        # (value, label). The value is what the POST carries and must stay the
        # column value; the label is what a person reads.
        "categories": tuple(
            (code, _pretty(code)) for code in IMPROVEMENT_CATEGORIES
        ),
        "priorities": tuple(
            (code, _pretty(code)) for code in IMPROVEMENT_PRIORITIES
        ),
        "default_category": IMPROVEMENT_BUG,
        "default_priority": PRIORITY_NORMAL,
        # The only pair the store accepts for a referred row. Named here so the
        # form cannot offer a choice the store will refuse.
        "referred_triage": REFERRED_TRIAGE,
        "referred_category": IMPROVEMENT_BUG,
        "referred_priority": PRIORITY_HIGH,
        "closed_statuses": (
            (FEEDBACK_RESOLVED, _pretty(FEEDBACK_RESOLVED)),
            (FEEDBACK_WONT_FIX, _pretty(FEEDBACK_WONT_FIX)),
        ),
    }


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


@router.get(FEEDBACK_URL, response_class=HTMLResponse)
def feedback_queue(request: Request) -> HTMLResponse:
    """The queue, newest first, with suspected assertion defects lifted to the top."""
    company_id = current_company_id()
    # Reading writes nothing, so only the id is wanted here. The label is for
    # the audit row, and this route appends one only when it refuses.
    _, user_id = _actor(request)

    state = STATE_OK
    reason: str | None = None
    rows: list[Row] = []
    items: list = []
    waiting = 0
    truncated = ""

    try:
        with session_scope() as session:
            try:
                waiting = len(unresolved_escalations(session, company_id))
            except SQLAlchemyError:
                waiting = 0
            reason = may_review(session, company_id, user_id)
            if reason is None:
                records = store.feedback_for_company(
                    session, company_id=company_id, limit=QUEUE_LIMIT
                )
                truncated = truncated_words(
                    len(records),
                    store.feedback_count(session, company_id=company_id),
                )
                # Stable sort over a query that is already newest first, so the
                # ranking lifts a band without shuffling the order inside it.
                rows = sorted(
                    (_row(session, company_id, record) for record in records),
                    key=_rank,
                )
                items = [
                    Item(
                        item_id=item.id,
                        title=item.title,
                        category=_pretty(item.category),
                        priority=_pretty(item.priority),
                        status=_pretty(item.status),
                    )
                    for item in store.improvement_items_for_company(
                        session, company_id=company_id
                    )
                ]
    except SQLAlchemyError:
        state = STATE_NO_TABLES

    context = chrome(
        company_id,
        page_title="Feedback",
        nav_active="",
        review_count=waiting,
    )
    context.update(_context())
    context.update(
        {
            "state": state,
            "reason": reason,
            "rows": rows,
            "items": items,
            "truncated": truncated,
        }
    )
    return templates.TemplateResponse(
        request,
        "feedback_review.html",
        context,
        status_code=403 if reason else 200,
    )


def _refuse(request: Request, company_id: str, reason: str) -> HTMLResponse:
    context = chrome(
        company_id, page_title="Feedback", nav_active="", review_count=0
    )
    context.update(_context())
    context.update(
        {
            "state": STATE_OK,
            "reason": reason,
            "rows": [],
            "items": [],
            "truncated": "",
        }
    )
    return templates.TemplateResponse(
        request, "feedback_review.html", context, status_code=403
    )


def _back() -> RedirectResponse:
    """303, so a reload does not repost the decision."""
    return RedirectResponse(url=FEEDBACK_URL, status_code=303)


def _missing() -> HTMLResponse:
    """Another company's id, or none at all. One answer for both.

    Which of the two it is would itself tell the caller that another tenant
    holds that id.
    """
    return HTMLResponse("No such feedback for this company.", status_code=404)


def _bad(reason: str) -> HTMLResponse:
    """A refusal from the store, shown rather than swallowed."""
    return HTMLResponse(reason, status_code=400)


@router.post(FEEDBACK_URL + "/{feedback_id}/triage")
def triage_feedback(
    request: Request,
    feedback_id: str,
    category: str = Form(default=IMPROVEMENT_BUG),
    priority: str = Form(default=PRIORITY_NORMAL),
    title: str = Form(default=""),
    detail: str = Form(default=""),
    item_id: str = Form(default=""),
):
    """Raise backlog work from a complaint, or attach it to work already raised."""
    company_id = current_company_id()
    actor, user_id = _actor(request)
    with session_scope() as session:
        reason = may_review(session, company_id, user_id)
        if reason is not None:
            return _refuse(request, company_id, reason)
        if store.feedback_row(
            session, company_id=company_id, feedback_id=feedback_id
        ) is None:
            return _missing()
        try:
            store.triage(
                session,
                company_id=company_id,
                feedback_id=feedback_id,
                actor=actor,
                actor_user_id=user_id,
                category=category,
                title=title,
                detail=detail,
                priority=priority,
                item_id=item_id.strip() or None,
            )
        except ValueError as refused:
            # Roll back before answering. The store checks before it writes, so
            # there should be nothing to undo -- but a refusal that left half a
            # decision committed would be the worst of both, and session_scope
            # commits on the way out of a return.
            session.rollback()
            return _bad(str(refused))
    return _back()


@router.post(FEEDBACK_URL + "/{feedback_id}/refer")
def refer_feedback(
    request: Request, feedback_id: str, note: str = Form(default="")
):
    """Send a complaint for a human citation re-check. Writes no escalation."""
    company_id = current_company_id()
    actor, user_id = _actor(request)
    with session_scope() as session:
        reason = may_review(session, company_id, user_id)
        if reason is not None:
            return _refuse(request, company_id, reason)
        if store.feedback_row(
            session, company_id=company_id, feedback_id=feedback_id
        ) is None:
            return _missing()
        try:
            store.refer_for_verification(
                session,
                company_id=company_id,
                feedback_id=feedback_id,
                actor=actor,
                actor_user_id=user_id,
                note=note,
            )
        except ValueError as refused:
            # Roll back before answering. The store checks before it writes, so
            # there should be nothing to undo -- but a refusal that left half a
            # decision committed would be the worst of both, and session_scope
            # commits on the way out of a return.
            session.rollback()
            return _bad(str(refused))
    return _back()


@router.post(FEEDBACK_URL + "/{feedback_id}/resolve")
def resolve_feedback(
    request: Request,
    feedback_id: str,
    resolution: str = Form(default=""),
    status: str = Form(default=FEEDBACK_RESOLVED),
):
    """Close a complaint, in words. The words are required by the store."""
    company_id = current_company_id()
    actor, user_id = _actor(request)
    with session_scope() as session:
        reason = may_review(session, company_id, user_id)
        if reason is not None:
            return _refuse(request, company_id, reason)
        if store.feedback_row(
            session, company_id=company_id, feedback_id=feedback_id
        ) is None:
            return _missing()
        try:
            store.resolve(
                session,
                company_id=company_id,
                feedback_id=feedback_id,
                actor=actor,
                actor_user_id=user_id,
                resolution=resolution,
                status=status,
            )
        except ValueError as refused:
            # Roll back before answering. The store checks before it writes, so
            # there should be nothing to undo -- but a refusal that left half a
            # decision committed would be the worst of both, and session_scope
            # commits on the way out of a return.
            session.rollback()
            return _bad(str(refused))
    return _back()


__all__ = [
    "FEEDBACK_URL",
    "NOTHING_YET",
    "PERMISSION",
    "REFERRAL_NOTE",
    "REFERRED_TRIAGE",
    "ANSWER_MISSING",
    "REFUSED",
    "SCREEN_IS_NOT_A_VERDICT",
    "SNAPSHOT_THIN",
    "UNREADABLE_TOOLS",
    "UNREADABLE_TURN",
    "STAND_IN",
    "SUSPECT_BANNER",
    "WANTED_PERMISSION",
    "may_review",
    "router",
]
