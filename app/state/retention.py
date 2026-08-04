"""How long every table keeps its rows, and what erasure means against a log
that cannot forget.

A retention schedule is normally a table in a document, which is to say a set of
claims nobody can check. This one is data and code: SCHEDULE names every table in
app/state/models.py with a window and a reason, tests/test_retention.py refuses a
table that has no rule, and each rule that claims a mechanism names a function
that is resolved by import rather than believed.

WHAT HAPPENS WHEN RETENTION MEETS AN APPEND-ONLY LOG
----------------------------------------------------
The audit chain is append-only twice over: app/state/audit.py refuses an UPDATE
or a DELETE from application code, and a SHA-256 chain over every field of every
row, gapless per company, detects a row removed by other means. Retention cannot
reach it, and pretending otherwise would be the one thing this product exists to
stop.

So erasure here has a particular shape, and it has to be said in full rather
than implied. A subject row is purged WHILE THE AUDIT ROW NAMING IT SURVIVES,
pointing at an id that no longer resolves.

    What a reader of the log can still learn about a purged record:
      that a record with that id existed; what type of thing it was; when each
      decision about it was taken; who took it, as a display string and, on the
      paths that record one, as an account id and a session; the address that
      request came from; what the decision was and the reason given for it.

    What they cannot learn:
      what the record said. The words of a chat turn, the text of a complaint,
      the address a share link was opened from, the user-agent a session ran
      under. Those live only in the purged row, and after the purge they are
      gone from this database.

THAT IS NOT COMPLETE ERASURE AND MUST NOT BE DESCRIBED AS ANY. record_event
stores subject_id, actor, reason and citation verbatim, so wherever one of those
IS the personal datum, no purge can reach it. Two live examples, both true of
this build:

  * A failed sign-in records `person:<address>` as the actor and, when no
    account matched, the address again as subject_id (app/auth/sessions.py).
    Purging every other table leaves that address in the chain for ever.
  * An invitation's audit row quotes the invited address in its reason
    (app/state/invites.py). Purging the invitation leaves that line standing.

tests/test_retention.py pins both, because a limit nobody wrote down is a limit
a buyer finds instead. deploy/site/privacy.html says the same in plain words.

WHAT THIS MODULE WILL NOT DO ABOUT THAT. It will not write a second, redacted
copy of the log, and it will not add a column marking a row as tombstoned. Both
would be an edit to a record that must not change, and the second log is the one
nobody reads -- audit.py makes that argument at length and it is not weaker here.

WHERE THE SCHEDULE STOPS. It reaches rows in this database and nothing else.
Whatever web server sits in front of the application keeps its own request log,
and nothing here reads or removes it. A copy of the database taken before a
purge still holds every row the purge removed, so a retention window is only as
short as the oldest snapshot anybody kept -- scripts/backup.py writes copies to
a local directory and nothing here reaches them.

THE RULES
---------
Every rule states a window AND a reason. A window with no reason is a number
somebody will change without thinking, and the test refuses a reason short
enough to be a label.

Rows carrying an address or an email get the shortest windows that are kept at
all. Rows holding the customer's own filings get no clock, because there is no
event in this build to start one -- no account closure exists, so the honest
entry is "while the account is open", with nothing counting.

RUNNING THEM
------------
Every purge is a DRY RUN unless the caller passes dry_run=False, and a flag that
is not a bool is refused rather than coerced: dry_run=0 is falsey and would
otherwise arm the destructive path by accident.

Every destructive run writes an audit row BEFORE it touches a row, naming the
rule, the table, the count and the cutoff -- and naming nothing else, because a
log that quotes what it deleted has moved the data rather than removed it. A
run that matches nothing writes no row at all: a purge that purged nothing is
not a deletion, and a log of non-events is a log nobody reads.

The audit row and the deletions sit in ONE transaction, so they commit together
or not at all. A caller that commits between the two has recorded a purge that
did not happen, and nothing here can stop them.

Running twice is safe. The second pass matches nothing and logs nothing.

THE ORDER IN purge_all IS CHOSEN SO THAT THE DRY RUN PREDICTS THE REAL RUN. No
rule may change what a later rule matches, or the report a person approved would
describe a different deletion from the one that ran. Two places that could have
gone wrong:

  * The share-open redaction runs BEFORE the share-open purge. Redaction writes
    to `ip`; the purge selects on `opened_at`; so marking a row does not remove
    it from the purge's selection and both counts hold in either mode.
  * The chat purge runs BEFORE the feedback purge. A transcript held back by a
    live complaint stays held for this whole pass and goes on the NEXT run,
    rather than being released halfway through by a rule that has just deleted
    the complaint holding it.

WHAT WOULD CALL THIS IN PRODUCTION, AND WHAT CALLS IT TODAY
-----------------------------------------------------------
Today: nothing. There is no scheduler in this product. No cron entry, no
background worker, no queue, no startup hook. app/main.py does not import this
module and neither does anything else, and tests/test_retention.py asserts that
by reading app/ -- so the day somebody wires it up, the test fails and forces
the privacy page to change in the same commit.

In production it would be a daily job, run once per company, in two steps that a
person can tell apart: a dry run whose report goes to whoever is accountable,
then the destructive run. The natural home is the same scheduler that would drive
ScheduledRun (app/state/models.py), which is also not built. Whatever runs it
needs a database session, a company id and nothing else -- these functions take
no request, no user and no configuration, so they can run from a cron entry, a
management command or a worker without any of them growing an opinion.

A LIMIT OF THIS IMPLEMENTATION. Each purge loads the company's rows for its
table and compares timestamps in Python, the way every other module in this
codebase compares them (app/auth/sessions.py, app/state/invites.py). That is
right at this size and wrong at a million rows. Pushing the comparison into SQL
means relying on UtcDateTime storing ISO-8601 text that sorts chronologically;
it does today, and nothing pins it, so the cheap correct thing is done instead
and the reason is written here rather than discovered later.
"""

