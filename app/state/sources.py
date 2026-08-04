"""The source registry: where documents come from, and who may change that.

WHAT THIS MODULE IS FOR, IN ONE SENTENCE. A regulated buyer asks two questions
of any product that reads their filings -- where does your data come from, and
who can change that -- and this is the write layer behind the screen that
answers both. It is a control surface and an evidence surface. It is not a
settings page.

THE HARD PART IS TELLING THE TRUTH, AND IT IS THE WHOLE DESIGN.

NOTHING IN THIS PRODUCT FETCHES ANYTHING. There is no crawler, no poller and no
scheduler. ScheduledRun is a table with cadences and no fetcher behind it. So
every function here is written to make it impossible for a screen to imply
otherwise:

* FETCHABLE_SOURCE_KINDS is asked, never assumed. It is empty today, and
  initial_status_for_kind() reads it, so a newly registered source is
  not_implemented -- registered, and this build cannot fetch from it.
* status and enabled are two facts and neither implies the other. enabled is
  what the administrator wants. status is what is true. Disabling a source does
  not touch its status, and set_source_enabled() below is written so that it
  cannot.
* last_scanned_at stays NULL until something scans, which is never. The screen
  prints NEVER_SCANNED rather than an empty cell, because a blank reads as a
  quiet week rather than as a capability that does not exist.
* FETCHES_NOTHING is a sentence, not a comment, and the screen renders it.

THE SECRET IS REFERENCED BY NAME AND NEVER HELD. credential_ref carries the NAME
of an environment variable. app/state/models.py argues that at length; what this
module adds is the part no column can express and that models.py explicitly
handed on: the allow-list. Whoever writes one of these rows can point the
product at any secret the process can read, so credential_ref_problem() refuses
a name outside CREDENTIAL_REF_PREFIX, refuses anything that is not shaped like
an environment variable at all, and NEVER QUOTES WHAT IT REFUSED. An error
message that echoes its input is how a pasted key reaches the page and the logs.

THERE IS NO CONNECTION TEST, AND THAT IS A DECISION. A button that makes this
server fetch a URL an administrator typed is a server-side request forgery
surface: it turns an admin screen into a way to ask the host to open connections
inside its own network -- the cloud metadata address, the loopback interface,
anything a name happens to resolve to. Doing it safely needs the resolved
address checked against every private range, redirects refused, the socket
pinned to the address that was checked, and a timeout. None of that is built.
Refusing is the better answer, so CONNECTION_TEST_REFUSAL is on the screen.

What is offered instead is endpoint_preflight(), which reads the text of an
endpoint and says whether a fetcher would be allowed to reach it. It opens no
socket and resolves no name. It proves nothing about whether the endpoint
exists, and its own reason string says so, because a preflight that reads like a
connection test is the same lie in smaller type.

THE ROWS ARE REAL. data/real holds 102 public filings from eight commissions,
each with a provenance file naming the URL it was fetched from. corpus_sources()
reads those files and register_corpus_sources() registers the eight, so the
registry describes where this workspace's corpus actually came from rather than
demonstrating an empty table. It is idempotent: pressing the button twice is not
two registrations.

WHERE THE READS BELONG. The scoped reads here belong in app/state/queries.py
beside versions_for_company, and they are here because that file is not owned by
this change. The tenant guard is IMPORTED rather than copied, so there is still
one definition of what an unscoped read is -- the same choice
app/web/views/shares_admin.py and app/state/sharing.py made, for the same
reason.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import NamedTuple
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import policy
from app.state.audit import ACTOR_USER, record_event
from app.state.identity import user_for_company
from app.state.models import (
    CONFIG_FORBIDDEN_SUBSTRINGS,
    FETCHABLE_SOURCE_KINDS,
    SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
    SOURCE_REGISTRATION_KIND_MCP_SERVER,
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    SOURCE_REGISTRATION_KIND_REST_API,
    SOURCE_REGISTRATION_KINDS,
    SOURCE_REGISTRY_PERMISSION,
    SOURCE_STATUS_CONNECTED,
    SOURCE_STATUS_NEVER_TRIED,
    SOURCE_STATUS_NOT_IMPLEMENTED,
    SOURCE_STATUS_UNREACHABLE,
    DocumentVersion,
    SourceRegistration,
    initial_status_for_kind,
)

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE FIVE BELONG IN app/state/audit.py BESIDE EVERY OTHER ACTION_ CONSTANT,
# AND THEY ARE HERE ONLY BECAUSE THIS CHANGE DOES NOT OWN THAT FILE. The same
# concession app/state/sharing.py made for its three, with the same warning:
# whoever picks this up must MOVE these lines rather than restate them, and this
# module must then import them. Two spellings of one action code produce a row
# that verifies perfectly and that no query for that action ever returns -- in
# the log and unfindable, which is worse than missing.
#
# The namespace is source. rather than source_registration., which is a
# deliberate risk taken with eyes open: `sources` is also the name of the
# evidence-provenance table six hundred lines up in models.py. Nothing writes an
# audit row about that table today, and if something ever does it must not reuse
# these five codes. The subject_type below is the unambiguous half of the
# answer, and it is what a query should filter on.
# ---------------------------------------------------------------------------

ACTION_SOURCE_REGISTERED = "source.registered"
ACTION_SOURCE_UPDATED = "source.updated"
ACTION_SOURCE_ENABLED = "source.enabled"
ACTION_SOURCE_DISABLED = "source.disabled"
ACTION_SOURCE_REMOVED = "source.removed"

# What these rows are about. Unambiguous on purpose: a `sources` table exists
# and answers a different question.
SUBJECT_SOURCE = "source_registration"


# ---------------------------------------------------------------------------
# The sentences the screen renders
#
# They live here rather than in the template because the tests assert them, the
# view passes them, and one spelling is the whole point: a claim about what this
# product can do must not exist in two places that can come to disagree.
#
# NO APOSTROPHES AND NO QUOTATION MARKS in any constant a test looks for in a
# rendered page. Jinja escapes them, and the escaped form is not the string.
# ---------------------------------------------------------------------------

#: The sentence the whole screen exists to make unavoidable.
FETCHES_NOTHING = "Nothing in this build fetches on a schedule."

#: Printed where last_scanned_at is NULL, which is every row. A blank cell reads
#: as a quiet week; this reads as a capability that does not exist.
NEVER_SCANNED = "Never scanned"

#: Why there is no connection test. On the page, not in a docstring.
CONNECTION_TEST_REFUSAL = (
    "There is no connection test on this screen, and that is a decision rather "
    "than an omission. A test button takes a URL an administrator typed and has "
    "this server fetch it, which turns an admin screen into a way to ask the "
    "host to open connections inside its own network: the cloud metadata "
    "address at 169.254.169.254, the loopback interface, anything a name "
    "happens to resolve to. Doing it safely needs the resolved address checked "
    "against every private range, redirects refused, the socket pinned to the "
    "address that was checked, and a timeout. None of that is built here, so "
    "nothing on this screen opens a socket."
)

#: The half of the answer a connection test would still not give.
HANDSHAKE_IS_NOT_A_FETCH = (
    "Reaching an endpoint would not be the same as being able to fetch a filing "
    "from it. A server that answers has said it is there, not that this product "
    "can find, download, read and attribute a document behind it."
)

#: Shown beside a credential_ref. The value is never shown, in either state.
CREDENTIAL_RESOLVES = "the named variable is set in this process"
CREDENTIAL_MISSING = "the named variable is NOT set in this process"

#: The counts on a row, and why they are three numbers and not one.
CORPUS_COUNT_CAVEAT = (
    "Three counts, because they answer three different questions and merging "
    "them would hide which one is missing. On disk is what data/real holds for "
    "that commission, counted from the provenance file beside each filing. "
    "Fetched from that address is how many document versions in this workspace "
    "carry a URL at that host: it is derived from the URL each version already "
    "records, so it says they came from the same place and not that this "
    "registration brought them. Points at this registration is the stored link, "
    "which is the only one of the three that is a claim about this row."
)

#: Rendered beside the derived count, so the derivation is never silent.
MATCHED_BY_ADDRESS = (
    "fetched from that address, matched on the URL each version already carries"
)

#: Rendered beside the stored link, for the same reason.
POINTS_AT_REGISTRATION = "point at this registration"

#: The framing for the provenance figures. True whatever the numbers say.
PROVENANCE_GAP = (
    "A document version can name the URL it was fetched from and the "
    "registration that brought it. The counts below are read from this "
    "database: where one is zero, nothing was recorded, and this screen will "
    "not fill the gap with a guess."
)

#: Why a refused form comes back empty.
FORM_NOT_REFILLED = (
    "The form is not filled back in. Whatever was refused may be the thing that "
    "must not be on this screen."
)

KIND_WORDS = {
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET: "Public commission docket",
    SOURCE_REGISTRATION_KIND_INTERNAL_STORE: "Internal document store",
    SOURCE_REGISTRATION_KIND_REST_API: "REST API behind a key",
    SOURCE_REGISTRATION_KIND_MCP_SERVER: "MCP server",
}

#: One line per status, in words a buyer can quote. The four are the four states
#: the brief asks this screen to tell apart at a glance.
STATUS_WORDS = {
    SOURCE_STATUS_NOT_IMPLEMENTED: (
        "Registered, and this build cannot fetch from that kind"
    ),
    SOURCE_STATUS_NEVER_TRIED: "Registered and reachable in principle, never tried",
    SOURCE_STATUS_CONNECTED: "Registered and reached",
    SOURCE_STATUS_UNREACHABLE: "Registered and unreachable",
}

#: The badge class each status wears, from the app stylesheet.
STATUS_BADGE = {
    SOURCE_STATUS_NOT_IMPLEMENTED: "badge--draft",
    SOURCE_STATUS_NEVER_TRIED: "badge--material",
    SOURCE_STATUS_CONNECTED: "badge--active",
    SOURCE_STATUS_UNREACHABLE: "badge--closed",
}

#: The two kinds where a credential is meaningful. A public docket is published
#: to everybody, and an internal store is reached by path -- pointing either at
#: a secret name would be a capability nobody needs.
KINDS_TAKING_A_CREDENTIAL = (
    SOURCE_REGISTRATION_KIND_REST_API,
    SOURCE_REGISTRATION_KIND_MCP_SERVER,
)

#: The allow-list models.py handed to the write layer. A credential reference
#: must start with this, so an administrator cannot point the product at the
#: session-signing key, the database URL, or anything else the process can read.
CREDENTIAL_REF_PREFIX = "STRATA_SOURCE_"

#: An environment variable name and nothing else.
_CREDENTIAL_REF_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SourceRefused(ValueError):
    """A write this module would not perform, with the sentence a person reads.

    ValueError so a caller that catches the ordinary refusal shape still catches
    it. `.reason` is written for an administrator, not for a log, and it NEVER
    contains what was submitted -- see credential_ref_problem().
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SourceInUse(SourceRefused):
    """Documents point at this registration, so it cannot be removed."""

    def __init__(self, reason: str, documents: int):
        super().__init__(reason)
        self.documents = documents


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """A random id, not a counter.

    models.py illustrates these as SRC-0001. A counter needs a read of the
    table tail and a write, and two rows registered in the same second would
    race for one number -- so this follows app/state/sharing.py. The id appears
    in URLs and is not a secret.
    """
    return f"SRC-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# What may be written, checked before the database has to refuse it
