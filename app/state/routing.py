"""Who owns this, and what to do when nobody can say.

An analyst opens a shared queue of fourteen withheld items and cannot tell which
are theirs. The one that is theirs waits behind thirteen that are not, and the
deadline that matters is on the one at the bottom. This module answers "whose is
this" by walking the escalation to the claim, the claim to the change, the change
to the company obligations it bears on, and the obligation to its owner.

ABSENCE IS DENIAL, AND THAT IS THE WHOLE POINT OF THIS FILE.

Every function here can fail to name a person, and none of them may guess when
they do. No obligation maps to the change, the obligation has no owner, the
owner's account was deleted or belongs to another tenant, the owner is
suspended, two obligations name two different people -- each is a refusal with
its own reason code, and after each one the escalation is STILL in the shared
queue with nobody's name on it.

AND ONE MORE, WHICH IS THE NEWEST AND THE LEAST OBVIOUS. The walk can reach a
real person, at a live account, who owns the duty -- and still refuse, because
nobody ever agreed the change bears on that duty. Only the pipeline said so, out
of a lexical word overlap. ROUTE_MAPPING_UNCONFIRMED is that refusal, and what
sets it apart is easy to state wrongly, so it is stated carefully. Every other
refusal is about A HOP IN THE WALK THAT DOES NOT HOLD. The step is missing (no
claim, no mapped duty, no owner, no account); or it reaches somebody who cannot
act (a suspended account, an invitation that ran out); or it forks and nothing
here can settle it (two owners, several role holders). This one holds at every
hop. The walk runs end to end and arrives at a live person who owns the duty,
and the refusal is about the AUTHORITY of the first step rather than about the
step -- a word overlap is not somebody's judgement. Work resting on an
unconfirmed mapping is work nobody has agreed is theirs, and handing it over is
the same act as guessing, performed one step earlier.

That paragraph first said the other refusals "all describe something missing",
and an adversarial reviewer named two that do not: ROUTE_OWNERS_DISAGREE is
about two owners being present and ROUTE_ROLE_AMBIGUOUS about too many role
holders. The two suspended-account codes describe something present as well. A
docstring that overstates its own rule is worse than a silent one, because the
next reader applies the rule and does not reread the code: told that every other
refusal is about an absence, they would go looking for the absence behind
ROUTE_OWNERS_DISAGREE and find a room with two people in it.

The temptation this file exists to refuse is the default assignee: the admin,
the person who raised it, the last person who touched anything nearby. Each is
one line of code and each is worse than the refusal, because a wrong assignment
looks handled. It leaves the queue, sits on a desk that will not act on it, and
nobody finds out until the deadline has gone. A refusal stays visible and says
what would fix it.

WHY THE REASON HAS A CODE AND NOT JUST A SENTENCE. "Could not route" collapses
five different fixes into one line nobody can act on. ROUTE_OBLIGATION_UNOWNED
means give the duty an owner; ROUTE_OWNER_INACTIVE means reinstate an account or
move the duty; ROUTE_OWNERS_DISAGREE means a person has to choose. Code branches
on the code; the analyst reads the text.

WHY THE REASON IS RE-DERIVED AT READ TIME. shared_queue() re-resolves every item
it shows rather than reading a stored verdict, for the reason app/state/claims.py
gives about verification: a stored verdict is a promise about rows that may have
changed since. Suspending an owner this morning must change what the queue says
this afternoon without anything having re-run. The audit row records what was
decided at the moment of routing; the queue says what is true now. Both are
wanted and they answer different questions.

THE ONE DEAD END THAT IS NOT ONE. An obligation whose owner is a name in
data/company_context.json with no account here used to refuse for ever: nobody
to route to, item in the shared queue, nothing anybody could do inside the
product to change it. app/state/invites.py closes that by creating the account
at STATUS_INVITED and this module then routes to it -- but the item is NOT
moving until that person accepts, and ROUTE_PENDING_ACCEPTANCE is how it says
so. The distinction is carried as data (Resolution.pending_acceptance,
invitation_id, invited_at) rather than as a sentence, because a screen that
renders "waiting on Priya Nandakumar" and "waiting on Priya Nandakumar, invited
two days ago, not yet accepted" the same way is lying about whether the work is
moving. awaiting_acceptance() is the queue for it, and it is the second half of
shared_queue: between them, no item is both stuck and invisible.

A ROLE RESOLVES ONLY WHEN EXACTLY ONE PERSON HOLDS IT, and that is a real limit
rather than a subtlety. WorkflowStepRun.assigned_to_user_id holds one account,
so "role:obligation_owner" in a company with five obligation owners has no single
answer and this module refuses instead of picking. The proper fix is a column
that lets a step run be held by a role rather than a person -- a queue several
people can see and one of them can claim -- and it is not built. Said here rather
than discovered by an admin whose route will not route.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.state.audit import ACTOR_SYSTEM, ACTOR_USER, record_event
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    ASSIGNEE_ROLE_PREFIX,
    ASSIGNEE_UNASSIGNED,
    ASSIGNEE_USER_PREFIX,
    AUTHOR_ANALYST,
    AUTHOR_SYSTEM,
    INVITE_APPROVED,
    INVITE_PENDING,
    STATUS_ACTIVE,
    STATUS_INVITED,
    TURN_AUTHOR_KINDS,
    Change,
    ChangeObligation,
    Claim,
    Escalation,
    Invitation,
    Obligation,
    Role,
    User,
    UserRole,
)

# Imported, never copied. Two spellings of one tenant guard drift, and the point
# of a chokepoint is that there is only one. row_for_company is the second guard
# and the newer one: it fetches a row by primary key and refuses to hand it to
# anybody but its owner, so the check cannot be left out of a call site the way
# it was left out of ensure_obligation below.
from app.state.queries import CrossTenantRow, _require_scope, row_for_company

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE BELONG IN app/state/audit.py, BESIDE THE REST OF THE VOCABULARY, AND
# MUST BE MOVED THERE RATHER THAN RESTATED. They sit here because that file was
# being edited by somebody else while this one was written, and a second copy of
# an action string is exactly the failure the ACTION_ constants were
# consolidated to stop: a row written under the wrong spelling verifies
# perfectly and is invisible to every query for the right one. When they move,
# this block is deleted and the import changes; nothing else does.
# ---------------------------------------------------------------------------

# The two halves of one decision, kept apart on purpose. escalation.routed means
# a person now holds this item. escalation.unrouted means the product looked and
# could not say who, which is a decision it made and has to answer for. One code
# with the outcome buried in the reason text would make "what did we fail to
# route, and why" a question that needs prose parsed rather than rows filtered.
ACTION_ESCALATION_ROUTED = "escalation.routed"
ACTION_ESCALATION_UNROUTED = "escalation.unrouted"

# The third outcome, and it is not a shade of either of the other two. The item
# is on a named desk and the person at that desk has not accepted their
# invitation, so nobody who can act holds it yet. Folding it into
# escalation.routed would make "what did we hand to somebody who never turned
# up" a question that needs the reason text parsed rather than rows filtered --
# and those are exactly the items that stall in silence, because the queue that
# shows unrouted work no longer shows them.
ACTION_ESCALATION_ROUTED_PENDING = "escalation.routed_pending"

# A change bearing on an obligation is the load-bearing judgement in this
# product -- the regulator's words and the company's do not share a vocabulary
# -- so it is appended to the chain even though ChangeObligation carries its own
# attribution columns. The columns say who; the chain says when, in an order
# nobody can edit afterwards.
ACTION_OBLIGATION_MAPPED = "obligation.mapped"
ACTION_OBLIGATION_OWNER_SET = "obligation.owner_set"


# ---------------------------------------------------------------------------
# Reason codes
#
# One code per fix. A caller branches on the code and shows the text.
# ---------------------------------------------------------------------------

ROUTE_OK = "ROUTE_OK"

# Somebody's name is on this and they have not accepted their invitation yet.
# It is an outcome, not a refusal: the item has left the shared queue and is on
# a desk. It is a SEPARATE CODE from ROUTE_OK because "waiting on Priya
# Nandakumar" and "waiting on Priya Nandakumar, invited two days ago, not yet
# accepted" are different facts about whether the work is moving, and a surface
# that renders them alike is lying about the second one. A caller that only
# knows ROUTE_OK sees a code it does not recognise and shows the reason text,
# which says both halves -- the conservative failure, not the flattering one.
ROUTE_PENDING_ACCEPTANCE = "ROUTE_PENDING_ACCEPTANCE"

# The walk from the escalation to a change broke.
ROUTE_NO_ESCALATION = "ROUTE_NO_ESCALATION"
ROUTE_NO_CLAIM = "ROUTE_NO_CLAIM"
ROUTE_NO_CHANGE = "ROUTE_NO_CHANGE"

# The change reached no owner.
ROUTE_NO_OBLIGATION = "ROUTE_NO_OBLIGATION"
ROUTE_MAPPING_UNCONFIRMED = "ROUTE_MAPPING_UNCONFIRMED"
ROUTE_OBLIGATION_UNOWNED = "ROUTE_OBLIGATION_UNOWNED"
ROUTE_OWNER_UNKNOWN = "ROUTE_OWNER_UNKNOWN"
ROUTE_OWNER_INACTIVE = "ROUTE_OWNER_INACTIVE"
ROUTE_OWNERS_DISAGREE = "ROUTE_OWNERS_DISAGREE"

# The assignee rule on a workflow step reached no account.
ROUTE_RULE_UNASSIGNED = "ROUTE_RULE_UNASSIGNED"
ROUTE_RULE_UNKNOWN = "ROUTE_RULE_UNKNOWN"
ROUTE_ROLE_UNKNOWN = "ROUTE_ROLE_UNKNOWN"
ROUTE_ROLE_EMPTY = "ROUTE_ROLE_EMPTY"
ROUTE_ROLE_AMBIGUOUS = "ROUTE_ROLE_AMBIGUOUS"
ROUTE_USER_UNKNOWN = "ROUTE_USER_UNKNOWN"
ROUTE_USER_INACTIVE = "ROUTE_USER_INACTIVE"

# The codes under which an item is on somebody's desk. Resolution.routed reads
# this tuple rather than comparing against ROUTE_OK, so adding an outcome here
# is one edit rather than one per call site -- and a caller that checks .routed
# is asking "has this left the queue", which both of these answer yes to.
ROUTE_OK_CODES = (ROUTE_OK, ROUTE_PENDING_ACCEPTANCE)

# The one wording of "a word overlap is not somebody's judgement", and it lives
# here rather than on a screen because more than one surface has to say it.
#
# IT USED TO BE app/web/views/changes.py's, AND THAT IS THE FAILURE THAT MOVED
# IT. Routing did not read mapped_by_kind, so the change screen carried this
# sentence alone -- and the escalation queue and the approval route, which are
# the surfaces that actually tell a person the work is theirs, said the
# confident half with nothing after it. A caveat that only appears on the page
# nobody was reading is not a caveat. The screen imports this now; there is no
# second copy and no alias.
#
# It is a bare sentence rather than a template because every caller wants it
# whole, and it is deliberately free of "above" and "below": it is read in a
# card, in a queue row and in an audit reason, and only one of those has a
# layout.
UNCONFIRMED_MAPPING = (
    "Nobody has confirmed the mapping this name rests on. The pipeline proposed "
    "it from a word overlap, so read the owner as a suggestion rather than as "
    "accountability until somebody confirms it."
)

ROUTING_REASON_CODES = (
    ROUTE_OK,
    ROUTE_PENDING_ACCEPTANCE,
    ROUTE_NO_ESCALATION,
    ROUTE_NO_CLAIM,
    ROUTE_NO_CHANGE,
    ROUTE_NO_OBLIGATION,
    ROUTE_MAPPING_UNCONFIRMED,
    ROUTE_OBLIGATION_UNOWNED,
    ROUTE_OWNER_UNKNOWN,
    ROUTE_OWNER_INACTIVE,
    ROUTE_OWNERS_DISAGREE,
    ROUTE_RULE_UNASSIGNED,
    ROUTE_RULE_UNKNOWN,
    ROUTE_ROLE_UNKNOWN,
    ROUTE_ROLE_EMPTY,
    ROUTE_ROLE_AMBIGUOUS,
    ROUTE_USER_UNKNOWN,
    ROUTE_USER_INACTIVE,
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """Who this goes to, or why nobody can be named.

    user_id is not None if and only if reason_code is one of ROUTE_OK_CODES.
    The `routed` property reads both, so a caller that checks one is checking
    the other and a half-built Resolution cannot be mistaken for a routed one.

    candidate_user_ids is who was considered and not chosen. It is populated on
    the two ambiguity refusals, because an analyst asked to break the tie needs
    the names -- a refusal that will not say who it was choosing between is a
    refusal nobody can clear.

    invitation_id AND invited_at ARE HERE SO NO TEMPLATE HAS TO ASSEMBLE THE
    SENTENCE. "waiting on Priya Nandakumar, invited 2 days ago, not yet
    accepted" is three facts, and a screen given only reason_text would have to
    parse prose to render them apart, sort by them, or count them. Both are
    NULL on every other outcome, because on every other outcome there is no
    invitation to describe.
    """

    reason_code: str
    reason_text: str
    user_id: str | None = None
    obligation_ids: tuple[str, ...] = ()
    candidate_user_ids: tuple[str, ...] = ()
    invitation_id: str | None = None
    invited_at: datetime | None = None

    @property
    def routed(self) -> bool:
        return self.reason_code in ROUTE_OK_CODES and self.user_id is not None

    @property
    def pending_acceptance(self) -> bool:
        """True when a person holds this and has not accepted their invitation.

        Derived from the code rather than stored beside it, so the two cannot
        disagree. A caller shows "waiting on X" for routed-and-not-pending and
        "waiting on X, invited <invited_at>, not yet accepted" for this one.
        """
        return self.reason_code == ROUTE_PENDING_ACCEPTANCE


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One escalation nobody holds, and the live reason it is still there."""

    escalation: Escalation
    resolution: Resolution


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_actor(actor: str) -> str:
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    return actor


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone; a naive timestamp is a defect")
    return moment