import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.state.audit import ACTOR_SYSTEM, record_event
from app.state.models import (
    ChatMessage,
    ChatSession,
    Feedback,
    Invitation,
    LoginSession,
    ShareLink,
    ShareOpen,
)
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# The vocabulary
#
# THESE TWO BELONG IN app/state/audit.py BESIDE THE REST OF THE ACTION CODES AND
# MUST BE MOVED THERE RATHER THAN RESTATED, for the reason app/state/invites.py
# gives for its own block: that file is owned by another agent in this build and
# a parallel edit would lose their work. When they move, this block is deleted
# and the import changes; nothing else does. It is in the handoff list.
#
# THEY ARE TWO CODES AND NOT ONE, on purpose. retention.purged means rows are
# gone. retention.redacted means a row is still there with one field taken out
# of it. Reading the second as the first would say a record was destroyed that
# is still readable; reading the first as the second would say a record survives
# that does not. Both readings are the log asserting something nobody did, which
# is the failure the ACTION_ constants exist to prevent.
# ---------------------------------------------------------------------------

ACTION_RETENTION_PURGED = "retention.purged"
ACTION_RETENTION_REDACTED = "retention.redacted"

# The actor on a retention row. A machine acted, so actor_kind stays
# ACTOR_SYSTEM and actor_user_id stays absent -- record_event refuses a user id
# on a system actor, and naming a person for a job nobody pressed a button for
# would be a false attribution in the one place false attribution is worst.
RETENTION_ACTOR = "system:retention"

# What goes in ShareOpen.ip once the address has been taken out.
#
# NOT AN EMPTY STRING, AND THE DIFFERENCE MATTERS. models.py says "" on that
# column means the server recorded no address -- a gap in the caller, not a fact
# about the visitor. Blanking a purged row would file a deletion as a gap and
# nobody could tell the two apart afterwards. A marker says which happened, and
# no address contains a bracket, so it can never be mistaken for one. This is
# section 26 of docs/best-practices.html applied to a column: a fallback has to
# announce itself.
IP_PURGED = "(purged)"


