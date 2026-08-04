"""Reads over proceedings, changes, claims and escalations -- all tenant scoped.

Two rules govern this module, and both are load-bearing.

First, every read takes a company_id and refuses an unscoped call, exactly as
app/state/queries.py does. The check is imported from there rather than copied,
because two copies of a tenant guard drift and the point of a chokepoint is that
there is only one.

Second, and this is the one the product rests on: verified_claims() re-runs
citation verification against the stored source every time it is called. It does
not read a stored verdict, because a stored verdict is a promise about bytes
that may have changed since. ADR-003 says an unverified claim cannot assert
itself; that guarantee is worth only as much as the freshness of the check
behind it.

Every claim also carries a SourceFiling: the link out to the commission's own
copy of the document it cites, or a sentence saying why there is not one. The
address is checked here, once, before any template sees it -- see the block
above SourceFiling for what the four states mean and why a refused address is
not carried out of this module at all.

Nothing here writes. A read that writes cannot be run against a replica, cannot
be run in a health check, and turns rendering a page into a state change.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.state.models import Change, Claim, DocumentVersion, Escalation, Proceeding

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope, versions_for_company
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_EMPTY_SPAN,
    REASON_OUT_OF_RANGE,
    REASON_QUOTE_MISMATCH,
    Citation,
    verify_citation,
)

REASON_CODE_OUT_OF_RANGE = "CITATION_OUT_OF_RANGE"
REASON_CODE_EMPTY_SPAN = "CITATION_EMPTY_SPAN"
REASON_CODE_QUOTE_MISMATCH = "CITATION_QUOTE_MISMATCH"
REASON_CODE_AMBIGUOUS_OCCURRENCE = "CITATION_AMBIGUOUS_OCCURRENCE"
REASON_CODE_SOURCE_UNREADABLE = "CITATION_SOURCE_UNREADABLE"
REASON_CODE_LOW_CONFIDENCE = "CONFIDENCE_BELOW_THRESHOLD"
REASON_CODE_UNVERIFIED = "CITATION_UNVERIFIED"

REASON_TEXT_SOURCE_UNREADABLE = (
    "the cited source version could not be read for this company"
)
REASON_TEXT_LOW_CONFIDENCE = "confidence sits below the threshold for asserting this"

_REASON_CODES = {
    REASON_OUT_OF_RANGE: REASON_CODE_OUT_OF_RANGE,
    REASON_EMPTY_SPAN: REASON_CODE_EMPTY_SPAN,
    REASON_QUOTE_MISMATCH: REASON_CODE_QUOTE_MISMATCH,
    REASON_AMBIGUOUS_OCCURRENCE: REASON_CODE_AMBIGUOUS_OCCURRENCE,
}

# Basis points, 0..10000. Configuration rather than a constant buried in code,
# per ADR-006. It ships at 0 -- no claim is withheld for confidence alone --
# because there is no evidence yet for where the line sits, and a number picked
# to look rigorous is worse than an honest zero. docs/architecture.html lists
# calibrating it as an open question. The mechanism is here so that setting it
# is configuration, not a code change.
MIN_CONFIDENCE_BP = int(os.environ.get("STRATA_MIN_CONFIDENCE_BP", "0"))


# ---------------------------------------------------------------------------
# Where the cited document came from
# ---------------------------------------------------------------------------
#
# A citation viewer that shows an extract and no way to reach the document is
# asking the reader to trust us, and this product refuses to ask that. So every
# claim carries a SourceFiling: either a link to the commission's own copy, or a
# sentence saying why there is not one.
#
# THERE ARE FOUR ANSWERS AND THEY ARE NOT INTERCHANGEABLE. A missing link, a
# dead link and a blank space are different facts, and a reader must be able to
# tell which they are looking at:
#
#   available       the version records a web address; here it is
#   none_recorded   no address was ever recorded -- the synthetic corpus
#   refused         an address was recorded and this module would not render it
#   unreadable      the cited version could not be read at all, so nothing is
#                   known about where it came from
#
# The synthetic corpus must never be given a URL. No commission published it, so
# a plausible-looking docket link would be the product inventing provenance --
# the one failure everything else here exists to prevent.
#
# THE ADDRESS IS UNTRUSTED. Today it arrives from a JSON file on disk; tomorrow
# from a source an administrator typed. `javascript:` in an href is the oldest
# trick there is, so the scheme is checked here, once, before either template
# sees it -- and a refused address is not carried out of this module at all, not
# even as text beside the refusal. Rendering it as text is one helpful edit away
# from rendering it as a link.

SOURCE_LINK_AVAILABLE = "available"
SOURCE_LINK_NONE_RECORDED = "none_recorded"
SOURCE_LINK_REFUSED = "refused"
SOURCE_LINK_UNREADABLE = "unreadable"

#: The only two schemes a source address may carry. An allow-list, because a
#: deny-list of the dangerous ones is a list somebody has to keep complete.
SOURCE_SCHEMES = ("http", "https")

NOTE_NONE_RECORDED = (
    "This is a synthetic fixture. There is no filing to open."
)
NOTE_REFUSED = (
    "The address recorded for this filing is not an http or https address, so "
    "it is not shown. An administrator has to correct it."
)
NOTE_UNREADABLE = (
    "The cited source could not be read, so there is nowhere to send you."
)


def safe_source_url(raw: str | None) -> str | None:
    """The address if it is a web address, else None. No third answer.

    Whitespace at either end is trimmed, because a hand-typed field collects it
    and it changes nothing. A control character anywhere in the string is
    refused outright: browsers strip tabs and newlines out of an href before
    following it, so "java\\tscript:alert(1)" is a live link in a browser and a
    harmless-looking unknown scheme to a parser. Refusing the whole string is
    the only way the two agree.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        # A malformed address -- an unclosed IPv6 bracket, say. Unparseable is
        # not "probably fine".
        return None
    if parts.scheme.lower() not in SOURCE_SCHEMES:
        return None
    if not parts.hostname:
        return None
    return url


