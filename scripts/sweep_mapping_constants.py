"""Measure the four hand-tuned constants in app/state/mapping.py.

WHAT THIS IS FOR. ADR-85 records four rules as judgements: a four-character
floor on a term, a prefix test rather than a substring test, discarding a word
more than half the company's duties share, and a two-word minimum before a duty
is offered. Each was tuned by eye against fixtures. None had ever been swept.
This sweeps each one across a range with the other three held at their shipped
values, and prints what the wrong-offer count and the refusal count do.

WHAT IT MEASURES AGAINST, SAID PLAINLY BECAUSE IT IS THE WEAK PART. The gold
set in data/evals/ is for a DIFFERENT TASK -- it labels whether a passage of a
real filing creates an obligation, and app/evals/obligations.py scores that. It
carries no change-to-obligation labels, so it cannot score this module. The
labels used here come from the synthetic corpus's own two files:

  MANIFEST  data/manifest.json names five changes and, for each, the obligation
            it bears on (CHG-5 also names a second). Projected onto the nine
            diff rows those five regions cover. Written by the corpus author
            before this module existed.
  SECTION   data/company_context.json gives every obligation a
            maps_to_docket_sections list. Matched against each diff row's own
            section label. Covers all 27 changes, and disagrees with MANIFEST in
            one place -- 5.4.3, where the section view adds OBL-004.

Neither is an independent human label of the kind data/evals/ holds. Both were
written by the same hand that designed the corpus, so they measure whether the
rule finds what the corpus intended, not whether it would find what a regulatory
analyst would. That limit is real and the reading in docs/.ai/eval-sweep.html
states it.

RUN IT:  .venv/bin/python scripts/sweep_mapping_constants.py
It writes nothing to the working database: STRATA_DATABASE_URL is pointed at a
scratch file before app.state.db is imported, exactly as tests/conftest.py does.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Before any `app.` import. app.state.db reads this at import time and init_db()
# drops tables, so a developer's strata.db would be destroyed by a sweep.
os.environ.setdefault(
    "STRATA_DATABASE_URL",
    "sqlite:///" + str(pathlib.Path(tempfile.gettempdir()) / f"strata-sweep-{os.getpid()}.db"),
)
sys.path.insert(0, str(ROOT))

from app.seed import demo_account_list, ensure_accounts, load  # noqa: E402
from app.state import mapping  # noqa: E402
from app.state.db import init_db, session_scope  # noqa: E402
from app.state.identity import user_by_email  # noqa: E402
from app.state.models import Change  # noqa: E402

COMPANY = "MEP"

CONTEXT = json.loads((ROOT / "data" / "company_context.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))

SHIPPED = {"prefix_min": 4, "match": "prefix", "share_max": 4, "min_terms": 2}


# ---------------------------------------------------------------------------
# The corpus, loaded once
# ---------------------------------------------------------------------------


def _seed_obligations(session) -> None:
    """The company's duties in the company's own words. Same read the tests make."""
    from app.state.routing import ensure_obligation

    people = {
        account.display_name: user_by_email(session, COMPANY, account.email)
        for account in demo_account_list()
    }
    for row in CONTEXT["obligations"]:
        owner = people.get(row.get("owner_name") or "")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id=row["id"],
            title=row["internal_wording"],
            owner_user_id=owner.id if owner is not None else None,
            project_id=None,
            source_document_ref=row.get("source_document_id"),
            actor="system:sweep",
        )


def build() -> None:
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        _seed_obligations(session)


# ---------------------------------------------------------------------------
# The labels
# ---------------------------------------------------------------------------


def _covers(row: Change, side: dict | None, version: str | None) -> bool:
    """Whether this diff row overlaps a manifest region on one side."""
    if not side or side.get("start") is None:
        return False
    start, end = side["start"], side["end"]
    if row.to_version_id == version and row.after_start is not None:
        if row.after_start < end and start < row.after_end:
            return True
    if row.from_version_id == version and row.before_start is not None:
        if row.before_start < end and start < row.before_end:
            return True
    return False


def manifest_labels(rows: list[Change]) -> dict[str, set[str]]:
    """Diff row id -> the obligations the manifest says its region bears on."""
    labels: dict[str, set[str]] = {}
    for change in MANIFEST["changes"]:
        duties = {change["maps_to_obligation_id"]}
        duties.update(change.get("also_related_obligation_ids") or [])
        for row in rows:
            before, after = change.get("before"), change.get("after")
            hit = _covers(row, before, (before or {}).get("version")) or _covers(
                row, after, (after or {}).get("version")
            )
            if hit:
                labels.setdefault(row.id, set()).update(duties)
    return labels


_SECTION = re.compile(r"^(\d+(?:\.\d+)*)")


def _sections_of(entry: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One maps_to_docket_sections entry, as (section numbers, versions)."""
    head = entry.split("(")[0].strip()
    numbers = tuple(part.strip() for part in head.split("/") if _SECTION.match(part.strip()))
    versions: tuple[str, ...] = ("v1", "v2", "v3")
    if "(" in entry:
        inner = entry[entry.find("(") + 1 : entry.rfind(")")]
        found = tuple(sorted(set(re.findall(r"v\d", inner))))
        if found:
            versions = found
    return numbers, versions


