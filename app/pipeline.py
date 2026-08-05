"""The chokepoint. One version in, its typed changes out, the audit trail behind.

docs/tdd.html gives this module one job: own the order of operations. It is
the only module that imports ingestion, diff, state and audit together, so
nothing else can skip a stage by calling downstream code directly. Read this
file and you know what happens to a new version and in what order.

Three properties are deliberate.

**It is idempotent.** Feed it the same version twice and the second call writes
nothing: no second copy of the passages, no second set of changes, no second
audit entry. `make run` calls the seed on every start, and a loader that
duplicated its own output on the second run would make every count in the
product a lie by the second morning.

**It refuses a corpus that moved under it.** If a version id is already stored
with different bytes, every offset derived from it -- passages, change spans,
citations -- was computed against text that no longer exists. Rebuilding half of
it is worse than failing: the product would answer every question plausibly and
wrongly (best-practices.html §27). So it raises and names the fix.

**It calls no model, and that stays true now that one exists.** The
interpretation stage docs/tdd.html places between diff and state is now
built -- app/interpretation/propose.py judges whether one change is material --
but it runs on the change screen at render time and writes nothing back. So
`Change.materiality` still stays NULL here rather than being defaulted to a
value that reads like a judgement, and the absence is visible in the data
instead of hidden in a default.

Keeping the model off this path is the decision, not an accident of ordering.
Ingest is where a corpus is turned into offsets every later citation depends on;
a model call in the middle of it would make loading the same bytes twice produce
two different databases, and would put a network dependency inside the one
function that must work on a laptop with no key. Judgement belongs where it can
be read beside the change it is about.
"""

import hashlib

from sqlalchemy.orm import Session

from app.diff.engine import diff, passage_refs
from app.ingestion.ingest import ingest_version
from app.state.audit import record_event
from app.state.claims import change_for_company, proceedings_for_company
from app.state.models import Change, DocumentVersion
from app.state.queries import _require_scope, passages_for_company, versions_for_company

# No identity provider exists (docs/security.html), so the pipeline signs its own
# entries under a name that is plainly not a person. An audit reader must never
# have to guess whether a human decided something.
ACTOR = "system:pipeline"

ACTION_INGEST = "ingest_version"
ACTION_RECORD_CHANGE = "record_change"


class CorpusChanged(RuntimeError):
    """A stored version's bytes no longer match the text being loaded.

    Raised rather than repaired. Passages, change spans and citations are all
    offsets into the stored text; once that text changes they address different
    words, and there is no partial fix that leaves the database consistent.
    """


def change_id(from_version_id: str, to_version_id: str, ordinal: int) -> str:
    """The id a change gets. Derived, never generated, so a re-run reuses it.

    A UUID here would be correct and useless: the second run would mint new ids
    for the same changes and the row count would double. The diff is a pure
    function of the two versions, so position in its output is a stable name.
    """
    return f"CHG-{from_version_id}-{to_version_id}-{ordinal:03d}"


def _proceeding(session: Session, company_id: str, proceeding_id: str):
    """The proceeding, scoped to the company. Absence is refused, not defaulted."""
    for proceeding in proceedings_for_company(session, company_id):
        if proceeding.id == proceeding_id:
            return proceeding
    raise ValueError(
        f"no proceeding {proceeding_id!r} for company {company_id!r}; "
        "create the proceeding before loading a version into it"
    )


def _sections_by_start(session: Session, company_id: str, version_id: str) -> dict:
    """Map a passage's start offset to the section it sits under.

    The diff works on offsets and text alone, which is right -- it should not
    depend on how ingestion labels sections. The label is still worth storing on
    the change, so it is looked up here from the passages ingestion already
    wrote, through the tenant chokepoint.
    """
    return {
        passage.char_start: passage.section
        for passage in passages_for_company(session, company_id, version_id)
    }


def _existing_version(
    session: Session, company_id: str, version_id: str
) -> DocumentVersion | None:
    """This company's stored version with that id, if it has one.

    Scoped, so it cannot see another company's row. A version id already taken
    by another company therefore falls through to the insert and fails loudly on
    the primary key, which is the safe direction: a silent reuse would hand one
    tenant another's source text.
    """
    for version in versions_for_company(session, company_id):
        if version.id == version_id:
            return version
    return None


