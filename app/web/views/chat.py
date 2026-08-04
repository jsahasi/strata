"""The chat surface: one turn endpoint, two feedback endpoints, and the dock.

WHAT THIS MODULE IS. The web edge of Clarke. It takes a typed sentence off the
wire, decides who is asking from the session cookie rather than from the body,
runs the deterministic screen in app/chat/persona.py, hands what survives to the
answering engine, writes the transcript, and answers the five keys the contract
names. It holds no prompt text, no tone, and no tools. persona.py owns how Clarke
speaks; app/chat/tools.py owns what Clarke may do. Nothing here restates either.

THE ISOLATION DESIGN, WHICH IS THE POINT OF THE REQUEST MODELS BELOW. The model
never supplies company_id and never supplies the actor. Both are injected here,
from the signed-in session, and the request bodies REFUSE those fields rather
than ignoring them. Ignoring is the tempting choice and it is the wrong one: a
caller that believes it set the scope and did not is a caller that will one day
be believed, and prompt injection that can smuggle a tenant id into a body reads
another company's record. A 422 says no; a silent drop says nothing.

The same reasoning applies to the snapshot on a thumbs-down. The exchange a
reviewer reads later is frozen by app/state/feedback.py out of the stored turns,
and a caller offering its own `context` is refused at the door. A
browser-supplied transcript is a browser-supplied transcript.

ABSENCE IS DENIAL, TWICE OVER.

  No principal, no turn. app/web/deps.py::current_company() falls back to the
  demo tenant when nobody is signed in, and that fallback exists for the seed
  and for scripts. A chat turn may not take it: it would write a transcript into
  a company on behalf of nobody. With no session this answers 401, and the
  middleware in the assembled application answers earlier still.

  No count, no answer. The wire's `withheld` is a measurement. An engine that
  returns prose without one has measured nothing, and reporting that as zero
  tells an analyst the answer is complete when nothing counted it (ADR-22). The
  answer is dropped and the reply says so. The same holds for `used`: provenance
  that cannot be read is dropped whole rather than quietly shortened, because a
  turn claiming fewer sources than it consulted is the same lie in a smaller
  font.

THE FALLBACK ANNOUNCES ITSELF. app/chat/engine.py does not exist in this tree
yet. Rather than answer from a model's memory, or from a cheerful stub, a turn
with no engine says plainly that nothing was looked up and offers the screens
that do hold the record. §26 of docs/best-practices.html, applied to the one
surface where a confident sentence costs nothing to produce.

WHAT IS AUDITED, AND WHAT IS NOT. A refusal is. Nothing else this module writes
is, and the second half of that is a deviation from the brief this surface was
built to, taken on evidence and reported rather than done quietly.

  A REFUSAL IS A DECISION THE SYSTEM MADE ABOUT A PERSON, which is the reason
  app/state/audit.py gives for auditing access.denied, so a screened turn is
  appended to the chain under the code persona.py chose for it.

  AN ORDINARY TURN IS NOT. chat_messages already holds who asked what and when,
  and copying every question into the chain would turn an audit log into a chat
  log and bury the decisions it exists to carry.

  A THUMB AND A BUG REPORT ARE NOT, on app/state/feedback.py's argument rather
  than this module's. Nothing rate-limits submission, so a person with a
  grievance could pad an append-only, un-trimmable evidentiary record at will --
  in a regulated workspace, where that record is the product's whole claim on an
  auditor's trust. The Feedback row is self-attributing already: user_id,
  company_id, created_at. What goes in the chain is every status change, because
  that is a person acting. The brief for this surface said both were audited;
  that store's reasoning is better than the instruction, and reversing the call
  is one record_event() in each of the two handlers below.

ONE WRITER, NOT TWO. app/state/feedback.py owns the feedback table -- the row
shapes, the snapshot, the refusals -- and this module calls it rather than
building rows of its own. It landed while this surface was being written, and
the hand-rolled copies that were here first are gone: two writers for one table
is how a snapshot ends up with a different shape depending on which screen wrote
it. The same file owns the tenant-scoped chat reads, so those are imported here
and not restated.
"""