def section_labels(rows: list[Change]) -> dict[str, set[str]]:
    """Diff row id -> obligations whose section list names this row's section.

    Matched on the row's own section label, which the diff takes from the AFTER
    side, against the obligation's entry for the row's to_version. A row with no
    section label is labelled as bearing on nothing: the rows without one are the
    docket's title and preamble, and no company duty is about those.
    """
    wanted: dict[tuple[str, str], set[str]] = {}
    for obligation in CONTEXT["obligations"]:
        for entry in obligation.get("maps_to_docket_sections") or []:
            numbers, versions = _sections_of(entry)
            for number in numbers:
                for version in versions:
                    wanted.setdefault((number, version), set()).add(obligation["id"])
    return {
        row.id: set(wanted.get((row.section or "", row.to_version_id), set()))
        for row in rows
    }


# ---------------------------------------------------------------------------
# The four constants, made variable
# ---------------------------------------------------------------------------


def _prefix_carries(term: str, words: tuple[str, ...]) -> bool:
    return any(word.startswith(term) for word in words)


def _substring_carries(term: str, words: tuple[str, ...]) -> bool:
    return any(term in word for word in words)


def _make_discriminating(share_max: int):
    """The shared-word rule with the majority threshold made a parameter.

    share_max is how many of the company's duties may hold a word before it
    stops being evidence. The shipped rule drops a word held by MORE THAN HALF,
    which on eight duties is share_max = 4. share_max >= the number of duties
    turns the rule off.
    """

    def discriminating_terms(titles: dict[str, str]) -> dict[str, tuple[str, ...]]:
        words = {key: mapping.words_of(title) for key, title in titles.items()}
        terms = {key: mapping.terms_of(title) for key, title in titles.items()}
        kept: dict[str, tuple[str, ...]] = {}
        for key, own in terms.items():
            surviving = []
            for term in own:
                held = sum(1 for other in words.values() if mapping._carries(term, other))
                if held >= 2 and held > share_max:
                    continue
                surviving.append(term)
            kept[key] = tuple(surviving)
        return kept

    return discriminating_terms


class Config:
    """One setting of all four constants, applied to the real module."""

    def __init__(self, prefix_min: int, match: str, share_max: int, min_terms: int):
        self.prefix_min, self.match = prefix_min, match
        self.share_max, self.min_terms = share_max, min_terms

    def __enter__(self):
        self._held = (
            mapping.PREFIX_MIN,
            mapping._carries,
            mapping.discriminating_terms,
            mapping.MIN_MATCHED_TERMS,
        )
        mapping.PREFIX_MIN = self.prefix_min
        mapping._carries = (
            _prefix_carries if self.match == "prefix" else _substring_carries
        )
        mapping.discriminating_terms = _make_discriminating(self.share_max)
        mapping.MIN_MATCHED_TERMS = self.min_terms
        return self

    def __exit__(self, *_):
        (
            mapping.PREFIX_MIN,
            mapping._carries,
            mapping.discriminating_terms,
            mapping.MIN_MATCHED_TERMS,
        ) = self._held
        return False


