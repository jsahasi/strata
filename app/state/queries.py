"""Every tenant-scoped read goes through here, so there is one place to audit.

A read with no company_id is refused rather than answered. A query that returns
everything when the caller meant nothing is how tenant isolation fails in
practice, and it fails silently.
"""

from sqlalchemy.orm import Session

from app.state.models import DocumentVersion, Passage


def _require_scope(company_id: str) -> str:
    if not company_id or not isinstance(company_id, str):
        raise ValueError("company_id is required; refusing an unscoped read")
    return company_id


def versions_for_company(session: Session, company_id: str) -> list[DocumentVersion]:
    _require_scope(company_id)
    return (
        session.query(DocumentVersion)
        .filter(DocumentVersion.company_id == company_id)
        .order_by(DocumentVersion.id)
        .all()
    )


def passages_for_company(
    session: Session, company_id: str, version_id: str
) -> list[Passage]:
    """A version's passages, in document order, scoped to the owning company.

    Passage carries no company_id of its own. Tenancy lives on the version that
    owns it, and this join is what enforces it -- so there is one place to audit
    rather than a column two writers could let drift apart.

    Without this scope, knowing a version id was enough to read another
    tenant's source text. Version ids are short and guessable, which made that
    reachable rather than theoretical.
    """
    _require_scope(company_id)
    return (
        session.query(Passage)
        .join(DocumentVersion, Passage.version_id == DocumentVersion.id)
        .filter(DocumentVersion.company_id == company_id)
        .filter(Passage.version_id == version_id)
        .order_by(Passage.ordinal)
        .all()
    )