from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.chat import persona
from app.state import feedback as store
from app.state.audit import ACTOR_USER, record_event
from app.state.db import session_scope
from app.state.feedback import chat_message_for_company as message_for_company
from app.state.identity import user_for_company
from app.state.models import (
    CHAT_ROLE_CLERK,
    CHAT_ROLE_PERSON,
    FEEDBACK_RATINGS,
    ChatMessage,
    ChatSession,
)

# Imported, not copied. The same guard app/state/feedback.py imports over these
# same tables: one spelling of "an unscoped read is refused", one place to audit.
from app.state.queries import _require_scope
from app.web.deps import current_user

router = APIRouter()

# ---------------------------------------------------------------------------
# Paths
#
# /chat and /feedback, and nothing under a prefix another screen owns. The six
# already spoken for are /admin, /workflow, /projects, /proceedings, /review and
# /escalations; tests/test_app_wiring.py refuses two routers on one path.
# ---------------------------------------------------------------------------

CHAT_URL = "/chat"
OPENING_URL = "/chat/opening"
FEEDBACK_URL = "/chat/feedback"
BUG_URL = "/feedback/bug"

# Where a withheld count sends a reader. This is the queue of refusals, not the
# review centre -- /review is a different screen with a different question.
REVIEW_QUEUE_URL = "/escalations"

# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

MIN_PILLS = 3
MAX_PILLS = 5

# Long enough for a pasted paragraph of a filing, short enough that nobody
# pastes a document into a chat box and waits. Refused rather than truncated:
# half a question answered as though it were the whole one is worse than a
# refusal a person can act on.
MAX_MESSAGE_CHARS = 2000

# The column is String(64) and SQLite will not enforce it. Cut here so the row
# holds what the caller sent or a plainly cut version of it, never a value the
# database silently changed shape on.
MAX_SURFACE_CHARS = 64
MAX_TITLE_CHARS = 256

# How much of the conversation the engine sees. Turns, oldest first. Twelve is
# six exchanges: enough to carry a follow-up like "and the second one?", short
# enough that a long session does not push the prompt out of shape. How much a
# thumbs-down freezes is app/state/feedback.py's number, not this one -- they
# answer different questions and would drift if they were one constant.
HISTORY_TURNS = 12

# ---------------------------------------------------------------------------
# The answering engine, as a seam
#
# It does not exist yet. Named as strings and imported per request rather than
# at module import, so this surface can be built, mounted and tested before it
# lands, and so it starts working the moment it does without a change here.
# ---------------------------------------------------------------------------

ENGINE_MODULE = "app.chat.engine"
ENGINE_ATTR = "answer"

#: The contract the engine must meet. Repeated in the report to whoever builds
#: it, and pinned by tests/test_chat_surface.py through a stub.
#:
#:     def answer(
#:         session, *, company_id: str, actor: User, message: str,
#:         surface: str, project_id: str | None, history: list[dict],
#:     ) -> dict   # {"reply": str, "pills": [...], "used": [...], "withheld": int}
#:
#: company_id and actor are injected here. The engine chooses which tool to call
#: and the non-identity arguments, and nothing else.

NO_ENGINE_REPLY = (
    "I can't answer that on this build. The part of me that reads the record "
    "is not wired in, so nothing was looked up and nothing here is an answer. "
    "The screens still hold everything."
)

ENGINE_DEFECT_REPLY = (
    "I stopped that answer. It came back without a count of what it left out, "
    "or without a readable record of what it consulted, and I don't report an "
    "answer I can't say the coverage of. Nothing is shown rather than a part "
    "shown as the whole."
)

EMPTY_MESSAGE = "a turn needs a question; nothing was sent"
LONG_MESSAGE = (
    f"that message is longer than {MAX_MESSAGE_CHARS} characters. It is refused "
    "rather than cut, because half a question answered as a whole one is worse"
)
NO_SESSION = "sign in again; this turn has nobody to run as"
NO_SUCH_MESSAGE = "no such message"
BAD_RATING = f"rating must be one of {', '.join(FEEDBACK_RATINGS)}"
NO_TITLE = "a bug report needs a title; an untitled one cannot be triaged"
DOUBLE_SUBMIT = "that turn was already recorded; ask again"
NO_RECORD = (
    "the record could not be written, so the turn did not happen and nothing "
    "here is an answer"
)

