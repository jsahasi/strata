"""Raw text in, hashed version plus offset-addressed passages out.

Ingestion never rewrites, trims or re-encodes the source. The unit of truth is
the raw text plus an integer offset into it, so anything that edited the text
here would silently invalidate every citation minted against it.
"""

import hashlib
import re

from sqlalchemy.orm import Session

from app.state.models import DocumentVersion, Passage

# A section heading looks like "SECTION 4. STUDY PROCESS" or "4.4 Study Timelines".
_SECTION = re.compile(r"^(?:SECTION\s+(\d+)\.|(\d+(?:\.\d+)*)\s+)")


def _segment(text: str) -> list[tuple[int, int, str]]:
    """Split on blank lines, returning (start, end, chunk) with exact offsets.

    Paragraph boundaries follow the document's own structure. The boundary
    choice is a retrieval convenience; correctness rests on the offsets.
    """
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        chunk = match.group(0)
        if chunk.strip():
            spans.append((match.start(), match.end(), chunk))
    return spans


def _section_of(chunk: str) -> str | None:
    match = _SECTION.match(chunk.strip())
    if not match:
        return None
    return match.group(1) or match.group(2)


def ingest_version(
    session: Session,
    *,
    version_id: str,
    company_id: str,
    docket: str,
    label: str,
    status: str,
    source_text: str,
) -> DocumentVersion:
    if status not in ("DRAFT", "FINAL"):
        raise ValueError(f"status must be DRAFT or FINAL, got {status!r}")

    version = DocumentVersion(
        id=version_id,
        company_id=company_id,
        docket=docket,
        label=label,
        status=status,
        source_text=source_text,
        source_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
    )
    session.add(version)

    current_section: str | None = None
    for ordinal, (start, end, chunk) in enumerate(_segment(source_text)):
        found = _section_of(chunk)
        if found:
            current_section = found
        session.add(
            Passage(
                version_id=version_id,
                ordinal=ordinal,
                char_start=start,
                char_end=end,
                text=chunk,
                section=current_section,
            )
        )

    session.flush()
    return version