def _refused(code: str, text: str, **extra) -> Resolution:
    return Resolution(reason_code=code, reason_text=text, user_id=None, **extra)


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


def obligations_for_company(session: Session, company_id: str) -> list[Obligation]:
    _require_scope(company_id)
    return (
        session.query(Obligation)
        .filter(Obligation.company_id == company_id)
        .order_by(Obligation.id)
        .all()
    )


def obligation_for_company(
    session: Session, company_id: str, obligation_id: str
) -> Obligation | None:
    """One obligation, or None. Another company's id resolves to None here."""
    _require_scope(company_id)
    if not obligation_id:
        return None
    return (
        session.query(Obligation)
        .filter(Obligation.company_id == company_id)
        .filter(Obligation.id == obligation_id)
        .one_or_none()
    )


def unowned_obligations(session: Session, company_id: str) -> list[Obligation]:
    """Duties with nobody's name on them. Work, not a tidy empty column."""
    _require_scope(company_id)
    return (
        session.query(Obligation)
        .filter(Obligation.company_id == company_id)
        .filter(Obligation.owner_user_id.is_(None))
        .order_by(Obligation.id)
        .all()
    )


def obligations_for_owner(
    session: Session, company_id: str, owner_user_id: str
) -> list[Obligation]:
    _require_scope(company_id)
    if not owner_user_id:
        return []
    return (
        session.query(Obligation)
        .filter(Obligation.company_id == company_id)
        .filter(Obligation.owner_user_id == owner_user_id)
        .order_by(Obligation.id)
        .all()
    )