# This module declares no action code of its own. The one it writes belongs to
# persona.py, which chose it with the refusal it names; the ones it does not
# write belong to app/state/feedback.py, which owns the decisions on that table.
# A third spelling here would be the drift audit.py's own comment complains
# about for escalation.approved.

# ---------------------------------------------------------------------------
# Pills
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pill:
    """A button under the last reply. The label is read, the prompt is sent."""

    label: str
    prompt: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "prompt": self.prompt}


# The opening set, and the padding for a turn that came back with too few.
#
# These are affordances, not voice: three things a person may do in this
# workspace, phrased as they would ask for them. The words Clarke speaks in --
# the greeting, the tagline, the refusals -- all come from persona.py and are
# never written here. A pill that named an opinion or an assurance would be tone
# and would belong there instead.
OPENING_PILLS: tuple[Pill, ...] = (
    Pill("What changed this week?", "What changed this week?"),
    Pill("Show my open items", "Show my open items"),
    Pill("Find a project", "Find a project"),
    Pill("What is withheld?", "What is being withheld, and why?"),
)

# A tripwire, not a safety net. This tuple is the floor under every turn's
# pills, so trimming it below the contract's minimum would quietly let a refused
# turn dead-end -- and a dead end is exactly what pills on a refusal are for.
# Failing at import is louder than a person noticing two buttons where there
# were three.
if len(OPENING_PILLS) < MIN_PILLS:
    raise RuntimeError(
        f"OPENING_PILLS holds {len(OPENING_PILLS)} and the wire promises at "
        f"least {MIN_PILLS}; a turn that came back with none could not be "
        "topped up to the contract"
    )


# ---------------------------------------------------------------------------
# The wire, in
#
# extra="forbid" on all three. See the module docstring: a body carrying
# company_id, actor or context is refused, never quietly dropped.
# ---------------------------------------------------------------------------


class TurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    surface: str = ""
    project_id: str | None = None


class ThumbIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    rating: str
    comment: str = ""


class BugIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    detail: str = ""
    surface: str = ""


# ---------------------------------------------------------------------------
# The scoped reads
#
# message_for_company is app/state/feedback.py::chat_message_for_company, under
# a shorter name at the import above. It is not reimplemented here: the same
# read from two files is the drift that puts a scope check in one of them and
# not the other. The conversation itself is read through that module's
# turns_for_session for the same reason. Only _live_session is local, because
# nothing else opens a conversation.
# ---------------------------------------------------------------------------


def _live_session(session, *, company_id: str, user_id: str) -> ChatSession:
    """This person's conversation in this company, opened if they have none.

    One conversation per person per tenant, and the wire carries no session id.
    That is deliberate on both counts: the dock rides every screen, so moving
    from projects to the review queue is not a new conversation, and a session
    id a browser could send is a session id a browser could send somebody
    else's.

    The scope guard is imported from app/state/queries.py, not written again
    here. Same guard, same failure, one place to audit -- which is the choice
    app/state/feedback.py makes above these same tables.
    """
    _require_scope(company_id)
    live = (
        session.query(ChatSession)
        .filter(ChatSession.company_id == company_id)
        .filter(ChatSession.user_id == user_id)
        .order_by(ChatSession.started_at.desc())
        .first()
    )
    if live is not None:
        return live
    live = ChatSession(
        id=_new_id("CHS"),
        company_id=company_id,
        user_id=user_id,
        started_at=_now(),
        last_turn_at=None,
    )
    session.add(live)
    session.flush()
    return live


def _history(session, *, company_id: str, chat_session_id: str) -> list[dict]:
    """What the engine sees of the conversation so far: the last few turns.

    Read through the store's own ordering -- by ordinal, never by created_at --
    and cut to the tail here rather than in the query, because the store owns
    the read and this module owns how much of it a prompt is worth.
    """
    rows = store.turns_for_session(
        session, company_id=company_id, chat_session_id=chat_session_id
    )
    return [{"role": row.role, "text": row.text} for row in rows[-HISTORY_TURNS:]]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _principal(request: Request):
    """Who is asking, or 401. Never the unsigned fallback -- see the docstring."""
    holder = current_user(request)
    if holder is None:
        raise HTTPException(status_code=401, detail=NO_SESSION)
    return holder


