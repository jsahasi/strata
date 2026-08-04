"""Every tenant-scoped read goes through here, so there is one place to audit.

A read with no company_id is refused rather than answered. A query that returns
everything when the caller meant nothing is how tenant isolation fails in
practice, and it fails silently.
"""

from sqlalchemy.orm import Session

from app.state.models import DocumentVersion


def versions_for_company(session: Session, company_id: str) -> list[DocumentVersion]:
    if not company_id or not isinstance(company_id, str):
        raise ValueError("company_id is required; refusing an unscoped read")
    return (
        session.query(DocumentVersion)
        .filter(DocumentVersion.company_id == company_id)
        .order_by(DocumentVersion.id)
        .all()
    )