def ensure_obligation(
    session: Session,
    company_id: str,
    *,
    obligation_id: str,
    title: str,
    actor: str,
    owner_user_id: str | None = None,
    project_id: str | None = None,
    source_document_ref: str | None = None,
) -> Obligation:
    """Write a duty if it is not stored, and otherwise leave the stored one alone.

    Idempotent, and deliberately not an upsert. A stored obligation may have been
    moved to a different owner inside the product, and a loader that ran again on
    the next start and put the corpus's owner back would take somebody's work off
    their desk without saying so. The corpus supplies a duty once; after that the
    product owns it.

    Filling in a missing owner later is a different operation with its own
    function, because a NULL owner is an absence to close and a stored owner is
    a decision to respect.

    THE ID IS CHECKED AGAINST THIS COMPANY BEFORE THE ROW IS HANDED BACK, and
    that check was missing. This function fetched the stored row by primary key
    and returned it, having run _require_scope -- which validates the STRING the
    caller passed and says nothing whatever about the ROW about to be returned.
    Obligation ids are unique across tenants and the corpus supplies them, so
    another company loading OBL-001 first meant this company's loader got their
    title, their owner and their project back, and every escalation that walked
    change to obligation to owner routed this company's work to their desk.

    It was latent rather than live. scripts/seed_demo_gaps.py is the only caller
    outside the tests, it passes one company, and no test covered the crossing
    -- which is why it survived: nothing was going to find it. Latent is the
    reason it was still there, not a reason to leave it, and a product whose
    argument is tenant isolation does not get to ship a hole on the grounds that
    nothing has walked into it yet.

    Two sibling call sites did the same primary-key fetch and remembered the
    post-check independently. That is a rule three people have to hold in their
    heads, and it had already failed once, so the fetch and the check are now one
    call -- app/state/queries.py::row_for_company -- and all three sites use it.
    """
    _require_scope(company_id)
    _require_actor(actor)
    if not obligation_id:
        raise ValueError("an obligation needs an id; the corpus supplies it")
    if not title:
        raise ValueError(
            "an obligation needs the company's own wording; a duty nobody can "
            "read is not a duty anybody can own"
        )

    # Re-raised in this module's own words rather than let through, because the
    # caller here is a loader reading a corpus and the sentence it needs is
    # about an obligation, not about a row. CrossTenantRow is a ValueError, so a
    # caller catching either catches this. What the message must not do is say
    # who does hold the id: it reads exactly as a missing row does, which is all
    # the asker is entitled to know.
    try:
        stored = row_for_company(session, company_id, Obligation, obligation_id)
    except CrossTenantRow as crossed:
        raise ValueError(
            f"no obligation {obligation_id!r} for this company"
        ) from crossed
    if stored is not None:
        return stored

    obligation = Obligation(
        id=obligation_id,
        company_id=company_id,
        title=title,
        owner_user_id=owner_user_id or None,
        project_id=project_id or None,
        source_document_ref=source_document_ref or None,
    )
    session.add(obligation)
    session.flush()
    return obligation


def set_obligation_owner(
    session: Session,
    company_id: str,
    *,
    obligation_id: str,
    owner_user_id: str | None,
    actor: str,
    only_when_unowned: bool = False,
    occurred_at: datetime | None = None,
) -> Obligation:
    """Put a name on a duty, or take one off. Audited either way.

    only_when_unowned is what a loader passes. It fills a NULL and refuses to
    move a duty that already has somebody on it, so running the seed again after
    a reassignment is a no-op rather than a quiet reversal.

    Setting the owner already stored writes nothing and appends no event.
    Re-stating a fact is not a decision, and a chain full of them is a chain
    nobody reads -- set_user_status makes the same choice for the same reason.
    """
    _require_scope(company_id)
    who = _require_actor(actor)

    obligation = obligation_for_company(session, company_id, obligation_id)
    if obligation is None:
        raise ValueError(f"no obligation {obligation_id!r} for this company")

    if only_when_unowned and obligation.owner_user_id is not None:
        return obligation
    if obligation.owner_user_id == (owner_user_id or None):
        return obligation

    was = obligation.owner_user_id
    obligation.owner_user_id = owner_user_id or None
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_OBLIGATION_OWNER_SET,
        subject_type="obligation",
        subject_id=obligation.id,
        reason=(
            f"owner moved from {was or 'nobody'} to "
            f"{obligation.owner_user_id or 'nobody'}"
        ),
        occurred_at=occurred_at,
    )
    session.flush()
    return obligation