#
# The check constraint on config is the backstop, and a person must never meet
# it: an IntegrityError is a stack trace where a sentence was wanted. Every
# function below returns "" when there is nothing wrong, so the call sites read
# as `problem = f(...)` and one shape covers all of them.
# ---------------------------------------------------------------------------


def config_problem(config: dict) -> str:
    """Refuse a config that carries anything shaped like a secret.

    THE SAME TUPLE THE CHECK CONSTRAINT USES, so the two cannot come to
    disagree, and case-insensitively because SQLite LIKE is. It is blunt on
    purpose and will occasionally refuse an innocent URL containing the word
    token. A loud refusal on a rare legitimate config beats a quiet leak of a
    live credential onto a screen and into whatever logs that screen.

    The offending word is named. The value is not: config is rendered, and an
    error page that quoted the whole blob would put the thing being refused
    back on the screen it was refused from.
    """
    try:
        text = json.dumps(config, sort_keys=True).lower()
    except (TypeError, ValueError):
        return (
            "That configuration cannot be stored as JSON. A source is described "
            "by plain text fields, and anything else is a bug in the form."
        )
    for word in CONFIG_FORBIDDEN_SUBSTRINGS:
        if word in text:
            return (
                f"That configuration contains {word}, and configuration is "
                "rendered on this screen. A key pasted into it would travel to "
                "every browser that opens the registry and into whatever logs "
                "the response. Put the key in an environment variable and name "
                f"the variable in the credential field. This check is blunt: if "
                f"{word} is an innocent part of a URL, that URL cannot be stored "
                "here today, and a loud refusal is the intended failure."
            )
    return ""