# ---------------------------------------------------------------------------
# A rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One table, one window, one reason, and the thing that carries it out.

    Frozen, so a caller cannot move a window at a call site and leave the
    privacy page describing a schedule the code no longer runs.

    mechanism is a dotted path, and the empty string is the honest value. ""
    means the window is written down and NOTHING PERFORMS IT. That distinction
    is the whole difference between a control and a promise, so it is a field
    rather than a footnote, and a test resolves every non-empty one by import.

    days is the number the code counts; None means no clock runs on this table
    in this build. window says the same thing in the words a person would use,
    because "while the account is open" is not a number and forcing it to be one
    would mean inventing a closure event that does not exist.
    """

    id: str
    table: str
    window: str
    days: int | None
    # The column the window is measured from. "" where no clock runs.
    clock: str
    reason: str
    mechanism: str = ""

    @property
    def enforced(self) -> bool:
        return bool(self.mechanism)

    def resolve_mechanism(self):
        """Import whatever this rule says carries it out. Raises if it is not there."""
        if not self.mechanism:
            return None
        module_path, _, name = self.mechanism.rpartition(".")
        return getattr(importlib.import_module(module_path), name)


def _customer_work(table: str, reason: str) -> Rule:
    """A rule for the customer's own work: no clock, and a reason saying why not.

    These rows are the filings a company loaded and everything derived from
    them. They are the reason anybody would buy this, and deleting them on a
    timer would delete the product. The window is the life of the account -- and
    there is no account-closure event anywhere in this build, so there is
    nothing for a clock to start from and no code here pretends there is.
    """
    return Rule(
        id=f"{table}.account",
        table=table,
        window="while the account is open",
        days=None,
        clock="",
        reason=reason,
        mechanism="",
    )


_PURGE = "app.state.retention."


SCHEDULE: tuple[Rule, ...] = (
    # -----------------------------------------------------------------------
    # Never
    # -----------------------------------------------------------------------
    Rule(
        id="audit_events.never",
        table="audit_events",
        window="never",
        days=None,
        clock="",
        reason=(
            "Never purged. Every row's hash covers the hash before it, so "
            "removing one does not delete a record, it destroys the evidence "
            "value of every record after it -- and a gap in the per-company "
            "sequence is itself read as a deletion."
        ),
        mechanism="app.state.audit._refuse_to_rewrite_history",
    ),
    # -----------------------------------------------------------------------
    # Personal data, with the shortest windows and code behind them
    # -----------------------------------------------------------------------
    Rule(
        id="login_sessions.expired",
        table="login_sessions",
        window="90 days",
        days=90,
        clock="expires_at",
        reason=(
            "An expired session row answers one question -- was this sign-in "
            "used, from which address, on which browser -- and after a quarter "
            "no investigation is still asking it. It carries an IP address and "
            "a user-agent, so it gets the shortest window of anything kept at "
            "all. Measured on expires_at rather than revoked_at, because the "
            "expiry is the last moment the token could have worked."
        ),
        mechanism=f"{_PURGE}purge_login_sessions",
    ),
    Rule(
        id="share_opens.address",
        table="share_opens",
        window="90 days",
        days=90,
        clock="opened_at",
        reason=(
            "The address is taken out and the row stays. Two different things "
            "sit in this row: the address, which is personal data about "
            "somebody who never signed in, and the record of what a recipient "
            "was shown, which is evidence. Ninety days is long enough to "
            "investigate a link that went to the wrong person and short enough "
            "that an address is not kept for a year to prove a page rendered."
        ),
        mechanism=f"{_PURGE}redact_share_open_addresses",
    ),
    Rule(
        id="share_opens.stored",
        table="share_opens",
        window="365 days",
        days=365,
        clock="opened_at",
        reason=(
            "The row itself goes at a year. It records what a named recipient "
            "was shown and whether the citation held at that moment, which "
            "models.py commits to answering 'a year later' -- so a year is the "
            "ceiling rather than the starting point. The link's own open_count "
            "is left alone: decrementing it would erase the fact that the link "
            "was opened, which is the part worth keeping."
        ),
        mechanism=f"{_PURGE}purge_share_opens",
    ),
    Rule(
        id="chat_sessions.transcript",
        table="chat_sessions",
        window="365 days",
        days=365,
        clock="last_turn_at, or started_at where nobody ever spoke",
        reason=(
            "What a person typed is their data, and a year of it is more than "
            "anyone needs to answer for an answer. A conversation opened and "
            "never used is measured from when it opened, because a row naming a "
            "person and a time is still a row about them."
        ),
        mechanism=f"{_PURGE}purge_chat_transcripts",
    ),
    Rule(
        id="chat_messages.transcript",
        table="chat_messages",
        window="365 days",
        days=365,
        clock="the owning session's last turn",
        reason=(
            "A turn belongs to its conversation and goes with it, on the same "
            "clock. Turns are never purged out from under a session that stays, "
            "because a transcript missing its middle reads as a conversation "
            "that did not happen."
        ),
        mechanism=f"{_PURGE}purge_chat_transcripts",
    ),
    Rule(
        id="feedback.stored",
        table="feedback",
        window="365 days",
        days=365,
        clock="created_at",
        reason=(
            "A complaint is what a person wrote, plus a frozen copy of the "
            "conversation they were complaining about, so it holds their words "
            "twice over. A year is long enough to act on it and to show a "
            "pattern; nothing is served by keeping it longer. A row still open "
            "at a year goes too: the backlog item it was triaged into survives "
            "and carries the substance."
        ),
        mechanism=f"{_PURGE}purge_feedback",
    ),
    Rule(
        id="invitations.spent",
        table="invitations",
        window="90 days",
        days=90,
        clock="expires_at",
        reason=(
            "An invitation holds the email address of somebody who may never "
            "have become a user, which makes it the row here least likely to "
            "belong to a customer at all. Once it can no longer be accepted it "
            "is a spent token record. Ninety days covers the argument about "
            "whether somebody was invited; the audit row saying they were "
            "outlives it -- and that row quotes the address, which no purge can "
            "reach. One clock for both kinds: a provisioning link lives 24 "
            "hours and a handoff seven days, but what is being kept afterwards "
            "is the same address, so the window runs from the moment each "
            "stopped working rather than from a TTL."
        ),
        mechanism=f"{_PURGE}purge_invitations",
    ),
    # -----------------------------------------------------------------------
    # The customer's own work. No clock, and the reason is the same shape each
    # time: this is what they bought, and there is no closure event to count
    # from. Stated per table rather than once, so a reader checking one table
    # finds an answer beside it rather than a cross-reference.
    # -----------------------------------------------------------------------
    _customer_work(
        "document_versions",
        "The filings the company loaded, kept byte for byte. Verification "
        "re-reads these bytes every time it checks a citation, so discarding "
        "them turns every claim in the product into an unverifiable assertion.",
    ),
    _customer_work(
        "passages",
        "The cuts taken from a filing, with their offsets. They belong to the "
        "version that owns them and live exactly as long as it does.",
    ),
    _customer_work(
        "proceedings",
        "One docket at one commission. It is the spine every change, claim and "
        "project hangs from, and it is the customer's record of what they "
        "tracked.",
    ),
    _customer_work(
        "changes",
        "What moved between two versions of a filing. This is the product's "
        "output and the customer's evidence of when a rule changed.",
    ),
    _customer_work(
        "claims",
        "The statements the product would make and the citations that earn "
        "them. Deleting one deletes the reason an action was taken.",
    ),
    _customer_work(
        "escalations",
        "The claims the product refused to assert, and why. A refusal is the "
        "most useful thing in the record when somebody asks what was known.",
    ),
    _customer_work(
        "projects",
        "The line of work an analyst lives in, running over months. It is the "
        "unit the customer organises everything else around.",
    ),
    _customer_work(
        "project_changes",
        "Somebody judged that a change applied to a project. Removing that "
        "erases the fact that anyone once thought so, which is the question "
        "asked afterwards.",
    ),
    _customer_work(
        "research_threads",
        "An open question against a project and the record of chasing it. It "
        "is the customer's own enquiry, not the product's.",
    ),
    _customer_work(
        "research_turns",
        "The notes and findings inside a thread, with the claims they cite. "
        "They live as long as the thread that holds them.",
    ),
    _customer_work(
        "work_plans",
        "What a project intends to do. It is the customer's plan and nothing "
        "here has a view on when they are finished with it.",
    ),
    _customer_work(
        "work_plan_steps",
        "The ordered steps of a plan, each owned by a named person. They live "
        "as long as the plan they belong to.",
    ),
    _customer_work(
        "scheduled_runs",
        "A standing instruction to re-check a project's sources. It is live "
        "configuration the customer set, not a record that ages.",
    ),
    _customer_work(
        "knowledge_items",
        "What the company learned, kept as versions rather than values. This "
        "is the store that only compounds if the history survives, so a clock "
        "on it would destroy the thing it is for.",
    ),
    _customer_work(
        "sources",
        "Where evidence came from. A citation whose source row is gone cannot "
        "be traced back, which is the whole point of storing it.",
    ),
    _customer_work(
        "findings",
        "One thing the review centre would assert, and the claim behind it. It "
        "is the customer's output and often the input to a filing.",
    ),
    _customer_work(
        "questions",
        "What the work still needs answered, and which of those stop it. The "
        "answered ones stay beside the question, which is why they are worth "
        "keeping at all.",
    ),
    _customer_work(
        "collective_takes",
        "What a project believed at a moment, and what it excluded. A "
        "superseded take is the record of having been wrong, which is exactly "
        "what an auditor asks for.",
    ),
    _customer_work(
        "deliverables",
        "What left the building: a memo, a filing, a briefing. The customer "
        "answers for these to a regulator, so this product does not put a "
        "clock on them.",
    ),
    _customer_work(
        "steer_directives",
        "A standing instruction that changed what the research looked for. "
        "Reconciling a set of findings with the instruction behind them needs "
        "the instruction to still exist.",
    ),
    _customer_work(
        "obligations",
        "The company's own duty in the company's own words. It is loaded from "
        "the customer's context file and is theirs, not a derived row.",
    ),
    _customer_work(
        "change_obligations",
        "The judgement that a docket change bears on a company duty. It is the "
        "load-bearing reasoning in this product and the row that says whose "
        "reasoning it was.",
    ),
    _customer_work(
        "approval_workflows",
        "An approval route, frozen once activated so a finished run still "
        "reads the graph it ran under. Purging an old version would leave live "
        "runs naming a route nobody can read.",
    ),
    _customer_work(
        "workflow_steps",
        "The nodes of a route. They are meaningless apart from the route that "
        "owns them and live exactly as long as it does.",
    ),
    _customer_work(
        "workflow_edges",
        "The arrows of a route, on the same footing as its nodes: part of a "
        "frozen graph a past run still points at.",
    ),
    _customer_work(
        "workflow_runs",
        "One escalation walked through one route. It is the record of who was "
        "asked and in what order, which is the segregation of duties argument "
        "made checkable.",
    ),
    _customer_work(
        "workflow_step_runs",
        "What happened at each step, including the steps nobody answered. A "
        "bypassed step is the row that stops an unapproved action reading as an "
        "approved one, so it outlives any window.",
    ),
    _customer_work(
        "improvement_items",
        "The backlog feedback is triaged into. It is about the product rather "
        "than about a person, and it outlives the complaint it came from on "
        "purpose, so that purging a complaint does not delete the work it "
        "caused. A triager can quote a person's words into the detail field, "
        "and nothing here reaches those; that is a real gap and it is written "
        "down rather than designed around.",
    ),
    _customer_work(
        "share_links",
        "The link itself, and who created and revoked it. It holds no address "
        "and no name, only a token hash, and it is the row every open hangs "
        "from -- so it outlives the opens that are purged from under it.",
    ),
    _customer_work(
        "users",
        "The account, holding an email address and a password hash. It is "
        "personal data and it gets no clock, because there is no closure event "
        "in this build to start one and a live account is not stale data. "
        "Suspending an account keeps the row on purpose, so that 'who could do "
        "what, when' stays answerable.",
    ),
    _customer_work(
        "user_roles",
        "One grant of one role, kept after revocation. An investigation asks "
        "what a person could do at the moment they did something, and a table "
        "that ages out grants can only answer what they can do now.",
    ),
    _customer_work(
        "roles",
        "The named bundles of permissions. Configuration rather than data "
        "about anybody, so nothing here ages.",
    ),
    _customer_work(
        "permissions",
        "The closed set of permission codes. Vocabulary, not tenant data: "
        "there is no person in it to have a retention right over.",
    ),
    _customer_work(
        "role_permissions",
        "Which codes each role carries. The absence of a row here is the "
        "segregation of duties control, so this table describes the product "
        "rather than any customer.",
    ),
)


_BY_ID = {rule.id: rule for rule in SCHEDULE}


def rule_for(rule_id: str) -> Rule:
    """The rule with this id, or KeyError. Never a default.

    A missing rule means the caller is running something the schedule does not
    describe, and answering with a plausible window would put a number in the
    audit log that no document justifies.
    """
    try:
        return _BY_ID[rule_id]
    except KeyError:
        raise KeyError(
            f"no retention rule called {rule_id!r}. Rules are declared in "
            "SCHEDULE in this module and nowhere else."
        ) from None


def rules_for_table(table: str) -> tuple[Rule, ...]:
    """Every rule touching one table. More than one where a row has two lives."""
    return tuple(rule for rule in SCHEDULE if rule.table == table)


# ---------------------------------------------------------------------------
# What a run reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """A row the window caught and the run would not touch, and why."""

    row_id: str
    reason: str


@dataclass(frozen=True)
class PurgeReport:
    """What a run did, or on a dry run what it would do.

    matched and removed are separate numbers and always both reported. On a dry
    run removed is 0 and matched is the answer; reporting one number would make
    the two modes indistinguishable in a log.

    held names rows the window caught and the rule refused, each with a reason
    in plain words. A rule that silently skipped a row would report a smaller
    deletion than the one somebody is owed an explanation for.

    detail carries anything the counts cannot say -- how many turns went with a
    conversation, which counter was left alone -- and is empty where they say
    everything.
    """

    rule: Rule
    company_id: str
    cutoff: datetime
    matched: int
    removed: int
    dry_run: bool
    held: tuple[Held, ...] = ()
    detail: str = ""

    def line(self) -> str:
        """One line for a person reading the output of a dry run."""
        mode = "would remove" if self.dry_run else "removed"
        text = (
            f"{self.rule.id}: {self.matched} rows in {self.rule.table} older "
            f"than {self.cutoff.isoformat()} ({self.rule.window}); {mode} "
            f"{self.matched if self.dry_run else self.removed}"
        )
        if self.held:
            text += f"; held back {len(self.held)}"
        if self.detail:
            text += f". {self.detail}"
        return text


# ---------------------------------------------------------------------------
# The machinery every rule shares
# ---------------------------------------------------------------------------


def _moment(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError(
            "now must carry a timezone; a naive timestamp cannot be compared "
            "with the stored ones and would silently pick a different cutoff"
        )
    return now


def _require_flag(dry_run) -> bool:
    """Refuse anything but a real bool.

    dry_run=0 is falsey and would arm the destructive path; dry_run="no" is
    truthy and would disarm it. Both are the kind of mistake that is only found
    afterwards, by which point rows are gone.
    """
    if not isinstance(dry_run, bool):
        raise ValueError(
            f"dry_run must be True or False; got {dry_run!r}. A value that is "
            "merely truthy or falsey decides whether rows are destroyed, and "
            "that decision is not one to infer."
        )
    return dry_run


def _apply(
    session: Session,
    *,
    rule: Rule,
    company_id: str,
    cutoff: datetime,
    moment: datetime,
    doomed: list,
    apply_to,
    action: str,
    verb: str,
    dry_run: bool,
    held: tuple[Held, ...] = (),
    detail: str = "",
) -> PurgeReport:
    """Write the audit row, then act. In that order, and only when there is work.

    Nothing is written on a dry run, and nothing is written when the rule
    matched no rows. The reason line names the rule, the table, the count and
    the cutoff, AND NOTHING OUT OF THE ROWS THEMSELVES: the log is append-only,
    so an address copied into it to record its deletion has been moved rather
    than removed.
    """
    matched = len(doomed)
    if dry_run or matched == 0:
        return PurgeReport(
            rule=rule,
            company_id=company_id,
            cutoff=cutoff,
            matched=matched,
            removed=0,
            dry_run=dry_run,
            held=held,
            detail=detail,
        )

    record_event(
        session,
        company_id=company_id,
        actor=RETENTION_ACTOR,
        action=action,
        subject_type="retention_rule",
        subject_id=rule.id,
        reason=(
            f"{matched} rows in {rule.table} {verb} under rule {rule.id} "
            f"({rule.window} on {rule.clock}); cutoff {cutoff.isoformat()}; "
            f"{len(held)} held back"
        ),
        occurred_at=moment,
        actor_kind=ACTOR_SYSTEM,
    )

    for row in doomed:
        apply_to(row)
    session.flush()

    return PurgeReport(
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        matched=matched,
        removed=matched,
        dry_run=False,
        held=held,
        detail=detail,
    )


def _cutoff(rule: Rule, moment: datetime) -> datetime:
    if rule.days is None:
        raise ValueError(f"{rule.id} runs on no clock and cannot be given a cutoff")
    return moment - timedelta(days=rule.days)


# ---------------------------------------------------------------------------
# The rules with code behind them
# ---------------------------------------------------------------------------


def purge_login_sessions(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """Expired sessions, 90 days after the expiry. Address and user-agent go too.

    Measured on expires_at, which is written at creation and never extended, so
    the clock starts at the last moment the token could have worked. A session
    revoked early still has an expiry and is measured from it, because revoking
    does not shorten the window an investigation would want.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("login_sessions.expired")
    cutoff = _cutoff(rule, moment)

    rows = [
        row
        for row in session.execute(
            select(LoginSession).where(LoginSession.company_id == company_id)
        )
        .scalars()
        .all()
        if row.expires_at < cutoff
    ]
    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=rows,
        apply_to=session.delete,
        action=ACTION_RETENTION_PURGED,
        verb="purged",
        dry_run=dry_run,
    )