# ---------------------------------------------------------------------------
# The map between a docket change and the company's own duties
# ---------------------------------------------------------------------------


def map_change_to_obligation(
    session: Session,
    company_id: str,
    *,
    change_id: str,
    obligation_id: str,
    mapped_by: str,
    mapped_by_kind: str = AUTHOR_SYSTEM,
    mapped_at: datetime | None = None,
    actor_user_id: str | None = None,
    note: str = "",
) -> ChangeObligation:
    """Record that one docket change bears on one company obligation.

    Idempotent on the pair: the same mapping twice is a double click, it returns
    the stored row and it appends nothing to the chain. attach_change takes the
    same line for the same reason.

    Both ends are checked against this company before the row is written. The
    unique index would stop a duplicate, but nothing in the schema stops a
    mapping from naming another tenant's change, and a mapping written across
    the boundary would route this company's work to that company's owner.

    actor_user_id AND note ARRIVED WITH THE FIRST CALLER THAT WAS A PERSON. Until
    app/state/mapping.py there was none: every mapping this function could ever
    have written was the pipeline's, so a bare actor string said everything there
    was to say and the chain row was hashed under digest scheme 2 with a NULL
    actor. A mapping a person makes is the load-bearing judgement in this
    product, and a chain entry for it that cannot name the account is an entry
    whose actor can be rewritten without the hash noticing -- which is what
    _digest_v2 exists to stop. Both default to the old behaviour, so no existing
    call site changes.

    note is appended to the reason rather than replacing it, so every row still
    starts with the same eight words and a query for the action still finds
    them all. What the caller adds is the sentence only it knows: whether the
    words proposed this mapping or a person reached past them.
    """
    _require_scope(company_id)
    who = _require_actor(mapped_by)
    if mapped_by_kind not in TURN_AUTHOR_KINDS:
        raise ValueError(
            f"mapped_by_kind must be one of {TURN_AUTHOR_KINDS}; got "
            f"{mapped_by_kind!r}. A mapping a person made and one the pipeline "
            "proposed must not read alike."
        )

    change = (
        session.query(Change)
        .filter(Change.company_id == company_id)
        .filter(Change.id == change_id)
        .one_or_none()
    )
    if change is None:
        raise ValueError(f"no change {change_id!r} for this company")
    if obligation_for_company(session, company_id, obligation_id) is None:
        raise ValueError(f"no obligation {obligation_id!r} for this company")

    stored = (
        session.query(ChangeObligation)
        .filter(ChangeObligation.change_id == change_id)
        .filter(ChangeObligation.obligation_id == obligation_id)
        .one_or_none()
    )
    if stored is not None:
        return stored

    stamp = _require_aware(mapped_at or _utcnow(), "mapped_at")
    row = ChangeObligation(
        company_id=company_id,
        change_id=change_id,
        obligation_id=obligation_id,
        mapped_at=stamp,
        mapped_by=who,
        mapped_by_kind=mapped_by_kind,
    )
    session.add(row)
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_OBLIGATION_MAPPED,
        subject_type="change",
        subject_id=change_id,
        reason=(
            f"bears on {obligation_id} ({mapped_by_kind})"
            + (f"; {note}" if note else "")
        ),
        actor_user_id=actor_user_id,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    session.flush()
    return row


def obligations_for_change(
    session: Session, company_id: str, change_id: str
) -> list[Obligation]:
    """Every duty this change bears on, for this company only.

    Scoped on three columns -- the change's company, the mapping's company and
    the obligation's company. Each was written by a different call, and a bug in
    any one of them routes one tenant's work to another tenant's owner.
    """
    _require_scope(company_id)
    if not change_id:
        return []
    return (
        session.query(Obligation)
        .join(ChangeObligation, ChangeObligation.obligation_id == Obligation.id)
        .join(Change, Change.id == ChangeObligation.change_id)
        .filter(Change.company_id == company_id)
        .filter(ChangeObligation.company_id == company_id)
        .filter(Obligation.company_id == company_id)
        .filter(ChangeObligation.change_id == change_id)
        .order_by(Obligation.id)
        .all()
    )


def mappings_for_change(
    session: Session, company_id: str, change_id: str
) -> list[ChangeObligation]:
    """The mapping rows themselves, not the duties behind them.

    obligations_for_change answers "what does this bear on"; this answers "who
    said so, and when". They are different questions and the second one only
    became askable when something other than a loader started writing rows:
    app/state/mapping.py has to tell a candidate the pipeline offered from a
    mapping a person confirmed, and mapped_by_kind lives on this row rather than
    on the obligation.

    It is here rather than in the module that reads it, so the change_obligations
    table has ONE file that queries it. A second reader elsewhere would be a
    second copy of this tenant filter, and the copy nobody audited is the one
    that leaks.
    """
    _require_scope(company_id)
    if not change_id:
        return []
    return (
        session.query(ChangeObligation)
        .filter(ChangeObligation.company_id == company_id)
        .filter(ChangeObligation.change_id == change_id)
        .order_by(ChangeObligation.obligation_id)
        .all()
    )