def credential_ref_problem(kind: str, credential_ref: str | None) -> str:
    """Refuse anything that is not the NAME of a permitted environment variable.

    THIS IS THE ALLOW-LIST app/state/models.py SAID BELONGED HERE. Its own note
    concedes that whoever writes one of these rows can point the product at any
    secret the process can read, that this makes credential_ref a capability
    rather than a label, and that no column can express the restriction. This
    function is the restriction.

    IT NEVER QUOTES WHAT IT REFUSED. The most likely wrong value is the key
    itself, pasted into the wrong box, and an error message that echoes its
    input renders that key on the page and writes it wherever the page is
    logged. So the messages describe the rule instead.
    """
    value = (credential_ref or "").strip()
    if not value:
        return ""
    if kind not in KINDS_TAKING_A_CREDENTIAL:
        return (
            f"A {KIND_WORDS.get(kind, kind).lower()} does not take a credential. "
            "A public docket is published to everybody and an internal store is "
            "reached by path, so naming a secret on one of them would grant a "
            "capability nothing here would use. Only these kinds take one: "
            + ", ".join(KIND_WORDS[k] for k in KINDS_TAKING_A_CREDENTIAL)
            + "."
        )
    if not _CREDENTIAL_REF_SHAPE.match(value):
        return (
            "That is not the name of an environment variable, and this field "
            "takes a name and never a key. The product does not store keys: it "
            "stores the name of the variable the key lives in, and resolves the "
            "name when it needs it, so a database read, a backup or a "
            "screenshot of this screen hands over nothing. Names look like "
            f"{CREDENTIAL_REF_PREFIX}ACME_KEY. The value submitted is not "
            "repeated here, because if it was the key itself then printing it "
            "back would be the leak this field exists to prevent."
        )
    if not value.startswith(CREDENTIAL_REF_PREFIX):
        return (
            "A credential reference must start with "
            f"{CREDENTIAL_REF_PREFIX}. Whoever writes a row here can point this "
            "product at any secret the process can read, so the names it may "
            "point at are limited to ones set for this purpose. Naming a "
            "variable outside that prefix would let an administrator have the "
            "product present the session-signing key or the database password "
            "to a third party."
        )
    return ""


def can_fetch(kind: str) -> bool:
    """Whether this build can fetch from that kind of source at all.

    ONE PLACE THE QUESTION IS ASKED, and it is asked rather than assumed. Every
    screen and every sentence that turns on the answer comes through here, so
    the day a fetcher lands the tuple in app/state/models.py gains one entry and
    nothing else in this product has to be found and edited. A view that wrote
    `kind in FETCHABLE_SOURCE_KINDS` for itself would be a second place to
    forget.

    It answers False for all four kinds today. Nothing here fetches anything.
    """
    return kind in FETCHABLE_SOURCE_KINDS