# A note on "https://psc.ky.gov@evil.example/x": credentials in front of the host
# make an address read as one place and go to another, and that one is allowed
# through here. It is not a hole. The host shown to the reader is the parsed
# hostname -- evil.example -- so the words under the cursor name where the click
# lands, which is a better defence than a refusal somebody would work around by
# using a lookalike domain instead. tests/test_source_links.py pins it.


@dataclass(frozen=True, slots=True)
class SourceFiling:
    """Where the cited document came from, ready to render and safe to render.

    `url` is set only when the address passed safe_source_url; `note` is set
    only when it did not. Exactly one of the two is ever filled, so a template
    cannot show a link and an excuse at the same time, and cannot show neither.

    `detail` is the human line -- who filed it, on what day, when we fetched it.
    It is built here rather than in a template because two templates render this
    and two copies of a sentence drift.
    """

    state: str
    url: str | None
    host: str | None
    link_text: str
    note: str
    detail: str
    filer: str | None
    filing_date: str | None
    retrieved_on: str | None


def _detail(
    filer: str | None, filing_date: str | None, retrieved_on: str | None
) -> str:
    """The provenance line, from whatever parts are actually known."""
    if filing_date and filer:
        filed = f"Filed {filing_date} by {filer}"
    elif filing_date:
        filed = f"Filed {filing_date}"
    elif filer:
        filed = f"Filed by {filer}"
    else:
        filed = ""
    fetched = f"Retrieved {retrieved_on}" if retrieved_on else ""
    return " · ".join(part for part in (filed, fetched) if part)


def _retrieved_on(retrieved_at: datetime | None) -> str | None:
    """The day we fetched it, in UTC. A day is what a reader needs here.

    A naive value cannot come from the database -- UtcDateTime refuses to store
    one -- so the zone branch only ever handles a caller passing one in. Its
    date is taken as it stands rather than being assumed to be UTC, because
    assuming a zone is inventing one.
    """
    if retrieved_at is None:
        return None
    if retrieved_at.tzinfo is not None:
        retrieved_at = retrieved_at.astimezone(timezone.utc)
    return retrieved_at.date().isoformat()


def source_filing(
    *,
    source_url: str | None,
    filer: str | None = None,
    filing_date: str | None = None,
    retrieved_at: datetime | None = None,
) -> SourceFiling:
    """Build the filing a claim shows. Never raises, always says something."""
    retrieved_on = _retrieved_on(retrieved_at)
    detail = _detail(filer, filing_date, retrieved_on)
    url = safe_source_url(source_url)

    if url is not None:
        host = urlsplit(url).hostname
        return SourceFiling(
            state=SOURCE_LINK_AVAILABLE,
            url=url,
            host=host,
            link_text=f"Open the filing at {host}",
            note="",
            detail=detail,
            filer=filer,
            filing_date=filing_date,
            retrieved_on=retrieved_on,
        )

    recorded = bool(source_url and source_url.strip())
    return SourceFiling(
        state=SOURCE_LINK_REFUSED if recorded else SOURCE_LINK_NONE_RECORDED,
        url=None,
        host=None,
        link_text="",
        note=NOTE_REFUSED if recorded else NOTE_NONE_RECORDED,
        detail=detail,
        filer=filer,
        filing_date=filing_date,
        retrieved_on=retrieved_on,
    )