def _share_opens_for_company(session: Session, company_id: str) -> list[ShareOpen]:
    """Every open in one company. share_opens carries no company_id of its own.

    Tenancy lives on the link, so the join IS the scope -- a query over this
    table without it is an unscoped read of who opened what, which is the shape
    models.py warns about on the table itself.
    """
    return (
        session.execute(
            select(ShareOpen)
            .join(ShareLink, ShareOpen.share_id == ShareLink.id)
            .where(ShareLink.company_id == company_id)
        )
        .scalars()
        .all()
    )


def redact_share_open_addresses(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """Take the address out of an old open. Keep the row, which is evidence.

    A row already carrying the marker is not matched, so running twice finds
    nothing. A row that never carried an address is not matched either: writing
    the marker onto it would turn "the server recorded none" into "there was one
    and it was taken out", which is a stronger claim than the row supports.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("share_opens.address")
    cutoff = _cutoff(rule, moment)

    rows = [
        row
        for row in _share_opens_for_company(session, company_id)
        if row.opened_at < cutoff and row.ip not in ("", IP_PURGED)
    ]

    def _redact(row: ShareOpen) -> None:
        row.ip = IP_PURGED

    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=rows,
        apply_to=_redact,
        action=ACTION_RETENTION_REDACTED,
        verb="redacted",
        dry_run=dry_run,
        detail=(
            f"the address is replaced with {IP_PURGED!r} rather than blanked, "
            "because a blank on this column means the server recorded none"
        ),
    )


def purge_share_opens(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """The open record itself, at a year.

    ShareLink.open_count and last_opened_at are deliberately left where they
    are. They are a convenience over these rows, and after a purge they say
    something true that the rows no longer show: the link was opened, this many
    times, most recently then. Decrementing the counter to match would erase
    that. The registry has to read a count higher than the stored rows as "older
    opens are no longer retained" rather than as a defect in the writer -- which
    is the reverse of the rule models.py states, and is stated here because it
    is a real consequence of purging under a counter.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("share_opens.stored")
    cutoff = _cutoff(rule, moment)

    rows = [
        row
        for row in _share_opens_for_company(session, company_id)
        if row.opened_at < cutoff
    ]
    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=rows,
        apply_to=session.delete,
        action=ACTION_RETENTION_PURGED,
        verb="purged",
        dry_run=dry_run,
        detail=(
            "ShareLink.open_count and last_opened_at are left as they are; a "
            "count higher than the stored opens means older opens have gone"
        ),
    )