def kind_problem(kind: str) -> str:
    if kind in SOURCE_REGISTRATION_KINDS:
        return ""
    return (
        f"There is no kind of source called {kind!r} in this product. The four "
        "are: " + ", ".join(f"{k} ({KIND_WORDS[k]})" for k in SOURCE_REGISTRATION_KINDS)
        + ". The kind decides what the configuration means and what could ever "
        "fetch from it, so it is not something to guess at."
    )


def endpoints(config: dict) -> tuple[str, ...]:
    """Every place this source names, single or several.

    Two spellings, deliberately. A hand-registered source names one `endpoint`.
    A commission that published the filings in data/real from two hostnames --
    Indiana did -- names them in `origins`, and flattening that into one string
    would be a fact replaced by a tidier untruth.
    """
    found: list[str] = []
    single = str(config.get("endpoint") or "").strip()
    if single:
        found.append(single)
    for host in config.get("origins") or ():
        host = str(host).strip()
        if host and host not in found:
            found.append(host)
    return tuple(found)


def reach_problem(config: dict) -> str:
    if endpoints(config):
        return ""
    return (
        "A source with nowhere to reach is not a registration, it is a note. "
        "Name the base URL, the docket search page or the store path this "
        "source is read from. Nothing in this build will fetch it, and the "
        "point of recording it is that a reader can see where the documents "
        "were said to come from."
    )


# ---------------------------------------------------------------------------
# The preflight: what can honestly be said about an endpoint without touching it
# ---------------------------------------------------------------------------


class Preflight(NamedTuple):
    """Whether a fetcher would be ALLOWED to reach this, and why.

    Never whether it is reachable. Nothing here contacts anything, and the
    reason string says so on both answers, because a preflight that reads like a
    connection test is the same lie in smaller type.
    """

    allowed: bool
    reason: str


#: Names that resolve inside a private network rather than on the public one.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")

_ALLOWED_PREFLIGHT_SCHEMES = ("https",)


def endpoint_preflight(url: str) -> Preflight:
    """Read an endpoint and say whether a fetcher would be permitted to reach it.

    A CHECK ON TEXT. No socket is opened, no name is resolved, and nothing is
    contacted. That is what makes it safe to run while rendering a page, and it
    is also what makes it insufficient on its own: a name that passes here can
    still resolve to 127.0.0.1, which is exactly why there is no fetch button
    behind it. See CONNECTION_TEST_REFUSAL.

    IT DOES NOT GATE THE WRITE, AND MUST NOT. An internal document store is
    inside this network by definition, and an MCP server on the same host is
    the ordinary case rather than the suspicious one, so refusing to REGISTER a
    private address would refuse the truth about where a company keeps its
    documents. The verdict is recorded beside the row instead, so that whoever
    builds the first fetcher inherits the warning rather than the assumption.
    """
    candidate = (url or "").strip()
    if not candidate:
        return Preflight(False, "There is no address here to check.")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return Preflight(False, "That cannot be read as a URL, so nothing can be said about it.")

    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return Preflight(False, "The host part of that URL cannot be read.")

    # EVERY REASON, NOT THE FIRST ONE. A person who is told the scheme is wrong,
    # fixes it, and is then told the address is the loopback interface has been
    # walked through a chain the product could have reported in one go. The
    # address checks come first because they are the serious half.
    problems: list[str] = []

    if not host:
        problems.append("That names no host, so there is no address to check.")
    else:
        try:
            literal = ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            where = (
                "inside a private range"
                if literal.is_private or literal.is_loopback or literal.is_link_local
                else "at a numeric address"
            )
            problems.append(
                f"That is a literal IP address {where}. A fetcher is not "
                "permitted to be pointed at one: it skips every name-based "
                "rule, and the private, loopback and link-local ranges are this "
                "host, its neighbours and the cloud metadata service."
            )
        elif host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
            problems.append(
                "That name resolves inside this network rather than on the "
                "public one, so reaching it would be this server talking to "
                "itself or to its neighbours."
            )
        elif "." not in host:
            problems.append(
                "That is a bare hostname with no domain. It resolves through "
                "whatever search domain this host is configured with, which is "
                "inside the network rather than outside it."
            )

    if parts.username or parts.password:
        problems.append(
            "That URL carries a username or a password in it. A credential in a "
            "URL travels into logs, proxies and screens. Name an environment "
            "variable in the credential field instead."
        )

    if parts.scheme.lower() not in _ALLOWED_PREFLIGHT_SCHEMES:
        problems.append(
            f"Only {', '.join(_ALLOWED_PREFLIGHT_SCHEMES)} would be permitted. "
            "Any other scheme either sends the documents and the key in clear "
            "text or names something that is not a web endpoint at all."
        )

    if problems:
        return Preflight(False, " ".join(problems) + " Nothing was contacted.")

    return Preflight(
        True,
        "Nothing here refuses this address in advance. That is a reading of the "
        "text, not a contact with the endpoint: nothing was contacted, the name "
        "was not resolved, and a name that passes this check can still resolve "
        "to an address inside this network.",
    )


# ---------------------------------------------------------------------------
# Scoped reads. Every one belongs in app/state/queries.py; company_id is
# keyword-only with no default, which is the shape that file uses, so the move
# is a cut and paste.
# ---------------------------------------------------------------------------