def ingest_and_diff(
    session: Session,
    *,
    company_id: str,
    proceeding_id: str,
    version_id: str,
    label: str,
    status: str,
    source_text: str,
    previous_version_id: str | None = None,
) -> list[Change]:
    """Ingest one version, diff it against its predecessor, persist the changes.

    Returns the persisted `Change` rows in document order -- the same rows on a
    second call with the same input, because nothing new is written.

    `previous_version_id` is None for the first version of a proceeding. That
    means no diff and an empty list, which is the truth: there is nothing to
    compare against yet. It does not mean the version was skipped.
    """
    _require_scope(company_id)
    proceeding = _proceeding(session, company_id, proceeding_id)

    stored = _existing_version(session, company_id, version_id)
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    if stored is None:
        ingest_version(
            session,
            version_id=version_id,
            company_id=company_id,
            docket=proceeding.docket,
            label=label,
            status=status,
            source_text=source_text,
        )
        record_event(
            session,
            company_id=company_id,
            actor=ACTOR,
            action=ACTION_INGEST,
            subject_type="document_version",
            subject_id=version_id,
            reason=(
                f"ingested {label} as {status} into docket {proceeding.docket}; "
                f"sha256 {digest}"
            ),
            citation=f"{version_id}:0:{len(source_text)}",
        )
    elif stored.source_sha256 != digest:
        raise CorpusChanged(
            f"version {version_id!r} is stored with different bytes "
            f"({stored.source_sha256[:12]} on disk, {digest[:12]} offered). "
            "Every passage, change span and citation derived from it addresses "
            "the stored text. Delete the database and reload the corpus rather "
            "than mixing two texts under one id."
        )
    # A version already stored with the same bytes is left alone, and no audit
    # entry is written. The log records what the system decided, not how many
    # times it was asked.

    if previous_version_id is None:
        return []

    if _existing_version(session, company_id, previous_version_id) is None:
        raise ValueError(
            f"previous version {previous_version_id!r} is not stored for company "
            f"{company_id!r}; load the proceeding's versions in order"
        )

    before = passage_refs(session, previous_version_id, company_id=company_id)
    after = passage_refs(session, version_id, company_id=company_id)

    before_sections = _sections_by_start(session, company_id, previous_version_id)
    after_sections = _sections_by_start(session, company_id, version_id)

    rows: list[Change] = []

    for ordinal, change in enumerate(diff(before, after)):
        row_id = change_id(previous_version_id, version_id, ordinal)

        existing = change_for_company(session, company_id, row_id)
        if existing is not None:
            rows.append(existing)
            continue

        section = None
        if change.after is not None:
            section = after_sections.get(change.after.char_start)
        if section is None and change.before is not None:
            section = before_sections.get(change.before.char_start)

        row = Change(
            id=row_id,
            company_id=company_id,
            proceeding_id=proceeding_id,
            from_version_id=previous_version_id,
            to_version_id=version_id,
            change_type=change.change_type,
            before_start=None if change.before is None else change.before.char_start,
            before_end=None if change.before is None else change.before.char_end,
            after_start=None if change.after is None else change.after.char_start,
            after_end=None if change.after is None else change.after.char_end,
            section=section,
            alignment_confidence=change.alignment_confidence,
            # No model call exists, so nothing may claim to have judged this.
            materiality=None,
            # Copied from the version being loaded, at write time, per ADR-005.
            status=status,
        )
        session.add(row)
        session.flush()

        record_event(
            session,
            company_id=company_id,
            actor=ACTOR,
            action=ACTION_RECORD_CHANGE,
            subject_type="change",
            subject_id=row_id,
            reason=(
                f"{change.change_type} between {previous_version_id} and "
                f"{version_id} in section {section or 'unlabelled'}; "
                f"alignment confidence {change.alignment_confidence:.2f}"
            ),
            citation=_citation_for(change, previous_version_id, version_id),
        )
        rows.append(row)

    return rows


def _citation_for(change, from_version_id: str, to_version_id: str) -> str:
    """Where in the source this change happened, as version:start:end.

    The after side where there is one, because that is the text now in force. A
    removal has no after side, so it cites what was taken out.
    """
    if change.after is not None:
        return f"{to_version_id}:{change.after.char_start}:{change.after.char_end}"
    if change.before is not None:
        return f"{from_version_id}:{change.before.char_start}:{change.before.char_end}"
    return ""


__all__ = [
    "ACTION_INGEST",
    "ACTION_RECORD_CHANGE",
    "ACTOR",
    "CorpusChanged",
    "change_id",
    "ingest_and_diff",
]