#: What a claim carries when its cited version could not be read at all. Not the
#: same as a fixture with no URL: nothing here knows whether that version has a
#: filing behind it, and saying "synthetic fixture" would be a guess.
UNREADABLE_FILING = SourceFiling(
    state=SOURCE_LINK_UNREADABLE,
    url=None,
    host=None,
    link_text="",
    note=NOTE_UNREADABLE,
    detail="",
    filer=None,
    filing_date=None,
    retrieved_on=None,
)


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    """A claim whose citation was re-checked against the source just now.

    actual_text is what the source really says at the cited offsets. It is
    carried so the citation viewer marks the span from the source rather than
    echoing the quote the claim supplied, which would prove nothing.

    filing is where that source came from -- the link out to the commission's
    own copy, or the sentence saying why there is not one. It has no default:
    a claim without one cannot be built, so no screen can quietly drop the way
    out to the evidence.
    """

    claim_id: str
    statement: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    cited_occurrence: int | None
    confidence_bp: int
    actual_text: str
    filing: SourceFiling


@dataclass(frozen=True, slots=True)
class WithheldClaim:
    """A claim the product declines to make. It cannot carry what it would say.

    There is no statement field, and slots=True means one cannot be attached at
    runtime either. That is the whole design: a template cannot render what the
    object does not have, so no CSS change, no stray `{{ claim.statement }}`,
    and no helpful refactor can leak an assertion that failed its citation. A
    greyed-out assertion is still an assertion; absence is the only treatment
    the reader's eye cannot complete.

    citation_quote is here and is not the claim. It is what was *quoted*, shown
    against source_excerpt -- the real bytes at those offsets -- so the analyst
    sees the mismatch itself. source_excerpt is empty when the offsets fall
    outside the source, because then there are no real bytes to show.

    filing is here for the same reason, and it matters more here than on a
    verified claim. The reader has just been told the product will not make a
    statement; the next question is what the filing itself says, and the answer
    is one click away or it is a wall.
    """

    claim_id: str
    reason_code: str
    reason_text: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    cited_occurrence: int | None
    source_excerpt: str
    filing: SourceFiling


def proceedings_for_company(session: Session, company_id: str) -> list[Proceeding]:
    _require_scope(company_id)
    return (
        session.query(Proceeding)
        .filter(Proceeding.company_id == company_id)
        .order_by(Proceeding.id)
        .all()
    )


def changes_for_proceeding(
    session: Session, company_id: str, proceeding_id: str
) -> list[Change]:
    """A proceeding's changes, in id order, scoped to the owning company.

    The join to Proceeding is deliberate belt and braces. Change carries its own
    company_id, so filtering on it alone would usually do -- but a write bug
    that stamped the wrong company on a change would then hand one tenant
    another's rows, and the column that was supposed to prevent it is the same
    column that let it through. The join makes the parent agree.
    """
    _require_scope(company_id)
    return (
        session.query(Change)
        .join(Proceeding, Change.proceeding_id == Proceeding.id)
        .filter(Change.company_id == company_id)
        .filter(Proceeding.company_id == company_id)
        .filter(Change.proceeding_id == proceeding_id)
        .order_by(Change.id)
        .all()
    )


def change_for_company(
    session: Session, company_id: str, change_id: str
) -> Change | None:
    """One change, or None. Another company's change id resolves to None.

    None rather than a raise, because a caller looking up an id it was given is
    the normal case and a 404 is the honest answer. What must never happen is
    the row coming back.
    """
    _require_scope(company_id)
    return (
        session.query(Change)
        .join(Proceeding, Change.proceeding_id == Proceeding.id)
        .filter(Change.company_id == company_id)
        .filter(Proceeding.company_id == company_id)
        .filter(Change.id == change_id)
        .one_or_none()
    )