def source_registrations_for_company(
    session: Session, *, company_id: str
) -> list[SourceRegistration]:
    """Every source this company has registered, grouped the way it is read.

    Kind first, because the four kinds fail in different ways and an
    administrator reads them apart, then name. Unpaginated, which is honest at
    this scale: a registry is a short list by nature, and an admin who can see
    only the first fifty cannot answer where the data comes from.
    """
    _require_scope(company_id)
    order = {kind: index for index, kind in enumerate(SOURCE_REGISTRATION_KINDS)}
    rows = (
        session.query(SourceRegistration)
        .filter(SourceRegistration.company_id == company_id)
        .all()
    )
    # Sorted here rather than in SQL: the order is the display order of the
    # kinds tuple, which the database has no way to know. A kind the tuple does
    # not list sorts last rather than vanishing.
    return sorted(
        rows,
        key=lambda row: (order.get(row.kind, len(order)), row.name.lower(), row.id),
    )


def source_registration_for_company(
    session: Session, *, company_id: str, source_id: str
) -> SourceRegistration | None:
    """One registration, or None. Another company id resolves to None, not a row."""
    _require_scope(company_id)
    if not source_id:
        return None
    return (
        session.query(SourceRegistration)
        .filter(SourceRegistration.company_id == company_id)
        .filter(SourceRegistration.id == source_id)
        .one_or_none()
    )


@dataclass(frozen=True, slots=True)
class ProvenanceCounts:
    """How much of this workspace can say where it came from.

    Three numbers rather than one, because they are three questions: how many
    versions there are, how many name a URL, and how many point at a
    registration. A version with a URL and no registration is a normal row --
    the 102 real filings were pulled by hand before any registry existed -- so
    the two cannot be folded together.
    """

    versions: int
    with_source_url: int
    by_registration: dict[str, int]
    #: How many versions were fetched from each address, keyed by scheme and
    #: host. DERIVED, and it is a weaker fact than by_registration: it says two
    #: rows share an origin, not that a registered source brought either of
    #: them. It exists because the second fact is unavailable for every filing
    #: pulled by hand before the registry existed, and "we cannot say" is worse
    #: than "these came from the same place" when both are labelled.
    by_origin: dict[str, int]

    @property
    def with_registration(self) -> int:
        return sum(self.by_registration.values())


def provenance_counts(session: Session, *, company_id: str) -> ProvenanceCounts:
    """The provenance figures for one company, read from the rows.

    Raises whatever SQLAlchemy raises if the provenance columns are not on the
    table. That is deliberate: a database missing them is a deployment that has
    not run the migration, and the caller must say so rather than render a
    confident zero.
    """
    _require_scope(company_id)
    rows = (
        session.query(
            DocumentVersion.source_url, DocumentVersion.source_registration_id
        )
        .filter(DocumentVersion.company_id == company_id)
        .all()
    )
    counts: dict[str, int] = {}
    origins: dict[str, int] = {}
    with_url = 0
    for source_url, registration_id in rows:
        if source_url:
            with_url += 1
            where = urlsplit(source_url)
            if where.scheme and where.netloc:
                origin = f"{where.scheme}://{where.netloc}"
                origins[origin] = origins.get(origin, 0) + 1
        if registration_id:
            counts[registration_id] = counts.get(registration_id, 0) + 1
    return ProvenanceCounts(
        versions=len(rows),
        with_source_url=with_url,
        by_registration=counts,
        by_origin=origins,
    )


def documents_pointing_at(session: Session, *, company_id: str, source_id: str) -> int:
    """How many of this company versions name this registration as their source."""
    _require_scope(company_id)
    return (
        session.query(DocumentVersion)
        .filter(DocumentVersion.company_id == company_id)
        .filter(DocumentVersion.source_registration_id == source_id)
        .count()
    )


def credential_is_set(credential_ref: str | None) -> bool | None:
    """Whether the named variable exists in this process. None when none named.

    THE ONLY THING THIS PRODUCT EVER SAYS ABOUT A CREDENTIAL. Not its value, not
    its length, not a prefix of it -- a boolean, and only to an administrator who
    already holds the permission that opens the registry. An administrator who
    names a variable that is not there has to be able to see that; the
    alternative is a source that looks configured and fails at the first fetch
    that this build will never make.
    """
    if not credential_ref:
        return None
    return credential_ref in os.environ


# ---------------------------------------------------------------------------
# The writes. Every one gated through app/auth/policy.py and audited.
# ---------------------------------------------------------------------------


def _actor_email(session: Session, company_id: str, user_id: str) -> str:
    holder = user_for_company(session, company_id, user_id)
    return holder.email if holder is not None else (user_id or "unknown")


def _audit(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    actor: str,
    action: str,
    source_id: str,
    reason: str,
    now: datetime,
) -> None:
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type=SUBJECT_SOURCE,
        subject_id=source_id,
        reason=reason,
        occurred_at=now,
        actor_user_id=user_id or None,
        actor_kind=ACTOR_USER,
    )


def _clean_config(config: dict | None) -> dict:
    """Drop empty values so a blank form field is an absent key, not an empty one.

    An empty string in config would render as a configured endpoint that happens
    to be blank, which is a different and false statement from not configured.
    """
    out = {}
    for key, value in (config or {}).items():
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None, [], {}):
            continue
        out[key] = value
    return out


