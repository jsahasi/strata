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

Nothing here writes. A read that writes cannot be run against a replica, cannot
be run in a health check, and turns rendering a page into a state change.
"""

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.state.models import Change, Claim, Escalation, Proceeding

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


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    """A claim whose citation was re-checked against the source just now.

    actual_text is what the source really says at the cited offsets. It is
    carried so the citation viewer marks the span from the source rather than
    echoing the quote the claim supplied, which would prove nothing.
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


def _source_text_by_version(session: Session, company_id: str) -> dict[str, str]:
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
        version.id: version.source_text
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
    sources = _source_text_by_version(session, company_id)

    verified: list[VerifiedClaim] = []
    withheld: list[WithheldClaim] = []

    for claim in claims:
        source_text = sources.get(claim.citation_version_id)

        if source_text is None:
            withheld.append(
                _withhold(
                    claim,
                    REASON_CODE_SOURCE_UNREADABLE,
                    REASON_TEXT_SOURCE_UNREADABLE,
                    "",
                )
            )
            continue

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
            )
        )

    return verified, withheld


def _withhold(
    claim: Claim, reason_code: str, reason_text: str, source_excerpt: str
) -> WithheldClaim:
    """Build the withheld form. The one place claim.statement is dropped.

    Every path that withholds goes through here, so there is a single line to
    read when asking whether the statement can escape -- and it never appears.
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
    )


__all__ = [
    "MIN_CONFIDENCE_BP",
    "REASON_CODE_AMBIGUOUS_OCCURRENCE",
    "REASON_CODE_EMPTY_SPAN",
    "REASON_CODE_LOW_CONFIDENCE",
    "REASON_CODE_OUT_OF_RANGE",
    "REASON_CODE_QUOTE_MISMATCH",
    "REASON_CODE_SOURCE_UNREADABLE",
    "REASON_CODE_UNVERIFIED",
    "VerifiedClaim",
    "WithheldClaim",
    "change_for_company",
    "changes_for_proceeding",
    "claims_for_change",
    "escalations_for_company",
    "proceedings_for_company",
    "verified_claims",
]