# ---------------------------------------------------------------------------
# The scoring
# ---------------------------------------------------------------------------


class Result:
    """One run's four counts, in the vocabulary app/evals/obligations.py uses.

    WRONG is a candidate offered for a pair the labels say is not there. The
    scorer calls the analogous thing WRONG_ASSERTION and this module only ever
    PROPOSES, so the word is borrowed rather than earned -- but a wrong offer is
    still what a person has to read and throw away, and it is the count that
    should go to zero.
    """

    def __init__(self):
        self.wrong = 0
        self.missed = 0
        self.right = 0
        self.correct_refusal = 0
        self.near_miss = 0
        self.offered_per_change: list[int] = []
        self.wrong_pairs: list[tuple[str, str, tuple[str, ...]]] = []
        self.missed_pairs: list[tuple[str, str]] = []

    @property
    def refusals(self) -> int:
        """Every pair the proposer declined to offer. Right ones and wrong ones."""
        return self.correct_refusal + self.missed

    @property
    def offers(self) -> int:
        return self.right + self.wrong


def run(session, rows: list[Change], labels: dict[str, set[str]], config: Config) -> Result:
    out = Result()
    with config:
        for row in rows:
            if row.id not in labels:
                continue
            gold = labels[row.id]
            proposal = mapping.propose_obligations_for_change(
                session, COMPANY, change_id=row.id
            )
            offered = 0
            for match in proposal.obligations:
                # reason == "" is the proposer's own test for "these words
                # cleared the threshold". Read rather than .offered, because
                # .offered also folds in whether somebody already mapped or
                # rejected the pair, which is a fact about the seed rather than
                # about the rule under test.
                proposes = match.reason == ""
                wanted = match.obligation_id in gold
                if proposes:
                    offered += 1
                    if wanted:
                        out.right += 1
                    else:
                        out.wrong += 1
                        out.wrong_pairs.append(
                            (row.id, match.obligation_id, match.matched_terms)
                        )
                else:
                    if wanted:
                        out.missed += 1
                        out.missed_pairs.append((row.id, match.obligation_id))
                    else:
                        out.correct_refusal += 1
                    if match.reason == mapping.MATCH_ONE_WORD:
                        out.near_miss += 1
            out.offered_per_change.append(offered)
    return out


# ---------------------------------------------------------------------------
# The sweeps
# ---------------------------------------------------------------------------


def _line(label: str, result: Result, mark: str = "") -> str:
    per = result.offered_per_change
    worst = max(per) if per else 0
    mean = sum(per) / len(per) if per else 0.0
    return (
        f"  {label:<14} wrong {result.wrong:>3}   missed {result.missed:>3}   "
        f"right {result.right:>3}   refusals {result.refusals:>3}   "
        f"near-miss {result.near_miss:>3}   offers/change {mean:4.1f} (max {worst}){mark}"
    )


SWEEPS = {
    "prefix_min": [2, 3, 4, 5, 6, 7],
    "match": ["prefix", "substring"],
    "share_max": [1, 2, 3, 4, 5, 6, 7, 8],
    "min_terms": [1, 2, 3, 4, 5],
}


def _held_counts(session) -> dict[str, int]:
    """Every term of every duty, and how many of the eight duties hold it.

    This is the distribution the shared-word rule cuts, and printing it is the
    fastest way to see why the cut lands where it does.
    """
    titles = {row["id"]: row["internal_wording"] for row in CONTEXT["obligations"]}
    with Config(prefix_min=4, match="prefix", share_max=99, min_terms=2):
        terms = mapping.discriminating_terms(titles)
        words = {key: mapping.words_of(title) for key, title in titles.items()}
        held: dict[str, int] = {}
        for own in terms.values():
            for term in own:
                held.setdefault(
                    term, sum(1 for other in words.values() if _prefix_carries(term, other))
                )
    return held