def register_source(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    name: str,
    kind: str,
    config: dict | None = None,
    credential_ref: str | None = None,
    now: datetime | None = None,
) -> SourceRegistration:
    """Register a place documents could come from. Audited. Fetches nothing.

    THE GATE COMES FIRST, before any validation, so a person who may not
    administer sources learns nothing about whether their configuration was
    acceptable. policy.require() writes the refusal into the chain and raises.

    The status is taken from initial_status_for_kind() rather than written here.
    Today it answers not_implemented for all four kinds because
    FETCHABLE_SOURCE_KINDS is empty. When a fetcher lands, that tuple gains one
    entry and this function starts writing never_tried for that kind without
    being edited.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    label = (name or "").strip()
    if not label:
        raise SourceRefused(
            "A source needs a name. It is what the registry lists and what "
            "somebody reading an audit row a year later has to recognise."
        )
    if len(label) > 256:
        raise SourceRefused("That name is longer than the 256 characters the row holds.")

    problem = kind_problem(kind)
    if problem:
        raise SourceRefused(problem)

    settings = _clean_config(config)
    for problem in (
        config_problem(settings),
        reach_problem(settings),
        credential_ref_problem(kind, credential_ref),
    ):
        if problem:
            raise SourceRefused(problem)

    stamp = now or _utcnow()
    row = SourceRegistration(
        id=_new_id(),
        company_id=company_id,
        name=label,
        kind=kind,
        # Never a literal. See initial_status_for_kind().
        status=initial_status_for_kind(kind),
        config=settings,
        credential_ref=(credential_ref or "").strip() or None,
        created_by_user_id=user_id,
        created_at=stamp,
        last_scanned_at=None,
        last_result=None,
        enabled=True,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as refused:
        # The check constraint caught something config_problem() did not. That
        # is the backstop doing its job, and it must still reach a person as a
        # sentence rather than as a stack trace.
        session.rollback()
        raise SourceRefused(
            "The database refused that row. The one rule it enforces about "
            "configuration is that it must carry nothing shaped like a secret, "
            "and the words it refuses are: " + ", ".join(CONFIG_FORBIDDEN_SUBSTRINGS)
            + ". Nothing was saved."
        ) from refused

    _audit(
        session,
        company_id=company_id,
        user_id=user_id,
        actor=_actor_email(session, company_id, user_id),
        action=ACTION_SOURCE_REGISTERED,
        source_id=row.id,
        reason=(
            f"registered {label} as a {kind}, reachable at "
            f"{', '.join(endpoints(settings)) or 'nowhere recorded'}"
            + (
                f", presenting the credential named {row.credential_ref}"
                if row.credential_ref
                else ""
            )
            + f". Status {row.status}: {STATUS_WORDS.get(row.status, row.status)}."
        ),
        now=stamp,
    )
    return row


def update_source(
    session: Session,
    *,
    company_id: str,
    source_id: str,
    user_id: str,
    name: str,
    config: dict | None = None,
    credential_ref: str | None = None,
    now: datetime | None = None,
) -> SourceRegistration:
    """Change what a registered source says about itself. Audited.

    THE KIND CANNOT BE CHANGED, and refusing that is not laziness. The kind
    decides what the configuration means, which credential rules apply and what
    could ever fetch from it; a row that changed kind under a stable id would
    make every audit row written about it before the change describe something
    else. Remove it and register the new one.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    row = source_registration_for_company(
        session, company_id=company_id, source_id=source_id
    )
    if row is None:
        # Same answer for not here and not yours. Telling them apart would tell
        # this administrator something about another tenant.
        raise LookupError(f"no source {source_id!r} for this company")

    label = (name or "").strip()
    if not label:
        raise SourceRefused("A source needs a name.")

    settings = _clean_config(config)
    for problem in (
        config_problem(settings),
        reach_problem(settings),
        credential_ref_problem(row.kind, credential_ref),
    ):
        if problem:
            raise SourceRefused(problem)

    before = {
        "name": row.name,
        "endpoints": endpoints(row.config or {}),
        "credential": row.credential_ref,
    }
    row.name = label
    row.config = settings
    row.credential_ref = (credential_ref or "").strip() or None
    stamp = now or _utcnow()

    changed = []
    if before["name"] != row.name:
        changed.append(f"name from {before['name']} to {row.name}")
    if before["endpoints"] != endpoints(settings):
        changed.append(
            "where it is read from, "
            f"from {', '.join(before['endpoints']) or 'nowhere recorded'} "
            f"to {', '.join(endpoints(settings)) or 'nowhere recorded'}"
        )
    if before["credential"] != row.credential_ref:
        changed.append(
            "the credential named, "
            f"from {before['credential'] or 'none'} to {row.credential_ref or 'none'}"
        )

    _audit(
        session,
        company_id=company_id,
        user_id=user_id,
        actor=_actor_email(session, company_id, user_id),
        action=ACTION_SOURCE_UPDATED,
        source_id=row.id,
        reason=(
            "changed " + "; ".join(changed)
            if changed
            else "saved with no field different from what was already there"
        ),
        now=stamp,
    )
    session.flush()
    return row