def purge_chat_transcripts(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """A conversation and every turn in it, a year after the last thing said.

    A conversation nobody ever spoke in is measured from when it opened, because
    a row naming a person and a time is still a row about them.

    HELD BACK: a conversation holding a turn that somebody complained about. The
    complaint stores chat_message_id, and purging the turn would leave it
    pointing at nothing -- a reviewer would open a year-old complaint and find
    the thing complained about missing with no explanation. The complaint goes
    on its own clock, and the transcript follows on a later run. The report
    names the row doing the holding, so nobody has to guess why a transcript
    outlived its window.

    The report counts conversations, not turns. Turns are in the detail line.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("chat_sessions.transcript")
    cutoff = _cutoff(rule, moment)

    conversations = (
        session.execute(select(ChatSession).where(ChatSession.company_id == company_id))
        .scalars()
        .all()
    )
    messages = (
        session.execute(select(ChatMessage).where(ChatMessage.company_id == company_id))
        .scalars()
        .all()
    )
    by_conversation: dict[str, list[ChatMessage]] = {}
    for message in messages:
        by_conversation.setdefault(message.session_id, []).append(message)

    complained_about = {
        row.chat_message_id: row.id
        for row in session.execute(
            select(Feedback).where(Feedback.company_id == company_id)
        )
        .scalars()
        .all()
        if row.chat_message_id
    }

    doomed: list[ChatSession] = []
    held: list[Held] = []
    for conversation in conversations:
        clock = conversation.last_turn_at or conversation.started_at
        if clock >= cutoff:
            continue
        blocking = [
            (message.id, complained_about[message.id])
            for message in by_conversation.get(conversation.id, [])
            if message.id in complained_about
        ]
        if blocking:
            message_id, feedback_id = blocking[0]
            held.append(
                Held(
                    row_id=conversation.id,
                    reason=(
                        f"turn {message_id} is the subject of feedback "
                        f"{feedback_id}, which is still stored; purging the "
                        "turn would leave the complaint pointing at nothing"
                    ),
                )
            )
            continue
        doomed.append(conversation)

    turns = sum(len(by_conversation.get(row.id, [])) for row in doomed)

    def _drop(conversation: ChatSession) -> None:
        for message in by_conversation.get(conversation.id, []):
            session.delete(message)
        session.delete(conversation)

    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=doomed,
        apply_to=_drop,
        action=ACTION_RETENTION_PURGED,
        verb="purged",
        dry_run=dry_run,
        held=tuple(held),
        detail=f"{turns} turns in {len(doomed)} conversations",
    )


def purge_feedback(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """A complaint and its frozen copy of the conversation, at a year.

    Measured on created_at and not on resolution, and that is a decision worth
    stating: a row still open at a year goes anyway. Keeping untriaged rows for
    ever would mean the queue nobody worked became the archive nobody agreed to.
    What survives is the backlog item it was triaged into, which carries the
    substance without the person's own words.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("feedback.stored")
    cutoff = _cutoff(rule, moment)

    rows = [
        row
        for row in session.execute(
            select(Feedback).where(Feedback.company_id == company_id)
        )
        .scalars()
        .all()
        if row.created_at < cutoff
    ]
    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=rows,
        apply_to=session.delete,
        action=ACTION_RETENTION_PURGED,
        verb="purged",
        dry_run=dry_run,
    )