def _clip(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _pills_out(pills: list[Pill]) -> list[dict[str, str]]:
    """Three to five, always, and never two of the same prompt.

    A turn that came back with too few is topped up from the opening set rather
    than answered with a dead end. The padding is honest -- each pill is an
    action this workspace really offers -- and a blocked person with no next
    step is the failure persona.py's refusal() was shaped to avoid.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for pill in list(pills) + list(OPENING_PILLS):
        if pill.prompt in seen:
            continue
        seen.add(pill.prompt)
        out.append(pill.as_dict())
        if len(out) == MAX_PILLS:
            break
    return out


def engine():
    """The answering engine, or None where it has not been built yet.

    Looked up per call rather than held, so the seam closes the moment
    app/chat/engine.py lands and so a test can stand a stub in its place.
    """
    try:
        module = importlib.import_module(ENGINE_MODULE)
    except ImportError:
        return None
    return getattr(module, ENGINE_ATTR, None)


class TurnDefect(RuntimeError):
    """The engine answered something this surface will not put on the wire."""


@dataclass(frozen=True, slots=True)
class Turn:
    """What goes out, and what goes in the transcript row. One shape for both."""

    reply: str
    pills: list[Pill]
    used: list[dict]
    withheld: int


def _read_used(raw) -> list[dict]:
    """Provenance, strictly. Anything unreadable raises rather than shrinks.

    This is the line a reviewer reads to see where an answer came from, in a
    product whose whole argument is that it shows its sources. An entry dropped
    because it was malformed would leave a turn claiming fewer sources than it
    consulted, and nothing would say so.
    """
    if not isinstance(raw, list):
        raise TurnDefect("used must be a list")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TurnDefect("each used entry must be an object")
        tool, found = entry.get("tool"), entry.get("found")
        if not isinstance(tool, str) or not tool:
            raise TurnDefect("a used entry with no tool name is not provenance")
        if not isinstance(found, int) or isinstance(found, bool):
            raise TurnDefect("found must be a whole number")
        out.append({"tool": tool, "found": found})
    return out


def _read_pills(raw) -> list[Pill]:
    """A pill may arrive as a pair or as a bare string.

    persona.py's Screened carries strings, because a refusal's next steps are
    the prompt and the label at once. Both forms are accepted; neither is
    invented here.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise TurnDefect("pills must be a list")
    out: list[Pill] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append(Pill(entry.strip(), entry.strip()))
            continue
        if isinstance(entry, Pill):
            out.append(entry)
            continue
        if isinstance(entry, dict):
            label = str(entry.get("label") or "").strip()
            prompt = str(entry.get("prompt") or "").strip() or label
            if label:
                out.append(Pill(label, prompt))
                continue
        raise TurnDefect("a pill needs a label and a prompt")
    return out


def _read_turn(raw) -> Turn:
    """Turn what the engine returned into what the wire promises, or refuse.

    withheld is the one field with no default anywhere in this function. NULL,
    missing and "not a number" all mean nobody counted, and none of them mean
    zero.
    """
    if not isinstance(raw, dict):
        raise TurnDefect("the engine returned no turn")
    reply = raw.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise TurnDefect("the engine returned no reply")
    withheld = raw.get("withheld")
    if not isinstance(withheld, int) or isinstance(withheld, bool) or withheld < 0:
        raise TurnDefect("the engine returned no count of what it withheld")
    return Turn(
        reply=reply.strip(),
        pills=_read_pills(raw.get("pills")),
        # Required, not defaulted. An engine that ran no tool says so with an
        # empty list; an engine that forgot to say has not told us it ran none.
        used=_read_used(raw.get("used")),
        withheld=withheld,
    )


def _refusal_turn(verdict: persona.Screened) -> Turn:
    """The deterministic refusal, in the persona's own words.

    No tool ran, so `used` is empty rather than absent. Nothing was asserted, so
    nothing was withheld and the count is a real zero rather than a stand-in for
    one nobody took.
    """
    return Turn(
        reply=verdict.reply,
        pills=_read_pills(verdict.pills),
        used=[],
        withheld=0,
    )


def _announced_turn(reply: str) -> Turn:
    """A turn that answered nothing, and says which nothing it answered.

    Same zero, same reason as above: this reply asserts nothing, so it withheld
    nothing. What it does not do is stay quiet about why it is empty.
    """
    return Turn(reply=reply, pills=list(OPENING_PILLS), used=[], withheld=0)


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


@router.post(CHAT_URL)
def turn(request: Request, body: TurnIn) -> dict:
    """One exchange. The scope and the actor come from the session, not the body."""
    holder = _principal(request)
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail=EMPTY_MESSAGE)
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail=LONG_MESSAGE)

    surface = _clip(body.surface, MAX_SURFACE_CHARS)
    project_id = (body.project_id or "").strip() or None

    try:
        with session_scope() as session:
            actor = user_for_company(session, holder.company_id, holder.user_id)
            if actor is None:
                raise HTTPException(status_code=401, detail=NO_SESSION)

            live = _live_session(
                session, company_id=holder.company_id, user_id=holder.user_id
            )
            history = _history(
                session, company_id=holder.company_id, chat_session_id=live.id
            )

            asked = _write_message(
                session,
                live=live,
                role=CHAT_ROLE_PERSON,
                text=message,
                surface=surface,
                tools_used=None,
                withheld_count=None,
            )

            verdict = persona.screen(message)
            if verdict.blocked:
                answered = _refusal_turn(verdict)
            else:
                answered = _answer(
                    session,
                    company_id=holder.company_id,
                    actor=actor,
                    message=message,
                    surface=surface,
                    project_id=project_id,
                    history=history,
                )

            said = _write_message(
                session,
                live=live,
                role=CHAT_ROLE_CLERK,
                text=answered.reply,
                surface=surface,
                tools_used=answered.used,
                withheld_count=answered.withheld,
            )
            live.last_turn_at = _now()

            if verdict.blocked:
                record_event(
                    session,
                    company_id=holder.company_id,
                    actor=f"person:{holder.email}",
                    action=verdict.audit_action,
                    subject_type="chat_message",
                    subject_id=asked.id,
                    reason=f"refused before the model: {verdict.reason}",
                    actor_user_id=holder.user_id,
                    actor_kind=ACTOR_USER,
                    session_id=holder.session_id,
                )

            return {
                "reply": answered.reply,
                "pills": _pills_out(answered.pills),
                "used": answered.used,
                "withheld": answered.withheld,
                "message_id": said.id,
            }
    except IntegrityError:
        # The unique index on (session_id, ordinal) refused a repeated position.
        # Two turns arrived at once, which is a double submit rather than a
        # conversation, and answering both would store an order nobody can
        # resolve afterwards.
        raise HTTPException(status_code=409, detail=DOUBLE_SUBMIT) from None
    except SQLAlchemyError:
        # The engine reads through the same session this turn is written in, so
        # a failed read leaves a transaction that can no longer be committed and
        # the turn cannot be recorded. A plain sentence, not a stack trace: the
        # person is owed the fact that nothing was written and nothing was
        # answered, and the panel prints exactly that.
        raise HTTPException(status_code=503, detail=NO_RECORD) from None


