"""What a person said about the product, and what a reviewer did about it.

NOTHING HERE MAY EDIT A CLAIM, RESOLVE AN ESCALATION, OR CHANGE A VERDICT.
Feedback records that a person disagreed. Only the source agreeing with the
quote makes a claim assert. This module writes to three tables -- feedback,
improvement_items and the audit chain -- and to nothing else, ever. A complaint
that a claim is wrong is evidence that somebody should look; it is not evidence
that the claim is wrong, and a store that could act on it would be a way to
publish a statement by complaining about it loudly enough.
tests/test_feedback.py reads this module's own source for a write to a claim or
an escalation, so the rule survives the next edit rather than this paragraph.

WHY THE LOOP EXISTS AT ALL. Feedback nobody reads is a button. The thumb has to
land somewhere a person opens, beside enough of the exchange to judge it, with
an action that turns it into work. That is the whole shape of this module: two
ways in, one queue out, three decisions.

ONE WRITER OF THIS TABLE, AND IT IS THIS FILE. app/web/views/chat.py serves the
two POSTs and calls record_thumb and record_bug_report rather than building rows
itself, so there is one place that decides what a complaint carries -- what the
snapshot holds, where the surface comes from, and which turns may be rated. It
briefly did build them inline, with a snapshot that dropped the very turn being
complained about, and the review screen still carries the guard that came out of
that: a row whose snapshot has no clerk turn has the answer recovered from the
live transcript, with a sentence saying it was. That guard stays. It costs one
query on the rows that need it, and it covers any row written before this
function existed as well as the next writer who tries.

-- THE JUDGEMENT: A WRONG ANSWER IS NOT A BROKEN BUTTON ---------------------

A broken button is a defect in the interface. A wrong answer, in this product,
may mean the system asserted a claim whose citation does not support it -- the
one failure the whole design exists to prevent. Those two cannot share a queue,
so this module routes them apart. It does NOT route the second one into the
escalation queue, and the reasoning matters more than the mechanism.

1. THE ESCALATION QUEUE IS THE VERIFIER'S OUTPUT, NOT A SUGGESTION BOX. Every
   Escalation row means the citation check ran against the stored source and
   failed, with a reason code and a claim id. Its value to a reviewer is that
   everything in it was machine-checked. Writing a row there from a person's
   opinion would end that: the reviewer could no longer say what the queue is a
   list of, and the first unfounded complaint filed there would be
   indistinguishable from a real refusal.

2. AND IT WOULD BE CLOSEABLE. Escalations resolve. A complaint dropped into that
   queue could be closed as resolved by somebody holding escalation.resolve, and
   the hash-chained log would then say, permanently and verifiably, that a
   refusal by the verifier had been reviewed and cleared -- when no verifier ever
   refused anything. A false clean record on an append-only chain is worse than
   no record.

3. A BACKLOG IS ALSO WRONG. A backlog is sorted by product value. "The product
   asserted something its source does not support" is not a feature request that
   lost a priority argument.

So the row is REFERRED: marked in words on the row itself, audited under its own
action code, kept out of the backlog, and shown at the top of the review screen
with the exchange beside it. What closes it is a person re-reading the source at
the cited offsets. Absence is denial applied to a complaint: the product cannot
confirm the complaint and must not dismiss it, so it hands it to somebody who
can look, and says plainly that it has not decided.

The one downgrade that is impossible: triaging a referred row into anything
other than a high-priority bug. A citation re-check has two honest outcomes --
the quote is exact and the complaint was wrong, or it is not and the product
asserted something it should have withheld. There is no third outcome where it
becomes a low-priority nicety, and triage() refuses to write one.

-- SUBMITTING IS A ROW; DECIDING IS A CHAIN ENTRY ---------------------------

record_thumb() and record_bug_report() write NO audit event. Every other write
here does. That asymmetry is deliberate and it is the answer to the concession
models.py makes about this table: nothing rate-limits submission, so any
signed-in person can flood the queue. If submission were audited, the flood
would land in the one table that is append-only by refusal and cannot be
trimmed, and a person with a grievance could pad the evidentiary record of a
regulated workspace at will. The Feedback row is already self-attributing --
user_id, company_id, created_at -- so nothing is lost. What goes in the chain is
what models.py says goes in the chain: every status change, because that is a
person acting.

-- WHAT IS SCOPED, AND HOW ---------------------------------------------------

Every read takes a keyword-only company_id with no default and refuses an empty
one, through _require_scope imported from app/state/queries.py rather than
copied. These reads belong IN that file beside versions_for_company; they are
here because that file is owned by another agent in this build, and the shape is
the one it uses so the move is a cut and paste. Same handoff note as
app/web/views/admin.py and app/web/views/workflow.py carry.

The four ACTION_ constants below belong in app/state/audit.py beside
ACTION_PROJECT_CREATED, and must be MOVED there rather than restated -- that
file's docstring gives the reason, which is that a second spelling writes rows
no query for the first spelling will ever return. They are here for the same
reason as the reads. This is a handoff, not a decision.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.state.audit import ACTOR_USER, record_event
from app.state.models import (
    CHAT_ROLE_CLERK,
    FEEDBACK_IN_PROGRESS,
    FEEDBACK_KIND_BUG_REPORT,
    FEEDBACK_KIND_FEEDBACK,
    FEEDBACK_NEW,
    FEEDBACK_RATINGS,
    FEEDBACK_RESOLVED,
    FEEDBACK_TRIAGED,
    FEEDBACK_WONT_FIX,
    IMPROVEMENT_BUG,
    IMPROVEMENT_CATEGORIES,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    ChatMessage,
    Feedback,
    ImprovementItem,
)

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# Audited actions. See the handoff note in the module docstring.
# ---------------------------------------------------------------------------

ACTION_FEEDBACK_TRIAGED = "feedback.triaged"
ACTION_FEEDBACK_REFERRED = "feedback.referred"
ACTION_FEEDBACK_RESOLVED = "feedback.resolved"
# A separate code from feedback.resolved on purpose. "Who closed a suspected
# wrongly-asserted claim, and what did they say they checked" is a question an
# auditor asks on its own, and it must not need a scan of every closure to
# answer. Reading a referral closure as an ordinary one would also lose the only
# record that a person re-read the source, since the row's resolution by then
# carries the closure rather than the referral.
ACTION_REFERRAL_CLOSED = "feedback.referral_closed"

# ---------------------------------------------------------------------------
# The referral
# ---------------------------------------------------------------------------

#: The first words of a referred row's ``resolution``. The marker is a sentence
#: rather than a flag column, because there is no column for it and because the
#: person who opens the database at 2am reads a sentence. is_referred() is the
#: only reader of this prefix; nothing else may test the string.
REFERRAL_PREFIX = "Referred for citation check: "

#: What the referral appends after the reviewer's own words, so the row says
#: what happens next rather than leaving the status to imply it.
REFERRAL_TAIL = (
    " -- Not backlogged. This closes when somebody has re-read the source at the "
    "cited offsets. The product has not decided whether the complaint is right."
)

#: The statuses that mean the row is shut. Closing is the one status change that
#: needs a form of words, so it has its own set rather than a check per call.
CLOSED_STATUSES = (FEEDBACK_RESOLVED, FEEDBACK_WONT_FIX)

# ---------------------------------------------------------------------------
# Absence, said out loud
# ---------------------------------------------------------------------------

#: What the screen prints where a clerk turn recorded no withheld count. NULL on
#: a clerk row is a defect in whoever wrote the turn -- it means nothing counted,
#: so nothing may report coverage for it. Printing 0 would report full coverage
#: for a turn nobody measured, which is the failure ADR-22 exists to prevent
#: arriving through the one surface where it would not be noticed.
COUNT_MISSING = "not counted -- this turn recorded no withheld count"

#: How many turns of the exchange a thumbs-down freezes. A cap, because the
#: snapshot goes in a JSON column and a long conversation would put a novel in
#: every row of the triage queue. When it bites, the snapshot says so: see
#: _snapshot.
CONTEXT_TURNS = 12

#: The marker entry a trimmed snapshot carries at its head. The role is
#: deliberately not one of CHAT_ROLES, so nothing can mistake it for a turn.
OMITTED_ROLE = "omitted"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12].upper()}"


def withheld_words(count: int | None) -> str:
    """How many claims a turn refused to assert, or that nobody counted.

    Two different facts get two different sentences. 0 is a measurement; None is
    the absence of one. A reader that folded them would report a clean turn for
    one nobody measured.
    """
    if count is None:
        return COUNT_MISSING
    if count == 0:
        return "0 withheld -- measured, and nothing was held back"
    if count == 1:
        return "1 claim withheld"
    return f"{count} claims withheld"


# ---------------------------------------------------------------------------
# The deterministic screen
#
# Cheap, wrong in one direction only, and it decides nothing. Borrowed in shape
# from app/chat/persona.py::screen(): refuse -- here, flag -- the unmistakable
# without spending a token, and leave a record that the flag was deterministic
# rather than a model's mood.
# ---------------------------------------------------------------------------

_ASSERTION_COMPLAINT = re.compile(
    r"\bmade (?:that|this|it) up\b"
    r"|\bmakes? (?:that|this|it) up\b"
    r"|\bmaking (?:that|this|it) up\b"
    r"|\bfabricat"
    r"|\bhallucinat"
    r"|\binvented\b"
    r"|\bnot in the (?:source|document|filing|order|record|text)\b"
    r"|\bnot what (?:it|the source|the document|the order) says\b"
    r"|\bdoes ?n.?t say (?:that|this|it)\b"
    r"|\bnever says?\b"
    r"|\bmisquot"
    r"|\bwrong (?:citation|quote|cite|source|paragraph|page|offsets?)\b"
    r"|\bcitation (?:is )?(?:wrong|bad|broken|does ?n.?t)"
    r"|\bno such (?:paragraph|section|filing|order|version)\b"
    r"|\bshould (?:have been|be) withheld\b"
    r"|\bshould not have (?:said|asserted|claimed|published)\b"
    r"|\bwrong answer\b"
    r"|\bunsupported\b",
    re.I,
)


def suspect_of_assertion(*, title: str | None, comment: str | None) -> str:
    """The words that make this read like a complaint about an asserted claim.

    Returns the matched phrase, or "" where the screen found nothing. IT IS A
    SCREEN AND NOT A VERDICT, in both directions. A hit does not mean the
    product was wrong; it means a reviewer should look first. No hit means the
    screen recognised nothing, NEVER that the answer was right -- somebody who
    writes "this is not right and I do not believe the second line" is reporting
    exactly the same defect in words this pattern does not know.

    So the flag only ever sorts the queue. It never filters it, never closes a
    row, and never decides where one routes. A person does that, which is what
    refer_for_verification() is for, and the review screen prints the sentence
    above beside the flag so nobody reads its absence as a clean bill.
    """
    hit = _ASSERTION_COMPLAINT.search(f"{title or ''}\n{comment or ''}")
    return hit.group(0) if hit else ""


def is_referred(row: Feedback) -> bool:
    """Whether this row was sent for a citation re-check rather than backlogged.

    One predicate, one reader of REFERRAL_PREFIX. The state is a pair -- work in
    progress, with nothing backlogged and a resolution that says why -- and a
    second place testing the string is a second answer waiting to disagree.
    """
    if row is None:
        return False
    return (row.resolution or "").startswith(REFERRAL_PREFIX)


# ---------------------------------------------------------------------------
# Scoped reads. These belong in app/state/queries.py -- see the module
# docstring. company_id is keyword-only and has no default, which is the shape
# that file uses, so the move is a cut and paste.
# ---------------------------------------------------------------------------


def chat_message_for_company(
    session: Session, *, company_id: str, message_id: str
) -> ChatMessage | None:
    """One turn, or None. Another tenant's id resolves to None, not to a row.

    Filtered on the message's own company_id rather than joined through the
    session, which is why that column is on the row: the id arrives off the wire
    on the feedback POST, so the scope check is one filter on the row the caller
    already has and not a join somebody forgets.
    """
    _require_scope(company_id)
    if not message_id:
        return None
    return (
        session.query(ChatMessage)
        .filter(ChatMessage.company_id == company_id)
        .filter(ChatMessage.id == message_id)
        .one_or_none()
    )


def turns_for_session(
    session: Session, *, company_id: str, chat_session_id: str
) -> list[ChatMessage]:
    """A conversation in order. ORDER BY ordinal, never created_at.

    A writer that stamps the question and the answer from one clock read ties
    the timestamps, and a transcript ordered by time then renders the answer
    above the question -- in the snapshot a reviewer judges a complaint from.
    """
    _require_scope(company_id)
    if not chat_session_id:
        return []
    return (
        session.query(ChatMessage)
        .filter(ChatMessage.company_id == company_id)
        .filter(ChatMessage.session_id == chat_session_id)
        .order_by(ChatMessage.ordinal)
        .all()
    )


def feedback_row(
    session: Session, *, company_id: str, feedback_id: str
) -> Feedback | None:
    """One complaint, or None. Another company's id is None and not a 403.

    None rather than a raise, matching change_for_company and user_for_company:
    the caller has an id from a URL and a 404 is the honest answer. Telling the
    caller apart -- "not here" from "not yours" -- would confirm that another
    tenant holds that id.
    """
    _require_scope(company_id)
    if not feedback_id:
        return None
    return (
        session.query(Feedback)
        .filter(Feedback.company_id == company_id)
        .filter(Feedback.id == feedback_id)
        .one_or_none()
    )


def _feedback_query(session: Session, company_id: str, statuses):
    query = session.query(Feedback).filter(Feedback.company_id == company_id)
    if statuses:
        query = query.filter(Feedback.status.in_(tuple(statuses)))
    return query


def feedback_for_company(
    session: Session,
    *,
    company_id: str,
    statuses: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[Feedback]:
    """The queue, newest first. Every status unless asked for some.

    Newest first because a triage screen is worked from the top and the thing
    somebody just hit is the thing worth seeing. The limit is a cap on one
    screen, not a policy: nothing here deletes, and the older rows stay readable
    by asking for a status.

    THE CAP DOES NOT ANNOUNCE ITSELF FROM HERE. A list cannot say it is short.
    feedback_count() is beside it so the caller can compare and say so on the
    page, and the review screen does. A queue that silently stops at 200 would
    tell a reviewer they had reached the end of the complaints.
    """
    _require_scope(company_id)
    return (
        _feedback_query(session, company_id, statuses)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .limit(limit)
        .all()
    )


def feedback_count(
    session: Session, *, company_id: str, statuses: tuple[str, ...] | None = None
) -> int:
    """How many there really are. The other half of the cap above."""
    _require_scope(company_id)
    return _feedback_query(session, company_id, statuses).count()


def improvement_item(
    session: Session, *, company_id: str, item_id: str
) -> ImprovementItem | None:
    _require_scope(company_id)
    if not item_id:
        return None
    return (
        session.query(ImprovementItem)
        .filter(ImprovementItem.company_id == company_id)
        .filter(ImprovementItem.id == item_id)
        .one_or_none()
    )


def improvement_items_for_company(
    session: Session, *, company_id: str, limit: int = 200
) -> list[ImprovementItem]:
    """The backlog, newest first."""
    _require_scope(company_id)
    return (
        session.query(ImprovementItem)
        .filter(ImprovementItem.company_id == company_id)
        .order_by(ImprovementItem.created_at.desc(), ImprovementItem.id.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# Recording. Neither of these writes an audit event -- see the module docstring.
# ---------------------------------------------------------------------------


def _snapshot(session: Session, *, company_id: str, message: ChatMessage) -> list:
    """Freeze the exchange as it stood when the thumb went down.

    THE RATED TURN IS IN THE SNAPSHOT. models.py describes context as "the turns
    BEFORE this one"; it is written here as the turns UP TO AND INCLUDING it,
    because a reviewer judging a complaint about an answer has to be able to read
    the answer. A snapshot that stops one turn short answers a question nobody
    asked. Noted as a handoff so the comment there and the writer here agree.

    Capped at CONTEXT_TURNS, and a trimmed snapshot says so at its head rather
    than silently starting mid-conversation. A fallback announces itself.

    tools_used and withheld_count are copied straight in, NULLs and all. Folding
    a NULL to zero here would put a false coverage number in front of the person
    deciding whether the product mis-stated something.
    """
    turns = [
        turn
        for turn in turns_for_session(
            session, company_id=company_id, chat_session_id=message.session_id
        )
        if turn.ordinal <= message.ordinal
    ]
    trimmed = len(turns) - CONTEXT_TURNS
    kept = turns[-CONTEXT_TURNS:] if trimmed > 0 else turns

    captured = [
        {
            "ordinal": turn.ordinal,
            "role": turn.role,
            "text": turn.text,
            "tools_used": turn.tools_used,
            "withheld_count": turn.withheld_count,
        }
        for turn in kept
    ]
    if trimmed > 0:
        captured.insert(
            0,
            {
                "ordinal": 0,
                "role": OMITTED_ROLE,
                "text": (
                    f"{trimmed} earlier turns in this conversation were not "
                    "captured. Open the transcript to read them."
                ),
                "tools_used": None,
                "withheld_count": None,
            },
        )
    return captured


def record_thumb(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    message_id: str,
    rating: str,
    comment: str = "",
) -> Feedback:
    """A thumb on one of Clerk's turns, with the exchange frozen beside it.

    company_id AND user_id ARE INJECTED BY THE CALLER FROM THE SIGNED-IN
    SESSION, never read off the wire. The feedback POST carries {message_id,
    rating, comment?} and nothing else, which is the same isolation rule the
    tool layer follows: a caller that can pass an identity is a caller that can
    be talked into passing a different one. surface comes from the message for
    the same reason -- it is the only place it exists that the submitter did not
    choose.

    A thumb on a person's own turn is refused: rating your own question says
    nothing about the product, and a queue full of them would bury the ones that
    do.

    An unknown or another tenant's message id is refused rather than recorded
    against nothing. A row pointing at no turn cannot be judged by anybody.
    """
    _require_scope(company_id)
    if not user_id:
        raise ValueError("user_id is required; unattributed feedback cannot be judged")
    if rating not in FEEDBACK_RATINGS:
        raise ValueError(
            f"rating must be one of {FEEDBACK_RATINGS}; got {rating!r}. There is no "
            "neutral thumb -- a row with no rating is a bug report."
        )

    message = chat_message_for_company(
        session, company_id=company_id, message_id=message_id
    )
    if message is None:
        raise ValueError("no such chat message for this company")
    if message.role != CHAT_ROLE_CLERK:
        raise ValueError(
            "a thumb attaches to a clerk turn; rating a person's own question "
            "records nothing about the product"
        )

    row = Feedback(
        id=_new_id("FBK"),
        company_id=company_id,
        user_id=user_id,
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=rating,
        title=None,
        comment=(comment or "").strip(),
        context=_snapshot(session, company_id=company_id, message=message),
        surface=message.surface or "",
        status=FEEDBACK_NEW,
        resolution=None,
        chat_message_id=message.id,
        improvement_item_id=None,
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def record_bug_report(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    title: str,
    detail: str = "",
    surface: str = "",
) -> Feedback:
    """A bug report from a screen. No rating, no chat, and a title of its own.

    A bug report carries no rating, and no rating is not a neutral one -- kind
    is what keeps the two shapes apart in one table. The title is required: it
    is what a triage queue is read by, and inventing one from the first line of
    the detail would put a sentence nobody wrote at the top of the queue.
    """
    _require_scope(company_id)
    if not user_id:
        raise ValueError("user_id is required; unattributed feedback cannot be judged")
    heading = (title or "").strip()
    if not heading:
        raise ValueError("a bug report needs a title; the queue is read by titles")

    row = Feedback(
        id=_new_id("FBK"),
        company_id=company_id,
        user_id=user_id,
        kind=FEEDBACK_KIND_BUG_REPORT,
        rating=None,
        title=heading,
        comment=(detail or "").strip(),
        context=None,
        surface=(surface or "").strip(),
        status=FEEDBACK_NEW,
        resolution=None,
        chat_message_id=None,
        improvement_item_id=None,
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Decisions. Every one of these is a person acting, so every one is audited.
# ---------------------------------------------------------------------------


def _decided(row: Feedback, actor: str, actor_user_id: str) -> None:
    if row is None:
        raise ValueError("no such feedback for this company")
    if not actor:
        raise ValueError("a decision needs an actor; an unattributed one is not auditable")
    if not actor_user_id:
        raise ValueError(
            "a decision needs the deciding account; the audit row records who "
            "the writing process believed was acting, and a blank there is a "
            "decision nobody can be asked about"
        )


def _audit(
    session: Session,
    *,
    company_id: str,
    actor: str,
    actor_user_id: str,
    action: str,
    row: Feedback,
    reason: str,
) -> None:
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type="feedback",
        subject_id=row.id,
        reason=reason,
        actor_user_id=actor_user_id,
        actor_kind=ACTOR_USER,
    )


def refer_for_verification(
    session: Session,
    *,
    company_id: str,
    feedback_id: str,
    actor: str,
    actor_user_id: str,
    note: str,
) -> Feedback:
    """Send a complaint for a citation re-check instead of into the backlog.

    This is the wrong-answer route, and what it does NOT do is the point: it
    writes no Escalation, touches no claim, and states no verdict. It marks the
    row in words, moves it to in-progress because a person now owns it, and
    appends one audit event under its own action code.

    Refused on a row already referred: a second note would overwrite the first
    and the queue would lose why it was sent. Refused on a closed row for the
    same reason -- reopening one would rewrite what somebody already decided.
    """
    _require_scope(company_id)
    row = feedback_row(session, company_id=company_id, feedback_id=feedback_id)
    _decided(row, actor, actor_user_id)

    words = (note or "").strip()
    if not words:
        raise ValueError(
            "a referral needs a note saying what to check; a bare flag tells the "
            "person who picks it up nothing"
        )
    if is_referred(row):
        raise ValueError(
            f"already referred: {row.resolution}. A second referral would "
            "overwrite the first and lose why it was sent."
        )
    if row.status in CLOSED_STATUSES:
        raise ValueError(
            f"this feedback is closed ({row.status}); reopening it would rewrite "
            "a decision somebody already made and recorded"
        )

    row.resolution = f"{REFERRAL_PREFIX}{words}{REFERRAL_TAIL}"
    row.status = FEEDBACK_IN_PROGRESS
    session.flush()

    _audit(
        session,
        company_id=company_id,
        actor=actor,
        actor_user_id=actor_user_id,
        action=ACTION_FEEDBACK_REFERRED,
        row=row,
        reason=(
            f"referred for citation check: {words}. No claim, escalation or "
            "verdict was changed."
        ),
    )
    return row


def triage(
    session: Session,
    *,
    company_id: str,
    feedback_id: str,
    actor: str,
    actor_user_id: str,
    category: str = IMPROVEMENT_BUG,
    title: str = "",
    detail: str = "",
    priority: str = PRIORITY_NORMAL,
    item_id: str | None = None,
) -> ImprovementItem:
    """Turn a complaint into backlog work, or attach it to work already raised.

    Passing item_id attaches to an existing item rather than raising a second
    one. Many complaints becoming one item is the usual case, not the exception,
    which is why the link sits on the complaint.

    A REFERRED ROW MAY ONLY BECOME A HIGH-PRIORITY BUG. See the module
    docstring: the two honest outcomes of a citation re-check are "the quote is
    exact" and "the product asserted something it should have withheld", and the
    second one is not a nicety. Attaching a referred row to an existing item is
    refused for the same reason -- the item it would join was raised for
    something else.
    """
    _require_scope(company_id)
    row = feedback_row(session, company_id=company_id, feedback_id=feedback_id)
    _decided(row, actor, actor_user_id)

    referred = is_referred(row)
    if referred:
        if item_id is not None:
            raise ValueError(
                "this feedback was referred for a citation check; it cannot be "
                "folded into an item raised for something else"
            )
        if category != IMPROVEMENT_BUG or priority != PRIORITY_HIGH:
            raise ValueError(
                "this feedback was referred for a citation check, so it may only "
                f"be raised as a {IMPROVEMENT_BUG} at {PRIORITY_HIGH} priority. A "
                "claim asserted on a citation that does not verify is not a "
                "low-priority nicety, and filing it as one is how it is lost."
            )

    if item_id is not None:
        item = improvement_item(session, company_id=company_id, item_id=item_id)
        if item is None:
            raise ValueError("no such improvement item for this company")
    else:
        if category not in IMPROVEMENT_CATEGORIES:
            raise ValueError(
                f"category must be one of {IMPROVEMENT_CATEGORIES}; got "
                f"{category!r}. A misspelt category writes an item no backlog "
                "read for that category will ever return."
            )
        heading = (title or "").strip() or (row.title or "").strip()
        if not heading:
            raise ValueError("an improvement item needs a title")
        item = ImprovementItem(
            id=_new_id("IMP"),
            company_id=company_id,
            category=category,
            title=heading,
            detail=(detail or "").strip() or row.comment,
            priority=priority,
        )
        session.add(item)
        session.flush()

    row.improvement_item_id = item.id
    row.status = FEEDBACK_TRIAGED
    session.flush()

    _audit(
        session,
        company_id=company_id,
        actor=actor,
        actor_user_id=actor_user_id,
        action=ACTION_FEEDBACK_TRIAGED,
        row=row,
        reason=(
            f"triaged into {item.id} ({item.category}, {item.priority})"
            + (
                "; raised from a referral, so a person had already re-read the "
                "cited source"
                if referred
                else ""
            )
        ),
    )
    return item


def resolve(
    session: Session,
    *,
    company_id: str,
    feedback_id: str,
    actor: str,
    actor_user_id: str,
    resolution: str,
    status: str = FEEDBACK_RESOLVED,
) -> Feedback:
    """Close a complaint, in words, under one of two action codes.

    The words are required. A resolved row with no resolution is a row somebody
    closed without saying why, and the queue would have no way to tell that from
    one that was actually dealt with.

    Closing a REFERRED row writes ACTION_REFERRAL_CLOSED and carries the original
    referral note into the audit reason. The row's resolution then holds the
    closure, so the chain is the only place the two survive together -- one log,
    not two.
    """
    _require_scope(company_id)
    row = feedback_row(session, company_id=company_id, feedback_id=feedback_id)
    _decided(row, actor, actor_user_id)

    words = (resolution or "").strip()
    if not words:
        raise ValueError(
            "a resolution needs words; a row closed with nothing written on it "
            "cannot be told from one nobody dealt with"
        )
    if status not in CLOSED_STATUSES:
        raise ValueError(
            f"resolve() can only close a row: status must be one of "
            f"{CLOSED_STATUSES}; got {status!r}"
        )

    referred = is_referred(row)
    referral_note = row.resolution if referred else ""

    row.resolution = words
    row.status = status
    session.flush()

    _audit(
        session,
        company_id=company_id,
        actor=actor,
        actor_user_id=actor_user_id,
        action=ACTION_REFERRAL_CLOSED if referred else ACTION_FEEDBACK_RESOLVED,
        row=row,
        reason=(
            f"closed as {status}: {words}"
            + (f" [{referral_note}]" if referred else "")
        ),
    )
    return row


__all__ = [
    "ACTION_FEEDBACK_REFERRED",
    "ACTION_FEEDBACK_RESOLVED",
    "ACTION_FEEDBACK_TRIAGED",
    "ACTION_REFERRAL_CLOSED",
    "CLOSED_STATUSES",
    "CONTEXT_TURNS",
    "COUNT_MISSING",
    "OMITTED_ROLE",
    "REFERRAL_PREFIX",
    "REFERRAL_TAIL",
    "chat_message_for_company",
    "feedback_count",
    "feedback_for_company",
    "feedback_row",
    "improvement_item",
    "improvement_items_for_company",
    "is_referred",
    "record_bug_report",
    "record_thumb",
    "refer_for_verification",
    "resolve",
    "suspect_of_assertion",
    "triage",
    "turns_for_session",
    "withheld_words",
]