def _coupling_check(session, rows: list[Change]) -> None:
    """Whether MATCH_ONE_WORD stays true when MIN_MATCHED_TERMS moves.

    The reason code is chosen by `MATCH_ONE_WORD if best_terms else
    MATCH_NO_OVERLAP`, which is right only while the threshold is two. This
    prints how many rows the note would lie to at each setting.
    """
    print("\nMATCH_ONE_WORD against MIN_MATCHED_TERMS")
    for min_terms in (2, 3, 4, 5):
        wrong_note = 0
        with Config(prefix_min=4, match="prefix", share_max=4, min_terms=min_terms):
            for row in rows:
                proposal = mapping.propose_obligations_for_change(
                    session, COMPANY, change_id=row.id
                )
                for match in proposal.obligations:
                    if match.reason == mapping.MATCH_ONE_WORD and len(match.matched_terms) != 1:
                        wrong_note += 1
        print(
            f"  min_terms={min_terms}: {wrong_note:>3} rows say ONE_WORD_IN_COMMON "
            "while carrying more than one word"
        )


JOINT = [
    (4, "prefix", 4, 2),
    (5, "prefix", 4, 2),
    (4, "prefix", 2, 2),
    (5, "prefix", 2, 2),
    (5, "prefix", 4, 3),
    (6, "prefix", 4, 2),
    (5, "substring", 4, 2),
]


def main() -> int:
    build()
    with session_scope() as session:
        rows = session.query(Change).filter(Change.company_id == COMPANY).order_by(Change.id).all()
        sets = {
            "MANIFEST": manifest_labels(rows),
            "SECTION": section_labels(rows),
        }

        print("\nTerms by how many of the eight duties hold them")
        held = _held_counts(session)
        for count in sorted({value for value in held.values() if value >= 2}, reverse=True):
            names = sorted(term for term, value in held.items() if value == count)
            print(f"  held by {count} of 8: {', '.join(names)}")
        print(f"  held by 1 of 8: {sum(1 for value in held.values() if value == 1)} terms")

        for name, labels in sets.items():
            positives = sum(len(v) for v in labels.values())
            print()
            print("=" * 78)
            print(
                f"{name} labels: {len(labels)} changes scored, "
                f"{positives} true pairs, {len(labels) * 8 - positives} false pairs"
            )
            print("=" * 78)
            for constant, values in SWEEPS.items():
                print(f"\n{constant} (others held at shipped values)")
                for value in values:
                    settings = dict(SHIPPED)
                    settings[constant] = value
                    result = run(session, rows, labels, Config(**settings))
                    mark = "   <- shipped" if value == SHIPPED[constant] else ""
                    print(_line(str(value), result, mark))
            print("\nmatch against min_terms (the interaction the docstring rests on)")
            for match in ("prefix", "substring"):
                for min_terms in (1, 2, 3):
                    result = run(
                        session, rows, labels, Config(4, match, 4, min_terms)
                    )
                    print(_line(f"{match} / {min_terms}", result))

            # The pairs behind the shipped setting, so a reader can check them.
            shipped = run(session, rows, labels, Config(**SHIPPED))
            print("\n  wrong offers at the shipped setting:")
            for change_id, obligation_id, terms in shipped.wrong_pairs:
                print(f"    {change_id:16s} {obligation_id}  on {', '.join(terms)}")
            print("  missed pairs at the shipped setting:")
            for change_id, obligation_id in shipped.missed_pairs:
                print(f"    {change_id:16s} {obligation_id}")

        print("\n" + "=" * 78)
        print("Joint settings, both label sets")
        print("=" * 78)
        print(
            f"  {'prefix_min':>10} {'match':>10} {'share':>6} {'terms':>6} | "
            "MANIFEST wrong/missed/right |  SECTION wrong/missed/right"
        )
        for settings in JOINT:
            left = run(session, rows, sets["MANIFEST"], Config(*settings))
            right = run(session, rows, sets["SECTION"], Config(*settings))
            print(
                f"  {settings[0]:>10} {settings[1]:>10} {settings[2]:>6} {settings[3]:>6} |"
                f"      {left.wrong:>3}/{left.missed:>3}/{left.right:>3}          |"
                f"      {right.wrong:>3}/{right.missed:>3}/{right.right:>3}"
            )

        _coupling_check(session, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