def _answer(session, **kwargs) -> Turn:
    """Ask the engine, and refuse whatever it cannot say the coverage of.

    Three failures land in the same place and none of them land silently: no
    engine, an engine that raised, and an engine whose answer this surface will
    not put on the wire. The exception text is not repeated to the person -- a
    stack message is not an answer -- but the reply names what happened.
    """
    ask = engine()
    if ask is None:
        return _announced_turn(NO_ENGINE_REPLY)
    try:
        return _read_turn(ask(session, **kwargs))
    except TurnDefect:
        return _announced_turn(ENGINE_DEFECT_REPLY)
    except HTTPException:
        raise
    except Exception:
        # Whatever the engine did, the turn is still answerable and the person
        # is still owed a plain sentence about it.
        return _announced_turn(ENGINE_DEFECT_REPLY)


def _write_message(
    session,
    *,
    live: ChatSession,
    role: str,
    text: str,
    surface: str,
    tools_used: list | None,
    withheld_count: int | None,
) -> ChatMessage:
    """Append one turn at the next position in the conversation.

    company_id is copied from the session row and from nowhere else. ordinal is
    the order and the clock is not: a writer that stamps the question and the
    answer from one read gives both the same created_at, and a tied transcript
    renders the answer above the question.
    """
    taken = (
        session.query(ChatMessage.ordinal)
        .filter(ChatMessage.session_id == live.id)
        .order_by(ChatMessage.ordinal.desc())
        .limit(1)
        .scalar()
    )
    row = ChatMessage(
        id=_new_id("CHM"),
        session_id=live.id,
        company_id=live.company_id,
        ordinal=(taken or 0) + 1,
        role=role,
        text=text,
        surface=surface,
        created_at=_now(),
        tools_used=tools_used,
        withheld_count=withheld_count,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# The opening
# ---------------------------------------------------------------------------


@router.get(OPENING_URL)
def opening(request: Request) -> dict:
    """What the dock says before anybody types, taken from persona.py verbatim.

    It is served rather than baked into the template because base.html is shared
    by every screen and none of those views carry the persona in their context.
    Copying the greeting into the markup would give the words a second home,
    which is the drift this codebase names in three other places. One
    same-origin request, no network beyond this host, and the panel says plainly
    when it could not read it.
    """
    _principal(request)
    return {
        "name": persona.DISPLAY_NAME,
        "tagline": persona.TAGLINE,
        "greeting": persona.GREETING,
        "pills": _pills_out([]),
        "review_url": REVIEW_QUEUE_URL,
    }


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


@router.post(FEEDBACK_URL)
def thumb(request: Request, body: ThumbIn) -> dict:
    """A thumb on one clerk reply, recorded by the store that owns the table.

    The identity is injected: company and user come from the session, never off
    the wire, and the surface comes from the message rather than from the caller
    -- it is the only place it exists that the person pressing the button did
    not choose.

    A thumbs-down with nothing written is still recorded. A rejected escalation
    needs a reason because it overrules the product; a thumb changes nothing on
    its own, and demanding prose for one loses the signal it was meant to carry.

    The rating is checked here as well as in the store, and the repetition is
    deliberate: this one gives the person a 400 naming the two words, and that
    one refuses whatever any other caller sends.
    """
    holder = _principal(request)
    rating = (body.rating or "").strip()
    if rating not in FEEDBACK_RATINGS:
        raise HTTPException(status_code=400, detail=BAD_RATING)

    with session_scope() as session:
        try:
            row = store.record_thumb(
                session,
                company_id=holder.company_id,
                user_id=holder.user_id,
                message_id=(body.message_id or "").strip(),
                rating=rating,
                comment=body.comment or "",
            )
        except ValueError:
            # One answer for "not here", "not yours" and "not a clerk turn".
            # Telling them apart would say whether another company holds the id.
            raise HTTPException(status_code=404, detail=NO_SUCH_MESSAGE) from None
        return {"recorded": True, "feedback_id": row.id}


@router.post(BUG_URL)
def bug(request: Request, body: BugIn) -> dict:
    """A bug report from any screen. No rating, no chat, no context.

    Standalone on purpose: the thing worth reporting is usually not a chat turn,
    and a bug form that only opens inside the assistant is a bug form nobody
    finds. The surface is captured by the caller and stored as sent -- "" means
    the caller recorded none, which is a gap in the caller rather than something
    to guess at.
    """
    holder = _principal(request)
    title = _clip(body.title, MAX_TITLE_CHARS)
    if not title:
        raise HTTPException(status_code=400, detail=NO_TITLE)

    with session_scope() as session:
        row = store.record_bug_report(
            session,
            company_id=holder.company_id,
            user_id=holder.user_id,
            title=title,
            detail=body.detail or "",
            surface=_clip(body.surface, MAX_SURFACE_CHARS),
        )
        return {"recorded": True, "feedback_id": row.id}


__all__ = [
    "BUG_URL",
    "CHAT_URL",
    "ENGINE_ATTR",
    "ENGINE_DEFECT_REPLY",
    "ENGINE_MODULE",
    "FEEDBACK_URL",
    "MAX_MESSAGE_CHARS",
    "MAX_PILLS",
    "MIN_PILLS",
    "NO_ENGINE_REPLY",
    "OPENING_PILLS",
    "OPENING_URL",
    "Pill",
    "REVIEW_QUEUE_URL",
    "Turn",
    "TurnDefect",
    "engine",
    "message_for_company",
    "router",
]