def claims_for_change(
    session: Session, company_id: str, change_id: str
) -> list[Claim]:
    """The stored claims for one change. Raw rows, no verification applied.

    Callers rendering anything to a person want verified_claims() instead. This
    exists for the pipeline and for tests, and returning a Claim row is not
    permission to display its statement.
    """
    _require_scope(company_id)
    return (
        session.query(Claim)
        .join(Change, Claim.change_id == Change.id)
        .filter(Claim.company_id == company_id)
        .filter(Change.company_id == company_id)
        .filter(Claim.change_id == change_id)
        .order_by(Claim.id)
        .all()
    )


def escalations_for_company(
    session: Session, company_id: str, *, unresolved_only: bool = False
) -> list[Escalation]:
    """Every escalation raised for this company's claims.

    unresolved_only is keyword-only so it cannot be passed into the company_id
    slot by accident. The review queue asks for the unresolved ones; audit asks
    for all of them, which is why resolving appends a timestamp rather than
    deleting a row.
    """
    _require_scope(company_id)
    query = (
        session.query(Escalation)
        .join(Claim, Escalation.claim_id == Claim.id)
        .filter(Escalation.company_id == company_id)
        .filter(Claim.company_id == company_id)
    )
    if unresolved_only:
        query = query.filter(Escalation.resolved_at.is_(None))
    return query.order_by(Escalation.id).all()


def _reason_code(reason: str | None) -> str:
    """Map the verifier's reason to a code. An unknown reason still withholds.

    If the verifier grows a reason this module has not been taught, the claim is
    withheld under a generic code rather than passed through. The failure is
    visible in the label and never in the direction of asserting something.
    """
    return _REASON_CODES.get(reason or "", REASON_CODE_UNVERIFIED)


@dataclass(frozen=True, slots=True)
class _Stored:
    """One stored version, as this module needs it: its bytes and its origin.

    The filing is built once per version rather than once per claim. Several
    claims on one change usually cite the same version, and parsing the same URL
    five times to reach the same answer is work nobody asked for.
    """

    text: str
    filing: SourceFiling


def _filing_for(version: DocumentVersion) -> SourceFiling:
    """The provenance columns as a thing a screen can render.

    All five are nullable and the synthetic corpus leaves them so. That is not a
    gap to paper over: NULL is how a fixture says it came from nowhere.
    """
    return source_filing(
        source_url=version.source_url,
        filer=version.filer,
        filing_date=version.filing_date,
        retrieved_at=version.source_retrieved_at,
    )


def _sources_by_version(session: Session, company_id: str) -> dict[str, _Stored]:
    """The company's stored sources, keyed by version id.

    Goes through versions_for_company -- the tenant chokepoint -- rather than
    querying DocumentVersion here. A second scoped read of the same table is a
    second thing to get wrong, and one of the two would eventually be the one
    nobody audited. A version belonging to another company is simply absent from
    this map, and an absent source withholds the claim.

    WHY NOT verify_citation_for_version(). That function makes the same pairing
    and adds a check this path does not have: it hashes the version's text and
    refuses when the digest disagrees with the one ingest recorded. It is not
    called here because it finds its version by reading every version the
    company owns, so adopting it would repeat that read once per claim instead
    of once per change. app/state/queries.py holds no read that returns a single
    scoped version; adding one belongs there, not here.

    WHAT THAT COSTS, PLAINLY. The bytes under a citation are re-read here every
    time, so an edit that lands on a cited span withholds the claim on the next
    render. An edit somewhere else in the same version does not: the quote still
    matches at its offsets, and the claim still asserts itself against a
    document that is no longer the one ingested. That is best-practices.html
    principle 28, unfixed on the path the product actually uses. Closing it
    means reporting the drift under its own reason code rather than as a quote
    mismatch, which changes what the change view and the review centre show for
    an edited source -- a decision wider than this module.
    """
    return {
        version.id: _Stored(text=version.source_text, filing=_filing_for(version))
        for version in versions_for_company(session, company_id)
    }