def purge_invitations(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> PurgeReport:
    """A spent invitation, 90 days after it stopped being acceptable.

    One clock for every status. An accepted invitation's expiry is in the past
    because it was accepted before it, a revoked one's likewise, and one nobody
    answered expired on its own -- so expires_at is the moment none of them
    could be used again, whichever way they ended.

    What goes with it: the address of somebody who may never have had an
    account. What stays: the audit row saying they were invited, which quotes
    that address in its reason and cannot be edited. That is the tension this
    module exists to state rather than resolve away.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    rule = rule_for("invitations.spent")
    cutoff = _cutoff(rule, moment)

    rows = [
        row
        for row in session.execute(
            select(Invitation).where(Invitation.company_id == company_id)
        )
        .scalars()
        .all()
        if row.expires_at < cutoff
    ]
    return _apply(
        session,
        rule=rule,
        company_id=company_id,
        cutoff=cutoff,
        moment=moment,
        doomed=rows,
        apply_to=session.delete,
        action=ACTION_RETENTION_PURGED,
        verb="purged",
        dry_run=dry_run,
    )


# The order purge_all runs them in. It is chosen so that no rule changes what a
# later rule matches, which is what makes the dry run a prediction rather than
# an estimate -- see the module docstring for the two cases that could have gone
# wrong. tests/test_retention.py compares the two modes report for report.
PURGE_ORDER = (
    redact_share_open_addresses,
    purge_share_opens,
    purge_login_sessions,
    purge_chat_transcripts,
    purge_feedback,
    purge_invitations,
)


def purge_all(
    session: Session,
    *,
    company_id: str,
    now: datetime | None = None,
    dry_run: bool = True,
) -> tuple[PurgeReport, ...]:
    """Run every enforced rule for one company, in order. Dry by default.

    This is the whole of what a scheduler would call, and nothing calls it. It
    takes a session, a company and a clock, so it can be driven from a cron
    entry, a management command or a worker without any of them growing an
    opinion about retention.

    One transaction, because the caller owns it. Every audit row and every
    deletion in this pass commits together or not at all.
    """
    _require_scope(company_id)
    dry_run = _require_flag(dry_run)
    moment = _moment(now)
    return tuple(
        run(session, company_id=company_id, now=moment, dry_run=dry_run)
        for run in PURGE_ORDER
    )


__all__ = [
    "ACTION_RETENTION_PURGED",
    "ACTION_RETENTION_REDACTED",
    "IP_PURGED",
    "PURGE_ORDER",
    "RETENTION_ACTOR",
    "SCHEDULE",
    "Held",
    "PurgeReport",
    "Rule",
    "purge_all",
    "purge_chat_transcripts",
    "purge_feedback",
    "purge_invitations",
    "purge_login_sessions",
    "purge_share_opens",
    "redact_share_open_addresses",
    "rule_for",
    "rules_for_table",
]
