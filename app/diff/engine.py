"""Pure function: two passage sequences in, typed changes out.

No model, no network, no clock, no randomness. Given the same two sequences it
returns the same list every time, which is what lets the corpus's deliberate
edits be asserted exactly rather than approximately.

The known weakness is wholesale restructuring: when a section is renumbered and
relocated, sequence alignment sees a deletion and an addition. The design does
not pretend otherwise. It reports alignment confidence so a low-confidence
alignment escalates instead of being presented as settled.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.text.normalize import normalize

# Confidence ceiling applied when two passages read almost identically but sit
# under different section numbers. Chosen to fall below any plausible escalation
# threshold, so a renumbering can never present itself as a settled match.
RESTRUCTURE_CONFIDENCE_CEILING = 0.5

_SECTION_LABEL = re.compile(r"^\s*(?:SECTION\s+)?(\d+(?:\.\d+)*)", re.IGNORECASE)


@dataclass(frozen=True)
class PassageRef:
    version_id: str
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True)
class Change:
    change_type: str
    before: PassageRef | None
    after: PassageRef | None
    alignment_confidence: float


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def _section_label(text: str) -> str | None:
    """The leading section identifier, e.g. "6" or "5.4.1". None if absent."""
    match = _SECTION_LABEL.match(normalize(text))
    return match.group(1) if match else None


def _alignment_confidence(before_text: str, after_text: str) -> float:
    """How much to trust that these two passages are the same passage.

    Text similarity alone is the wrong measure, and wrong in the dangerous
    direction. When a section is renumbered and relocated -- Section 6 becoming
    subsection 5.4 -- the words barely move, so similarity runs high precisely
    when the structural identity has changed. Reporting that as a confident
    match is how a restructure gets silently mistaken for a small edit.

    So a disagreement in section number caps the score. The passages may well
    correspond, but the alignment is now an inference rather than an
    observation, and ADR-004 says an uncertain alignment escalates rather than
    presents itself as settled.
    """
    text_similarity = _similarity(before_text, after_text)

    before_label = _section_label(before_text)
    after_label = _section_label(after_text)

    if before_label and after_label and before_label != after_label:
        return min(text_similarity, RESTRUCTURE_CONFIDENCE_CEILING)

    return text_similarity


def diff(before: list[PassageRef], after: list[PassageRef]) -> list[Change]:
    """Align two passage sequences and classify what differs between them."""
    left = [normalize(ref.text) for ref in before]
    right = [normalize(ref.text) for ref in after]

    changes: list[Change] = []
    matcher = SequenceMatcher(None, left, right, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # Pair positionally, then report any surplus on either side.
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                old, new = before[i1 + offset], after[j1 + offset]
                changes.append(
                    Change(
                        "modified", old, new, _alignment_confidence(old.text, new.text)
                    )
                )
            for index in range(i1 + paired, i2):
                changes.append(Change("removed", before[index], None, 0.0))
            for index in range(j1 + paired, j2):
                changes.append(Change("added", None, after[index], 0.0))
        elif tag == "delete":
            for index in range(i1, i2):
                changes.append(Change("removed", before[index], None, 0.0))
        elif tag == "insert":
            for index in range(j1, j2):
                changes.append(Change("added", None, after[index], 0.0))

    return changes


def passage_refs(session, version_id: str, *, company_id: str) -> list[PassageRef]:
    """Read a version's passages, in document order, scoped to one company.

    company_id is keyword-only and has no default, so a caller cannot omit it by
    accident and cannot pass it positionally into the wrong slot. Every read goes
    through app.state.queries, which is the one place tenant scoping is enforced
    and therefore the one place worth auditing.
    """
    from app.state.queries import passages_for_company

    rows = passages_for_company(session, company_id, version_id)
    return [
        PassageRef(version_id, row.char_start, row.char_end, row.text) for row in rows
    ]