def set_source_enabled(
    session: Session,
    *,
    company_id: str,
    source_id: str,
    user_id: str,
    enabled: bool,
    now: datetime | None = None,
) -> SourceRegistration:
    """Turn a source on or off. Audited. STATUS IS NOT TOUCHED.

    enabled is what the administrator wants. status is what is true. This
    function writes the first and cannot write the second, which is the whole
    reason they are two columns: a disable that also wrote a status would turn
    an intention into a claim about reachability that nobody checked.

    Setting it to what it already is writes nothing. A second click is not a
    second decision, and a second audit row would make one decision look like
    two. app/state/sharing.py refuses a repeat revocation the same way.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    row = source_registration_for_company(
        session, company_id=company_id, source_id=source_id
    )
    if row is None:
        raise LookupError(f"no source {source_id!r} for this company")

    if bool(row.enabled) == bool(enabled):
        return row

    row.enabled = bool(enabled)
    stamp = now or _utcnow()
    _audit(
        session,
        company_id=company_id,
        user_id=user_id,
        actor=_actor_email(session, company_id, user_id),
        action=ACTION_SOURCE_ENABLED if enabled else ACTION_SOURCE_DISABLED,
        source_id=row.id,
        reason=(
            f"{'enabled' if enabled else 'disabled'} {row.name}. That is what "
            "this company wants of the source and says nothing about whether it "
            f"can be reached: its status is unchanged at {row.status}."
        ),
        now=stamp,
    )
    session.flush()
    return row


def remove_source(
    session: Session,
    *,
    company_id: str,
    source_id: str,
    user_id: str,
    now: datetime | None = None,
) -> str:
    """Delete a registration nothing depends on. Audited. Refused otherwise.

    REMOVAL IS REFUSED WHILE DOCUMENTS POINT AT IT, and the alternative was to
    null those pointers out on the way past. That would leave versions that came
    from somewhere with nothing saying where, silently, which is the failure
    this whole screen exists to prevent. Disabling is almost always the act that
    was wanted, so the refusal says so.

    A DATABASE THAT CANNOT ANSWER IS A REFUSAL TOO. If the provenance column is
    not on the table -- an older deployment, a migration not yet run -- the
    count cannot be established, and an unestablished fact degrades to saying so
    rather than to assuming zero and deleting.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    row = source_registration_for_company(
        session, company_id=company_id, source_id=source_id
    )
    if row is None:
        raise LookupError(f"no source {source_id!r} for this company")

    try:
        attached = documents_pointing_at(
            session, company_id=company_id, source_id=source_id
        )
    except SQLAlchemyError as unreadable:
        raise SourceRefused(
            "Whether any document points at this source could not be read from "
            "this database, so it was not removed. Deleting it while a version "
            "names it would leave text that came from somewhere with nothing "
            f"saying where. The read failed with: {unreadable.__class__.__name__}."
        ) from unreadable

    if attached:
        raise SourceInUse(
            f"{attached} document version(s) in this workspace name this source "
            "as where they came from. Removing it would leave them saying they "
            "came from somewhere, with nothing to point at. Disable it instead: "
            "that stops anything using it while the provenance it already "
            "recorded stays true.",
            attached,
        )

    name, kind, stamp = row.name, row.kind, now or _utcnow()
    session.delete(row)
    session.flush()
    _audit(
        session,
        company_id=company_id,
        user_id=user_id,
        actor=_actor_email(session, company_id, user_id),
        action=ACTION_SOURCE_REMOVED,
        source_id=source_id,
        reason=(
            f"removed {name}, a {kind}. No document version in this workspace "
            "named it as its source, so nothing lost its provenance."
        ),
        now=stamp,
    )
    return source_id


# ---------------------------------------------------------------------------
# The real corpus: eight commissions, 102 filings, read from the provenance
# files rather than typed here
#
# scripts/ingest_real.py loads one pair of those filings and drops every
# provenance field on the floor. That is not this module to fix, and it is the
# reason a registered corpus source shows a count on disk and a count in the
# workspace that do not agree -- see CORPUS_COUNT_CAVEAT. Both numbers are read
# from the thing they describe, so neither is a guess.
# ---------------------------------------------------------------------------

REAL_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "real"

#: Written into the config of a registration made from the corpus, so a second
#: press of the button finds the row it already wrote. The value is the slug of
#: the jurisdiction, which is what the provenance files themselves name.
CORPUS_KEY = "corpus_key"


@dataclass(frozen=True, slots=True)
class CorpusSource:
    """One commission, as the provenance files describe it.

    `origins` is scheme and host -- https://psc.ky.gov -- and not a bare
    hostname, because it is read straight out of the URL each filing was
    fetched from and a reader has to be able to go back there.
    """

    key: str
    jurisdiction: str
    origins: tuple[str, ...]
    dockets: tuple[str, ...]
    documents: int
    earliest_filing: str
    latest_filing: str