def verified_claims(
    session: Session,
    company_id: str,
    change_id: str,
    *,
    min_confidence_bp: int | None = None,
) -> tuple[list[VerifiedClaim], list[WithheldClaim]]:
    """Split a change's claims into what may be asserted and what may not.

    The verdict is computed here, now, by re-reading the stored source at the
    cited offsets -- never read from a stored boolean. Edit the source after a
    claim was written and the claim flips to withheld on the very next read.
    That is the behaviour ADR-003 promises and the reason there is no
    `verified` column anywhere in the schema.

    Order of checks matters. The citation is tested first, so a claim that
    misquotes reports the misquote rather than a confidence number, which is
    what the analyst needs in order to act.

    Returns (verified, withheld). Both lists follow claim id order. Nothing is
    written: the escalations table records what the pipeline already raised, and
    this list is derived, so the two cannot drift into disagreeing about what
    the source says right now.
    """
    _require_scope(company_id)
    threshold = MIN_CONFIDENCE_BP if min_confidence_bp is None else min_confidence_bp

    claims = claims_for_change(session, company_id, change_id)
    sources = _sources_by_version(session, company_id)

    verified: list[VerifiedClaim] = []
    withheld: list[WithheldClaim] = []

    for claim in claims:
        stored = sources.get(claim.citation_version_id)

        if stored is None:
            withheld.append(
                _withhold(
                    claim,
                    REASON_CODE_SOURCE_UNREADABLE,
                    REASON_TEXT_SOURCE_UNREADABLE,
                    "",
                    # Not NONE_RECORDED. Nothing here knows whether that version
                    # has a filing behind it, and a guess in either direction is
                    # the failure this module exists to refuse.
                    UNREADABLE_FILING,
                )
            )
            continue

        source_text = stored.text

        result = verify_citation(
            Citation(
                version_id=claim.citation_version_id,
                char_start=claim.citation_start,
                char_end=claim.citation_end,
                quoted_text=claim.citation_quote,
            ),
            source_text,
            expected_occurrence=claim.cited_occurrence,
        )

        if not result.verified:
            withheld.append(
                _withhold(
                    claim,
                    _reason_code(result.reason),
                    result.reason or "",
                    result.actual_text or "",
                    stored.filing,
                )
            )
            continue

        if claim.confidence_bp < threshold:
            withheld.append(
                _withhold(
                    claim,
                    REASON_CODE_LOW_CONFIDENCE,
                    REASON_TEXT_LOW_CONFIDENCE,
                    result.actual_text or "",
                    stored.filing,
                )
            )
            continue

        verified.append(
            VerifiedClaim(
                claim_id=claim.id,
                statement=claim.statement,
                citation_version_id=claim.citation_version_id,
                citation_start=claim.citation_start,
                citation_end=claim.citation_end,
                citation_quote=claim.citation_quote,
                cited_occurrence=claim.cited_occurrence,
                confidence_bp=claim.confidence_bp,
                actual_text=result.actual_text or "",
                filing=stored.filing,
            )
        )

    return verified, withheld


def _withhold(
    claim: Claim,
    reason_code: str,
    reason_text: str,
    source_excerpt: str,
    filing: SourceFiling,
) -> WithheldClaim:
    """Build the withheld form. The one place claim.statement is dropped.

    Every path that withholds goes through here, so there is a single line to
    read when asking whether the statement can escape -- and it never appears.

    `filing` has no default. A caller that has not decided what to say about the
    source cannot get a claim out of this function, which is the point: the
    default would be silence, and silence is what the reader cannot interpret.
    """
    return WithheldClaim(
        claim_id=claim.id,
        reason_code=reason_code,
        reason_text=reason_text,
        citation_version_id=claim.citation_version_id,
        citation_start=claim.citation_start,
        citation_end=claim.citation_end,
        citation_quote=claim.citation_quote,
        cited_occurrence=claim.cited_occurrence,
        source_excerpt=source_excerpt,
        filing=filing,
    )


__all__ = [
    "MIN_CONFIDENCE_BP",
    "NOTE_NONE_RECORDED",
    "NOTE_REFUSED",
    "NOTE_UNREADABLE",
    "REASON_CODE_AMBIGUOUS_OCCURRENCE",
    "REASON_CODE_EMPTY_SPAN",
    "REASON_CODE_LOW_CONFIDENCE",
    "REASON_CODE_OUT_OF_RANGE",
    "REASON_CODE_QUOTE_MISMATCH",
    "REASON_CODE_SOURCE_UNREADABLE",
    "REASON_CODE_UNVERIFIED",
    "SOURCE_LINK_AVAILABLE",
    "SOURCE_LINK_NONE_RECORDED",
    "SOURCE_LINK_REFUSED",
    "SOURCE_LINK_UNREADABLE",
    "SOURCE_SCHEMES",
    "UNREADABLE_FILING",
    "SourceFiling",
    "VerifiedClaim",
    "WithheldClaim",
    "change_for_company",
    "changes_for_proceeding",
    "claims_for_change",
    "escalations_for_company",
    "proceedings_for_company",
    "safe_source_url",
    "source_filing",
    "verified_claims",
]