def changes_for_obligation(
    session: Session, company_id: str, obligation_id: str
) -> list[Change]:
    """Every docket change bearing on one duty. The owner's own view of it."""
    _require_scope(company_id)
    if not obligation_id:
        return []
    return (
        session.query(Change)
        .join(ChangeObligation, ChangeObligation.change_id == Change.id)
        .filter(Change.company_id == company_id)
        .filter(ChangeObligation.company_id == company_id)
        .filter(ChangeObligation.obligation_id == obligation_id)
        .order_by(Change.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Resolving a person
# ---------------------------------------------------------------------------


def _active_user(session: Session, company_id: str, user_id: str) -> User | None:
    return (
        session.query(User)
        .filter(User.company_id == company_id)
        .filter(User.id == user_id)
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# The one predicate for "is this invitation still live"
#
# It lives here, in the module that has to decide whether an account at
# STATUS_INVITED is a pending handoff or a dead end, and app/state/invites.py
# imports it rather than writing a second copy. Two spellings of one predicate
# is the drift this codebase refuses everywhere else: one would call an expired
# invitation live and the queue would show work as moving that nobody can touch.
# The arrow runs invites -> routing and never back, so there is no cycle.
# ---------------------------------------------------------------------------

# Both live statuses. pending is a same-domain invitation nobody had to
# approve; approved is a cross-domain one a holder of user.manage released. They
# are different facts about how the person got in and the same fact about
# whether they may still accept.
INVITE_LIVE_STATUSES = (INVITE_PENDING, INVITE_APPROVED)


def invitation_is_live(invitation: Invitation | None, moment: datetime) -> bool:
    """True when this invitation can still be accepted at `moment`.

    Expiry is decided here rather than by a sweep, for the reason given at the
    head of this file about re-deriving verdicts: an invitation that runs out
    overnight has to change what the queue says in the morning whether or not
    any job ran. Nothing writes the expired status; a row simply stops being
    live, and any writer that later sets the status agrees with this function
    rather than replacing it.
    """
    if invitation is None:
        return False
    if invitation.status not in INVITE_LIVE_STATUSES:
        return False
    if invitation.accepted_at is not None:
        return False
    return invitation.expires_at > moment


def invitation_for_invited_user(
    session: Session, company_id: str, *, user_id: str
) -> Invitation | None:
    """The newest invitation this company issued that created that account.

    Scoped on the company as well as the account, because invitations.id and
    users.id are unique across tenants and nothing in the schema stops a row
    naming the other side of the boundary. Newest first: revoke and re-invite
    are two legitimate rows for one person, and the live one is the last.
    """
    _require_scope(company_id)
    if not user_id:
        return None
    return (
        session.query(Invitation)
        .filter(Invitation.company_id == company_id)
        .filter(Invitation.invited_user_id == user_id)
        .order_by(Invitation.invited_at.desc(), Invitation.id.desc())
        .first()
    )


def _invited_owner(
    session: Session,
    company_id: str,
    user: User,
    moment: datetime,
    *,
    obligation_ids: tuple[str, ...] = (),
) -> Resolution:
    """An account at STATUS_INVITED: a pending handoff, or a dead end.

    Live invitation -- the item is theirs and is NOT moving, and the resolution
    says both. No live invitation -- the invitation was revoked or ran out, the
    account can never sign in, and this is the same dead end a suspended owner
    is. It refuses, and the item goes back to the shared queue rather than
    sitting on a desk nobody occupies.
    """
    invitation = invitation_for_invited_user(session, company_id, user_id=user.id)
    if not invitation_is_live(invitation, moment):
        return _refused(
            ROUTE_OWNER_INACTIVE,
            f"{user.display_name} was invited and their invitation is no longer "
            "live, so the account cannot sign in and cannot act. Invite them "
            "again or give the duty to somebody who is here.",
            obligation_ids=obligation_ids,
            candidate_user_ids=(user.id,),
        )
    return Resolution(
        reason_code=ROUTE_PENDING_ACCEPTANCE,
        reason_text=(
            f"waiting on {user.display_name}, invited "
            f"{invitation.invited_at.isoformat()} and not yet accepted. The item "
            "is theirs and nobody has picked it up."
        ),
        user_id=user.id,
        obligation_ids=obligation_ids,
        invitation_id=invitation.id,
        invited_at=invitation.invited_at,
    )


def _owner_of_obligations(
    session: Session,
    company_id: str,
    obligations: list[Obligation],
    moment: datetime,
) -> Resolution:
    """The walk from a set of duties to the one person who holds them all.

    Split out of resolve_change_owner when the confirmed/proposed distinction
    arrived, and split rather than copied for the usual reason: the caller now
    runs this walk over two different sets of obligations depending on what is
    behind them, and two spellings of "which of these owners is THE owner" would
    drift the moment one of them gained a status the other did not know about.
    Every refusal below is the one it always was, in the words it always used.
    """
    ids = tuple(o.id for o in obligations)
    owners = {o.owner_user_id for o in obligations if o.owner_user_id}
    if not owners:
        names = ", ".join(ids)
        return _refused(
            ROUTE_OBLIGATION_UNOWNED,
            f"the obligation this change touches has no owner ({names}). An "
            "obligation nobody owns is how a filing gets missed; give it an "
            "owner rather than giving this to somebody at random.",
            obligation_ids=ids,
        )
    if len(owners) > 1:
        candidates = tuple(sorted(owners))
        return _refused(
            ROUTE_OWNERS_DISAGREE,
            f"this change touches {len(ids)} obligations with "
            f"{len(candidates)} different owners ({', '.join(candidates)}). "
            "Nothing here can say which of them is accountable, so a person "
            "has to choose.",
            obligation_ids=ids,
            candidate_user_ids=candidates,
        )

    owner_id = owners.pop()
    user = _active_user(session, company_id, owner_id)
    if user is None:
        return _refused(
            ROUTE_OWNER_UNKNOWN,
            f"the obligation names owner {owner_id}, and this company has no "
            "such account. The person may have left, or the id may belong to "
            "another tenant; either way nobody here can act on it.",
            obligation_ids=ids,
        )
    if user.status == STATUS_INVITED:
        # Not a refusal any more, and this is the whole of the change. An
        # invited account is a person the product has reached and who has not
        # arrived; the item is theirs and the resolution says it has not landed.
        return _invited_owner(session, company_id, user, moment, obligation_ids=ids)
    if user.status != STATUS_ACTIVE:
        return _refused(
            ROUTE_OWNER_INACTIVE,
            f"{user.display_name} owns this and their account is "
            f"{user.status}. A suspended account cannot sign in, so assigning "
            "to it would look handled and would not be.",
            obligation_ids=ids,
            candidate_user_ids=(user.id,),
        )

    return Resolution(
        reason_code=ROUTE_OK,
        reason_text=f"{user.display_name} owns {', '.join(ids)}",
        user_id=user.id,
        obligation_ids=ids,
    )


def resolve_change_owner(
    session: Session, company_id: str, *, change_id: str, now: datetime | None = None
) -> Resolution:
    """The one person accountable for a change, or the reason there is not one.

    A MAPPING THE PIPELINE PROPOSED DOES NOT ROUTE, AND THAT IS THE WHOLE OF
    THIS FUNCTION'S RECENT HISTORY. Until ROUTE_MAPPING_UNCONFIRMED existed this
    function did not read ChangeObligation.mapped_by_kind at all, so a mapping
    app/state/mapping.py proposed from a lexical word overlap walked to a named
    human exactly as a mapping a person had confirmed did. On that path a
    candidate had become a finding in the most consequential place the product
    has: somebody was told work was theirs because two words appeared in both
    documents.

    Not theoretical. A Kentucky vegetation-management budget table shares
    "project" and "budget" with MEP's cost-allocation duty OBL-005, and the
    change screen printed "Sarah Lindqvist owns OBL-005" over it. That screen
    was given a caveat; the escalation queue and the approval route were not,
    because they do not go through it. The caveat now lives here, where every
    caller passes, and the screen imports the sentence rather than keeping a
    second copy of it -- see UNCONFIRMED_MAPPING above.

    THE TWO SETS, AND WHY THE CONFIRMED ONE WINS OUTRIGHT. When any mapping
    behind this change is a person's, only the person's mappings are considered
    and the proposer's are set aside entirely. The alternative -- resolving over
    all of them -- lets a guess overrule a judgement by arithmetic: a candidate
    naming a second owner turns a change that routed cleanly into
    ROUTE_OWNERS_DISAGREE, so a guess about any change could stop that change
    routing. scripts/seed_demo_gaps.py had already noticed the hazard and works
    around it by refusing to propose beside a confirmed mapping; that workaround
    is no longer load-bearing, though it is still harmless.

    WHERE THE REFUSAL SITS IN THE ORDER, WHICH IS A DELIBERATE CHOICE AND NOT AN
    ACCIDENT OF WRITING. It fires only where a NAME WOULD OTHERWISE HAVE BEEN
    HANDED OUT -- the two ROUTE_OK_CODES outcomes. Every other refusal is left
    exactly as it was, because this code exists to stop an assignment and not to
    pile a second complaint onto a refusal that already stopped one. A proposed
    mapping onto an unowned duty still answers ROUTE_OBLIGATION_UNOWNED: "give
    this duty an owner" is true and actionable whoever wrote the mapping, and
    app/state/invites.py branches on that exact code to decide an invitation is
    the fix. Reordering these would silently turn an owner-gap invitation behind
    a proposed mapping into INV_NOT_A_GAP -- which in the seeded workspace is
    most of them, since most mappings there are the proposer's. A gap behind a
    confirmed mapping would still work, so the breakage would be partial, which
    is the kind that gets found late.

    ROUTE_PENDING_ACCEPTANCE IS CONVERTED TOO, and that is the sharp end rather
    than an edge case. That code means the item has left the shared queue and is
    on a named desk, which is why it is in ROUTE_OK_CODES at all. A person
    invited into this product, and handed an escalation, on the strength of a
    word overlap nobody confirmed is the worst version of this bug and not an
    exception to it.

    THE COST, STATED EXACTLY RATHER THAN ROUNDED. An owner-gap invitation still
    completes on a proposed mapping: the gap is real, the invitation is written
    and the account is created, because an unowned duty answers
    ROUTE_OBLIGATION_UNOWNED whoever wrote the mapping. What no longer happens is
    the last step -- the escalation does not land on the new person's desk, and
    stays in the shared queue under ROUTE_MAPPING_UNCONFIRMED until somebody
    confirms the mapping. An earlier draft of this docstring said the flow "can
    no longer complete", which is a rounder and worse sentence: it describes a
    product that refuses an invitation it in fact grants.
    tests/test_invites.py::test_an_invitation_cannot_land_work_that_rests_on_a_proposed_mapping
    pins both halves.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")

    obligations = obligations_for_change(session, company_id, change_id)
    if not obligations:
        return _refused(
            ROUTE_NO_OBLIGATION,
            f"no company obligation is mapped to change {change_id}, so there is "
            "nobody this is the duty of. Map the change to an obligation, or "
            "leave it here for a person to read.",
        )

    # AUTHOR_ANALYST is the only kind that counts as a person's judgement, and
    # it is tested for by equality rather than "not AUTHOR_SYSTEM". A third kind
    # added to TURN_AUTHOR_KINDS later must arrive as a decision about whether
    # it can route, not inherit the answer by being unequal to one string.
    kinds = {
        row.obligation_id: row.mapped_by_kind
        for row in mappings_for_change(session, company_id, change_id)
    }
    confirmed = [o for o in obligations if kinds.get(o.id) == AUTHOR_ANALYST]

    resolution = _owner_of_obligations(
        session, company_id, confirmed or obligations, moment
    )
    if confirmed or not resolution.routed:
        return resolution

    # A name was about to be handed out on nobody's judgement. The person is
    # carried in candidate_user_ids and NOT in user_id, which keeps Resolution's
    # invariant -- user_id is set if and only if the code is one of
    # ROUTE_OK_CODES -- so no caller reading user_id can be given a name the
    # product is refusing to stand behind. An analyst still gets the name,
    # because a refusal that will not say who it was about is a refusal nobody
    # can clear.
    holder = _active_user(session, company_id, resolution.user_id)
    who = holder.display_name if holder is not None else resolution.user_id
    return _refused(
        ROUTE_MAPPING_UNCONFIRMED,
        f"{who} owns {', '.join(resolution.obligation_ids)} and this does not "
        f"route to them. {UNCONFIRMED_MAPPING} Confirm it and this routes to "
        f"{who}.",
        obligation_ids=resolution.obligation_ids,
        candidate_user_ids=(resolution.user_id,),
    )


def resolve_escalation_owner(
    session: Session,
    company_id: str,
    *,
    escalation_id: str,
    now: datetime | None = None,
) -> Resolution:
    """Walk escalation to claim to change to obligation to owner.

    Every hop that breaks gets its own code. A missing claim and a missing
    obligation are different faults with different fixes, and reporting both as
    "could not route" would leave an analyst reading rows by hand to tell which.
    """
    _require_scope(company_id)

    escalation = (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one_or_none()
    )
    if escalation is None:
        return _refused(
            ROUTE_NO_ESCALATION,
            f"no escalation {escalation_id!r} for this company",
        )

    claim = (
        session.query(Claim)
        .filter(Claim.company_id == company_id)
        .filter(Claim.id == escalation.claim_id)
        .one_or_none()
    )
    if claim is None:
        return _refused(
            ROUTE_NO_CLAIM,
            f"escalation {escalation_id} names claim {escalation.claim_id}, "
            "which this company has no row for.",
        )

    change = (
        session.query(Change)
        .filter(Change.company_id == company_id)
        .filter(Change.id == claim.change_id)
        .one_or_none()
    )
    if change is None:
        return _refused(
            ROUTE_NO_CHANGE,
            f"claim {claim.id} names change {claim.change_id}, which this "
            "company has no row for.",
        )

    return resolve_change_owner(session, company_id, change_id=change.id, now=now)


def _holders_of_role(session: Session, company_id: str, role_name: str) -> list[User]:
    """Every active account holding a live grant of one role, by id.

    Revoked grants do not count and suspended accounts do not count, which is
    the same filter permissions_for_user applies -- so a person this returns is
    a person who could actually sign in and act.
    """
    return (
        session.query(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(User.company_id == company_id)
        .filter(User.status == STATUS_ACTIVE)
        .filter(UserRole.company_id == company_id)
        .filter(UserRole.revoked_at.is_(None))
        .filter(Role.name == role_name)
        .filter((Role.company_id.is_(None)) | (Role.company_id == company_id))
        .order_by(User.id)
        .distinct()
        .all()
    )


def _role_exists(session: Session, company_id: str, role_name: str) -> bool:
    return (
        session.query(Role)
        .filter(Role.name == role_name)
        .filter((Role.company_id.is_(None)) | (Role.company_id == company_id))
        .first()
        is not None
    )


def resolve_assignee(
    session: Session,
    company_id: str,
    *,
    rule: str,
    escalation_id: str | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Turn one workflow step's assignee_rule into one account, or refuse.

    The four shapes are the wire contract's: obligation_owner, unassigned,
    role:<name>, user:<id>. Anything else is refused rather than guessed at --
    a rule the product cannot read is a rule nobody wrote down correctly, and
    routing it to somebody would hide the typo.

    unassigned is a refusal and not an error. The canvas has a word for
    nobody-yet and this is it; the step stays visible with nobody on it.
    """
    _require_scope(company_id)
    rule = (rule or "").strip()

    if rule == ASSIGNEE_UNASSIGNED:
        return _refused(
            ROUTE_RULE_UNASSIGNED,
            "this step says nobody is assigned. It stays open and visible "
            "until somebody says who is asked.",
        )

    if rule == ASSIGNEE_OBLIGATION_OWNER:
        if not escalation_id:
            return _refused(
                ROUTE_NO_ESCALATION,
                "this step routes to the obligation owner, and there is no "
                "escalation to resolve one against.",
            )
        return resolve_escalation_owner(
            session, company_id, escalation_id=escalation_id, now=now
        )

    if rule.startswith(ASSIGNEE_ROLE_PREFIX):
        name = rule[len(ASSIGNEE_ROLE_PREFIX) :].strip()
        if not name:
            return _refused(
                ROUTE_RULE_UNKNOWN,
                f"{rule!r} names no role. Write role:<role_name>.",
            )
        if not _role_exists(session, company_id, name):
            return _refused(
                ROUTE_ROLE_UNKNOWN,
                f"this company has no role called {name!r}.",
            )
        holders = _holders_of_role(session, company_id, name)
        if not holders:
            return _refused(
                ROUTE_ROLE_EMPTY,
                f"nobody active holds the role {name!r}, so this step has "
                "nobody to go to. Grant the role rather than sending the work "
                "somewhere else.",
            )
        if len(holders) > 1:
            names = ", ".join(user.display_name for user in holders)
            return _refused(
                ROUTE_ROLE_AMBIGUOUS,
                f"{len(holders)} people hold the role {name!r} ({names}) and a "
                "step is held by one account. Nothing here can pick between "
                "them, so a person has to. A step a role can share is not "
                "built; see the note at the head of app/state/routing.py.",
                candidate_user_ids=tuple(user.id for user in holders),
            )
        holder = holders[0]
        return Resolution(
            reason_code=ROUTE_OK,
            reason_text=f"{holder.display_name} is the only active {name}",
            user_id=holder.id,
        )

    if rule.startswith(ASSIGNEE_USER_PREFIX):
        user_id = rule[len(ASSIGNEE_USER_PREFIX) :].strip()
        if not user_id:
            return _refused(
                ROUTE_RULE_UNKNOWN,
                f"{rule!r} names no account. Write user:<user_id>.",
            )
        user = _active_user(session, company_id, user_id)
        if user is None:
            return _refused(
                ROUTE_USER_UNKNOWN,
                f"this company has no account {user_id!r}.",
            )
        if user.status != STATUS_ACTIVE:
            return _refused(
                ROUTE_USER_INACTIVE,
                f"{user.display_name}'s account is {user.status} and cannot "
                "sign in to answer this.",
                candidate_user_ids=(user.id,),
            )
        return Resolution(
            reason_code=ROUTE_OK,
            reason_text=f"named account: {user.display_name}",
            user_id=user.id,
        )

    return _refused(
        ROUTE_RULE_UNKNOWN,
        f"{rule!r} is not an assignee rule. It must be "
        f"{ASSIGNEE_OBLIGATION_OWNER}, {ASSIGNEE_UNASSIGNED}, "
        f"{ASSIGNEE_ROLE_PREFIX}<role_name> or {ASSIGNEE_USER_PREFIX}<user_id>.",
    )


# ---------------------------------------------------------------------------
# Writing the assignment
# ---------------------------------------------------------------------------


def route_escalation(
    session: Session,
    company_id: str,
    *,
    escalation_id: str,
    actor: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Put an escalation on the owner's desk, or record why it stays in the queue.

    Both outcomes are audited. A refusal is a decision the product made about a
    piece of work, and a decision nobody can see afterwards is a decision nobody
    can dispute -- which is the state this whole product exists to end.

    An escalation somebody already holds is left alone and nothing is written.
    The seed runs on every start, and re-routing would take work off a desk it
    had been moved onto deliberately.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")

    escalation = (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one_or_none()
    )
    if escalation is None:
        return _refused(
            ROUTE_NO_ESCALATION, f"no escalation {escalation_id!r} for this company"
        )

    if escalation.assigned_to_user_id is not None:
        # Nothing is written on this path -- the work stays where it is -- but
        # what is REPORTED has to be true. A holder who has not accepted their
        # invitation, or whose account was suspended after it landed, is not
        # somebody holding this in any sense a queue should call settled, and
        # answering ROUTE_OK for them is how an item stops being visible while
        # nobody is working it.
        holder = _active_user(session, company_id, escalation.assigned_to_user_id)
        if holder is None:
            return _refused(
                ROUTE_OWNER_UNKNOWN,
                f"this is held by {escalation.assigned_to_user_id}, and this "
                "company has no such account. Nobody here can act on it.",
            )
        if holder.status == STATUS_INVITED:
            return _invited_owner(session, company_id, holder, stamp)
        if holder.status != STATUS_ACTIVE:
            return _refused(
                ROUTE_OWNER_INACTIVE,
                f"{holder.display_name} holds this and their account is "
                f"{holder.status}, so it is on a desk nobody sits at.",
                candidate_user_ids=(holder.id,),
            )
        return Resolution(
            reason_code=ROUTE_OK,
            reason_text=f"already held by {holder.display_name}",
            user_id=escalation.assigned_to_user_id,
        )

    resolution = resolve_escalation_owner(
        session, company_id, escalation_id=escalation_id, now=stamp
    )

    kind = ACTOR_USER if actor_user_id else ACTOR_SYSTEM
    if not resolution.routed:
        record_event(
            session,
            company_id=company_id,
            actor=who,
            action=ACTION_ESCALATION_UNROUTED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason=f"{resolution.reason_code}: {resolution.reason_text}",
            occurred_at=stamp,
            actor_user_id=actor_user_id,
            actor_kind=kind,
        )
        session.flush()
        return resolution

    escalation.assigned_to_user_id = resolution.user_id
    escalation.assigned_at = stamp
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=(
            ACTION_ESCALATION_ROUTED_PENDING
            if resolution.pending_acceptance
            else ACTION_ESCALATION_ROUTED
        ),
        subject_type="escalation",
        subject_id=escalation_id,
        reason=f"{resolution.reason_code}: {resolution.reason_text}",
        occurred_at=stamp,
        actor_user_id=actor_user_id,
        actor_kind=kind,
    )
    session.flush()
    return resolution


def unroute_escalation(
    session: Session,
    company_id: str,
    *,
    escalation_id: str,
    actor: str,
    reason: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Take an item off a desk and put it back in the shared queue, audited.

    The inverse of route_escalation and the reason it exists: when the person
    an item was handed to stops being reachable -- their invitation revoked,
    say -- the item must become visible again. Leaving it assigned to an
    account that can never sign in is the "looks handled and is not" failure
    this module was written to refuse, arrived at from the other direction.

    An item nobody holds is left alone and nothing is written. The reason it is
    in the queue is re-derived and returned, so the caller can report it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")

    escalation = (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one_or_none()
    )
    if escalation is None:
        return _refused(
            ROUTE_NO_ESCALATION, f"no escalation {escalation_id!r} for this company"
        )
    if escalation.assigned_to_user_id is None:
        return resolve_escalation_owner(
            session, company_id, escalation_id=escalation_id, now=stamp
        )

    # The pair is written together or not at all: an assignment time with no
    # assignee describes an event nobody can name.
    escalation.assigned_to_user_id = None
    escalation.assigned_at = None
    session.flush()

    resolution = resolve_escalation_owner(
        session, company_id, escalation_id=escalation_id, now=stamp
    )
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_ESCALATION_UNROUTED,
        subject_type="escalation",
        subject_id=escalation_id,
        reason=f"{reason}; now {resolution.reason_code}: {resolution.reason_text}",
        occurred_at=stamp,
        actor_user_id=actor_user_id,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    session.flush()
    return resolution


# ---------------------------------------------------------------------------
# The two queues a person opens
# ---------------------------------------------------------------------------


def _open_escalations(session: Session, company_id: str):
    return (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.resolved_at.is_(None))
    )


def shared_queue(session: Session, company_id: str) -> list[QueueItem]:
    """Everything nobody holds, each with the reason it is still there, now.

    The reason is re-derived rather than read back, so reinstating an account
    this afternoon changes what the queue says without anything re-running.
    """
    _require_scope(company_id)
    rows = (
        _open_escalations(session, company_id)
        .filter(Escalation.assigned_to_user_id.is_(None))
        .order_by(Escalation.id)
        .all()
    )
    return [
        QueueItem(
            escalation=row,
            resolution=resolve_escalation_owner(
                session, company_id, escalation_id=row.id
            ),
        )
        for row in rows
    ]


def awaiting_acceptance(
    session: Session, company_id: str, *, now: datetime | None = None
) -> list[QueueItem]:
    """Items on a desk whose owner has not accepted their invitation yet.

    The queue that exists because closing the owner handoff opened a new way for
    work to stall silently. shared_queue shows what nobody holds; this shows what
    somebody holds and nobody can act on. Between them there is no state where an
    item is stuck and invisible, which was the point of the whole exercise.

    An account whose invitation was revoked or has run out is NOT here. It is a
    dead end rather than a wait, resolve_escalation_owner refuses on it, and the
    fix is to un-route the item rather than to keep watching it.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")
    rows = (
        _open_escalations(session, company_id)
        .filter(Escalation.assigned_to_user_id.isnot(None))
        .order_by(Escalation.assigned_at, Escalation.id)
        .all()
    )
    items = []
    for row in rows:
        resolution = resolve_escalation_owner(
            session, company_id, escalation_id=row.id, now=moment
        )
        if resolution.pending_acceptance:
            items.append(QueueItem(escalation=row, resolution=resolution))
    return items


def escalations_for_user(
    session: Session, company_id: str, user_id: str
) -> list[Escalation]:
    """What is on one person's desk. The answer to "which of these are mine"."""
    _require_scope(company_id)
    if not user_id:
        return []
    return (
        _open_escalations(session, company_id)
        .filter(Escalation.assigned_to_user_id == user_id)
        .order_by(Escalation.assigned_at, Escalation.id)
        .all()
    )


__all__ = [
    "ACTION_ESCALATION_ROUTED",
    "ACTION_ESCALATION_ROUTED_PENDING",
    "ACTION_ESCALATION_UNROUTED",
    "ACTION_OBLIGATION_MAPPED",
    "ACTION_OBLIGATION_OWNER_SET",
    "INVITE_LIVE_STATUSES",
    "QueueItem",
    "ROUTE_MAPPING_UNCONFIRMED",
    "ROUTE_NO_CHANGE",
    "ROUTE_NO_CLAIM",
    "ROUTE_NO_ESCALATION",
    "ROUTE_NO_OBLIGATION",
    "ROUTE_OBLIGATION_UNOWNED",
    "ROUTE_OK",
    "ROUTE_OK_CODES",
    "ROUTE_PENDING_ACCEPTANCE",
    "ROUTE_OWNERS_DISAGREE",
    "ROUTE_OWNER_INACTIVE",
    "ROUTE_OWNER_UNKNOWN",
    "ROUTE_ROLE_AMBIGUOUS",
    "ROUTE_ROLE_EMPTY",
    "ROUTE_ROLE_UNKNOWN",
    "ROUTE_RULE_UNASSIGNED",
    "ROUTE_RULE_UNKNOWN",
    "ROUTE_USER_INACTIVE",
    "ROUTE_USER_UNKNOWN",
    "ROUTING_REASON_CODES",
    "UNCONFIRMED_MAPPING",
    "Resolution",
    "awaiting_acceptance",
    "changes_for_obligation",
    "ensure_obligation",
    "escalations_for_user",
    "invitation_for_invited_user",
    "invitation_is_live",
    "map_change_to_obligation",
    "mappings_for_change",
    "obligation_for_company",
    "obligations_for_change",
    "obligations_for_company",
    "obligations_for_owner",
    "resolve_assignee",
    "resolve_change_owner",
    "resolve_escalation_owner",
    "route_escalation",
    "set_obligation_owner",
    "shared_queue",
    "unowned_obligations",
    "unroute_escalation",
]