@dataclass(frozen=True, slots=True)
class CorpusScan:
    """What data/real says, and what in it could not be read.

    `unreadable` is not decoration. A provenance file that will not parse is a
    filing whose origin this product cannot state, and skipping it silently
    would shrink the count on the screen with nothing saying why.
    """

    sources: tuple[CorpusSource, ...]
    unreadable: tuple[str, ...]
    documents: int


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def corpus_scan(data_dir: pathlib.Path | None = None) -> CorpusScan:
    """Read every provenance file in data/real and group it by commission.

    No cache. It is a hundred small files on an admin screen, and a cached
    answer that outlived the corpus would be this module telling a reader about
    a filing set that is no longer there.
    """
    directory = data_dir or REAL_DIR
    if not directory.is_dir():
        return CorpusScan((), (), 0)

    grouped: dict[str, dict] = {}
    unreadable: list[str] = []
    total = 0
    for path in sorted(directory.glob("*.provenance.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        jurisdiction = str(meta.get("jurisdiction") or "").strip()
        if not jurisdiction:
            unreadable.append(path.name)
            continue
        total += 1
        entry = grouped.setdefault(
            jurisdiction,
            {"origins": set(), "dockets": set(), "documents": 0, "dates": set()},
        )
        entry["documents"] += 1
        # The scheme AND the host, taken from the URL the filing was actually
        # fetched from rather than assumed. A bare hostname would be a place
        # nobody can go back to, and the preflight would refuse it for having
        # no scheme -- which would be this screen scolding the corpus for a
        # detail this function threw away.
        fetched_from = urlsplit(str(meta.get("source_url") or ""))
        if fetched_from.scheme and fetched_from.netloc:
            entry["origins"].add(f"{fetched_from.scheme}://{fetched_from.netloc}")
        docket = str(meta.get("docket") or "").strip()
        if docket:
            entry["dockets"].add(docket)
        filed = str(meta.get("filing_date") or "").strip()
        if filed:
            entry["dates"].add(filed)

    sources = tuple(
        sorted(
            (
                CorpusSource(
                    key=_slug(jurisdiction),
                    jurisdiction=jurisdiction,
                    origins=tuple(sorted(entry["origins"])),
                    dockets=tuple(sorted(entry["dockets"])),
                    documents=entry["documents"],
                    # ISO dates sort as text, which is why filing_date is text.
                    earliest_filing=min(entry["dates"]) if entry["dates"] else "",
                    latest_filing=max(entry["dates"]) if entry["dates"] else "",
                )
                for jurisdiction, entry in grouped.items()
            ),
            key=lambda entry: (-entry.documents, entry.jurisdiction),
        )
    )
    return CorpusScan(sources, tuple(unreadable), total)


def corpus_sources(data_dir: pathlib.Path | None = None) -> tuple[CorpusSource, ...]:
    """The eight commissions the real corpus came from. Reads, writes nothing."""
    return corpus_scan(data_dir).sources


@dataclass(frozen=True, slots=True)
class CorpusOutcome:
    registered: tuple[str, ...]
    already: tuple[str, ...]
    unreadable: tuple[str, ...]


def register_corpus_sources(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    now: datetime | None = None,
    data_dir: pathlib.Path | None = None,
) -> CorpusOutcome:
    """Register the commissions the filings in data/real actually came from.

    IDEMPOTENT, and matched on the corpus key rather than on the name, so an
    administrator who renamed a row does not get a second copy of it. Pressing
    the button twice is not two registrations, and the second press writes no
    audit rows.

    Every row is a public_docket, enabled, with the status
    initial_status_for_kind() gives it -- which is not_implemented, because
    nothing here fetches. That is the point of registering them: the screen then
    describes where this workspace corpus came from, honestly, rather than
    demonstrating an empty table.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    scan = corpus_scan(data_dir)
    existing = {
        (row.config or {}).get(CORPUS_KEY): row
        for row in source_registrations_for_company(session, company_id=company_id)
        if (row.config or {}).get(CORPUS_KEY)
    }

    registered: list[str] = []
    already: list[str] = []
    stamp = now or _utcnow()
    for entry in scan.sources:
        if entry.key in existing:
            already.append(existing[entry.key].id)
            continue
        row = register_source(
            session,
            company_id=company_id,
            user_id=user_id,
            name=entry.jurisdiction,
            kind=SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
            config={
                CORPUS_KEY: entry.key,
                "origins": list(entry.origins),
                "dockets": list(entry.dockets),
                "corpus_path": "data/real",
            },
            credential_ref=None,
            now=stamp,
        )
        registered.append(row.id)
    return CorpusOutcome(tuple(registered), tuple(already), scan.unreadable)


__all__ = [
    "ACTION_SOURCE_DISABLED",
    "ACTION_SOURCE_ENABLED",
    "ACTION_SOURCE_REGISTERED",
    "ACTION_SOURCE_REMOVED",
    "ACTION_SOURCE_UPDATED",
    "CONNECTION_TEST_REFUSAL",
    "CORPUS_COUNT_CAVEAT",
    "CORPUS_KEY",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_REF_PREFIX",
    "CREDENTIAL_RESOLVES",
    "FETCHES_NOTHING",
    "FORM_NOT_REFILLED",
    "HANDSHAKE_IS_NOT_A_FETCH",
    "KINDS_TAKING_A_CREDENTIAL",
    "KIND_WORDS",
    "MATCHED_BY_ADDRESS",
    "NEVER_SCANNED",
    "POINTS_AT_REGISTRATION",
    "PROVENANCE_GAP",
    "REAL_DIR",
    "STATUS_BADGE",
    "STATUS_WORDS",
    "SUBJECT_SOURCE",
    "CorpusOutcome",
    "CorpusScan",
    "CorpusSource",
    "Preflight",
    "ProvenanceCounts",
    "SourceInUse",
    "SourceRefused",
    "can_fetch",
    "config_problem",
    "corpus_scan",
    "corpus_sources",
    "credential_is_set",
    "credential_ref_problem",
    "documents_pointing_at",
    "endpoint_preflight",
    "endpoints",
    "kind_problem",
    "provenance_counts",
    "reach_problem",
    "register_corpus_sources",
    "register_source",
    "remove_source",
    "set_source_enabled",
    "source_registration_for_company",
    "source_registrations_for_company",
    "update_source",
]
