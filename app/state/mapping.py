"""What a change might bear on, proposed in words a person can check.

THE HOLE THIS FILLS, said plainly. app/state/routing.py can walk a change to an
obligation to an owner and refuses in fifteen named ways when it cannot. Every
one of those refusals was reachable and exactly one of them ever fired:
ROUTE_NO_OBLIGATION, on every change in the product, because nothing anywhere
wrote a row into change_obligations. The table had a writer
(map_change_to_obligation), the writer had tests, and no caller. So the sentence
this product is named for -- map the change to the duties it affects and route
an action from there -- stopped one step before the map.

WHAT THIS MODULE IS NOT ALLOWED TO BE. ADR-008 reserves the semantic join --
the company's own words against the docket's words -- for embeddings that are
not built, and data/company_context.json is deliberately arranged to defeat
anything less: eight obligations, each carrying a note_for_semantic_join field
that says where the wording does NOT line up. OBL-001 says "post security" where
the docket says "post collateral". OBL-002's compliance date moves in the docket
and the internal wording never mentions a date at all. Those are the cases a
lexical rule cannot reach, and the corpus put them there on purpose.

So this proposes and never asserts, and the distinction is carried in the data
rather than in a caption. A row in change_obligations records mapped_by_kind:
AUTHOR_SYSTEM is a candidate the pipeline offered, AUTHOR_ANALYST is a mapping a
person made. The screen renders the two in different shapes, and
resolve_change_owner is downstream of both -- see the limit named at the foot of
this docstring, which is real and is not fixed here.

--------------------------------------------------------------------------
HOW A CANDIDATE IS PRODUCED, AND WHY IT IS NOT A SEARCH
--------------------------------------------------------------------------

The question is not "which passage best matches these words", which is what
app/state/search.py answers. It is "of the passages THIS CHANGE TOUCHES, which
carry the words of this duty" -- a comparison over a handful of already
identified passages rather than a search over 8,707 of them. Handing that to
FTS5 would be the wrong tool twice over: match_expression builds a CONJUNCTION
of phrases, and an obligation's title is a whole sentence, so a conjunction over
it matches nothing anywhere in the corpus. Building an OR expression instead
would be a second expression builder beside the one search.py argues at length
for keeping single.

What is reused is the part that has to agree, and it is imported rather than
copied: index_body(), so a passage is folded here exactly as the index folded
it, and PREFIX_MIN, so a term is widened here exactly as far as the index widens
it. A ligature, a soft hyphen or a full-width digit therefore reads the same to
this module as to retrieval, which is the property search.py's third section is
about. Nothing here queries the index, so nothing here can be made wrong by an
index that was never built -- and scripts/build_index.py runs LAST in `make
seed`, after the step that seeds these mappings.

FOUR DECISIONS, EACH MEASURED AGAINST THE CORPUS RATHER THAN CHOSEN.

ONE. A TERM IS A WORD OF FOUR CHARACTERS OR MORE, and the number is PREFIX_MIN,
imported. search.py measured it for the prefix rule and its argument carries
over unchanged: below four a word stops narrowing anything -- "the", "and",
"may" -- while the cost of a word that matches half the corpus is paid by every
comparison it takes part in. It also removes the digits, since "5.4.1" splits
into 5, 4 and 1 and none of them survives the floor. That is wanted: a section
number is not evidence that a duty is affected.

TWO. A TERM MATCHES WHEN A WORD OF THE PASSAGE STARTS WITH IT, which is exactly
what FTS5 does with a trailing star and exactly what search.py's PREFIX_MIN
paragraph argues for. "cost" reaches "costs", "customer" reaches "customers",
and the plural, the participle and the gerund come back without a stemmer or a
word list. A SUBSTRING TEST WAS TRIED FIRST AND IS WRONG HERE: OBL-001 carries
the word "work", and "work" is a substring of "network", so every passage about
network upgrades proposed the security-posting duty. On a screen a reader can
see that mistake, which is the only reason it was caught -- the fallback scan in
search.py uses substrings deliberately, and it is permitted to be loose because
its output goes to a verifier rather than onto a page.

THREE. A WORD MORE THAN HALF THIS COMPANY'S DUTIES USE IS NOT EVIDENCE, because
it cannot tell one duty from another. Measured: "large" appears in six of MEP's
eight obligations, so before this rule every change mentioning a Large Load
Customer proposed six duties at once and the list was noise. Derived from the
company's own obligations rather than from a stopword list somebody types, for
the reason tests/test_tenancy_derived.py gives about hand-kept lists: a list a
person maintains cannot catch the word that person forgot. A word only ONE duty
uses is always evidence, whatever the arithmetic says, so a company with a
single obligation is not silently unable to propose anything.

FOUR. TWO WORDS, NOT ONE. One word in common between a filing and a duty is a
coincidence; the second is what makes it worth a person's glance. Measured on
the seeded corpus at one word, every change proposed four to eight duties and
the ranking carried the whole burden. A near miss is NOT dropped from the
report, it is reported as ONE_WORD_IN_COMMON, so somebody deciding whether the
threshold is set right can see what sits just under it.

WHAT THIS RULE STILL GETS WRONG, and it is worth stating rather than
discovering. "with", "each", "that" and "before" are four characters long and
appear in nearly every filing, and nothing here can tell them from "network" or
"curtailment", because the only corpus it measures against is the company's own
eight obligations. A document-frequency weight over the passage corpus is the
fix and it is not built. What stands in for it is the report itself: every
candidate prints the words that produced it, so a candidate resting on "each"
and "with" looks exactly as thin as it is. That is the same move the citation
design makes everywhere else -- show the evidence beside the assertion and let a
person judge it -- rather than a confidence number nobody can take apart.

--------------------------------------------------------------------------
ABSENCE IS DENIAL, WHICH IS HALF OF WHAT THIS MODULE DOES
--------------------------------------------------------------------------

A proposal naming two duties and saying nothing about the other six reads
exactly like a complete answer about a company with two duties. There is
nothing in the shape to notice: no key missing, no count that disagrees with
itself. That is ADR-79's argument for the coverage map, one layer up, and it
applies here word for word -- so EVERY obligation in scope gets a row, and a row
with no candidate carries the reason it has none.

Four empty rows, four different facts, and reported as the same thing three of
them are false:

  NOT_FOUND_BY_THESE_WORDS   compared; this duty's words are not in the
                             passages this change touches. A statement about the
                             words, NEVER about the subject -- a change can bear
                             on a duty at length while sharing no vocabulary
                             with it, and half this corpus is built that way.
  ONE_WORD_IN_COMMON         compared; one word matched, which is under the
                             threshold. A near miss, not a silence.
  OBLIGATION_HAS_NO_WORDS    the duty's own wording holds no word long enough to
                             search on, so nothing was compared against it.
  OBLIGATION_NOT_SEARCHED    nothing was compared at all -- the change's offsets
                             reach no stored passage. Nobody has read it.

refuse_a_short_proposal() turns the rule from a convention into an invariant,
exactly as refuse_a_short_map() does for retrieval: a future change that filters
a zero out of the list stops the read rather than quietly improving the output.

--------------------------------------------------------------------------
THE LIMIT THIS MODULE USED TO LEAVE OPEN, AND WHAT CLOSED IT
--------------------------------------------------------------------------

resolve_change_owner() did not read mapped_by_kind. A mapping this module
proposed therefore routed an escalation to an owner exactly as a mapping a
person confirmed did, and on that path a candidate had become a finding in the
most consequential place there is. It was not theoretical: a Kentucky
vegetation-management budget table shares the words "project" and "budget" with
MEP's cost-allocation duty, and the change screen printed "Sarah Lindqvist owns
OBL-005" over it.

app/state/routing.py closed it with ROUTE_MAPPING_UNCONFIRMED. A change reached
only by a proposal of this module's does not route at all: the refusal names the
person it would have handed the work to, says nobody confirmed the mapping, and
leaves the item in the shared queue. That was a change to the routing contract
every caller of resolve_change_owner depends on, which is why it is recorded
here as well as there -- this module writes the rows that refusal reads.

WHAT DID NOT CHANGE, AND IS STILL THIS MODULE'S HALF. The screen renders a
proposal and a confirmation in different shapes, the audit chain records which
kind was written, and the seed writes both so a reviewer meets the difference
rather than reading about it. confirm_obligation_for_change is the only way a
candidate becomes a person's judgement, and it is therefore the only way that
refusal is cleared.

WHAT IT COST, stated because a closed limit that hid a new one would be worse
than the open one. An owner-gap invitation can no longer put an item on the new
owner's desk when the only mapping behind it is a proposal: the invitation is
written, the account is created, and the escalation stays in the shared queue
under ROUTE_MAPPING_UNCONFIRMED until somebody confirms the mapping. That is the
wanted answer -- a person invited into this product and handed work on the
strength of a word overlap is the sharpest form of the bug, not an exception to
it -- but it is a real step somebody now has to take.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.state.audit import ACTOR_SYSTEM, ACTOR_USER, record_event
from app.state.claims import change_for_company
from app.state.models import AUTHOR_ANALYST, ChangeObligation, Obligation, Passage
from app.state.queries import _require_scope, passages_for_company
from app.state.routing import (
    ACTION_OBLIGATION_MAPPED,
    map_change_to_obligation,
    mappings_for_change,
    obligations_for_company,
)

# Imported, never restated. The folding on both sides of this comparison has to
# be the folding the index used, or a term that reaches a passage through
# retrieval fails to reach the same passage here -- and the difference would be
# invisible, because both answers look complete.
from app.state.search import PREFIX_MIN, index_body

# ---------------------------------------------------------------------------
# The thresholds
# ---------------------------------------------------------------------------

#: How many of this duty's words a passage must carry before the duty is
#: offered. See decision four in the module docstring: one is a coincidence.
MIN_MATCHED_TERMS = 2

#: How long the ranked shortlist is.
#:
#: THREE, AND IT CAPS THE SHORTLIST AND NOTHING ELSE. Proposal.obligations still
#: carries a row for every duty in scope, and Proposal.candidates is the top of
#: the ranking for a caller that wants one thing rather than a list -- the seed
#: takes candidates[0], and a caller writing a single mapping should not have to
#: re-derive the order. The change screen deliberately renders every offered
#: row rather than this shortlist, because on a screen the accounting is the
#: product and a row hidden by a cap is a duty a reader was not told about.
MAX_CANDIDATES = 3

# ---------------------------------------------------------------------------
# Why a proposal has nothing in it
# ---------------------------------------------------------------------------

#: This company has no such change. The same answer a made-up id gets, so
#: nothing here says whether the id exists somewhere else.
PROPOSAL_NO_CHANGE = "NO_SUCH_CHANGE"

#: The company has recorded no obligations at all. A fact about the company's
#: record and not about the change -- kept apart from every code below for the
#: reason app/state/search.py keeps NOTHING_IN_SCOPE apart from its own: "your
#: duties are not loaded" must never be able to read as "no duty is affected".
PROPOSAL_NO_OBLIGATIONS = "NO_OBLIGATIONS_RECORDED"

#: The change's offsets reach no stored passage, so nothing was compared.
PROPOSAL_NO_PASSAGES = "CHANGE_REACHES_NO_PASSAGE"

# ---------------------------------------------------------------------------
# Why one obligation has no candidate
# ---------------------------------------------------------------------------

MATCH_NO_OVERLAP = "NOT_FOUND_BY_THESE_WORDS"
MATCH_ONE_WORD = "ONE_WORD_IN_COMMON"
MATCH_NO_WORDS = "OBLIGATION_HAS_NO_WORDS"
MATCH_NOT_SEARCHED = "OBLIGATION_NOT_SEARCHED"

_NOTES = {
    MATCH_NO_OVERLAP: (
        "None of this duty's own words appear in the passages this change "
        "touches. That is a statement about the words and not about the duty: "
        "a change can bear on an obligation at length while sharing no "
        "vocabulary with it, which is exactly what this corpus was built to "
        "show. Map it by hand if you know it belongs."
    ),
    MATCH_ONE_WORD: (
        "One word in common, which is under the threshold for offering this as "
        "a candidate. One shared word between a filing and a duty is a "
        "coincidence more often than it is a link. It is reported rather than "
        "dropped so the threshold can be judged."
    ),
    MATCH_NO_WORDS: (
        "This duty's wording holds no word long enough to compare on, so "
        "nothing was searched for it. Nothing follows about whether the change "
        "affects it."
    ),
    MATCH_NOT_SEARCHED: (
        "Nothing was compared for this duty, because the change's offsets reach "
        "no stored passage of either filing. It is not a duty the words missed."
    ),
}

_PROPOSAL_NOTES = {
    PROPOSAL_NO_CHANGE: "No change with that id on this company's record.",
    PROPOSAL_NO_OBLIGATIONS: (
        "This company has no obligations recorded, so there was nothing to map "
        "this change against. That is a fact about the record and not about the "
        "change: load the company's duties before reading anything into it."
    ),
    PROPOSAL_NO_PASSAGES: (
        "The offsets on this change fall outside every stored passage of both "
        "filings, so no text was read and nothing was compared. Every duty "
        "below is reported as unsearched rather than as unaffected."
    ),
}

#: A word, after folding. Digits are kept at this stage and removed by the
#: length floor, so the rule stays one rule rather than two.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_actor(actor: str) -> str:
    """Imported in spirit from routing.py: an unattributed write is not auditable.

    The check is repeated rather than the private helper imported, because a
    promotion below writes the actor onto a stored row without going through
    map_change_to_obligation, and an empty string there would put a mapping on
    the record with nobody's name on it.
    """
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    return actor


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchSpan:
    """The passage that carried the words, in the shape a citation needs.

    The same five fields app/state/search.py::PassageHit carries, and for the
    same reason: a span with offsets into a stored filing can be re-read, and a
    span without them is a quotation nobody can check. text is the passage row's
    own text -- the source verbatim -- never the folded form the comparison ran
    against.
    """

    version_id: str
    ordinal: int
    section: str | None
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True, slots=True)
class ObligationMatch:
    """One duty's row in a proposal: the words that matched, or why none did.

    reason is "" if and only if this row cleared MIN_MATCHED_TERMS. Every other
    value is one of the codes above and note is the sentence that goes in front
    of a person. A caller that renders matched_terms and drops reason has thrown
    away the half of this read that is new.

    mapped says a row already exists in change_obligations for this pair, and
    mapped_by_kind says who put it there. The two are separate from the matching
    entirely: a duty a person mapped by hand can still carry NOT_FOUND_BY_THESE
    _WORDS, and that combination is not a contradiction -- it is the honest
    record of somebody knowing something the words could not reach.
    """

    obligation_id: str
    title: str
    project_id: str | None
    document_ref: str | None
    matched_terms: tuple[str, ...]
    span: MatchSpan | None
    mapped: bool
    mapped_by_kind: str
    reason: str
    note: str

    @property
    def offered(self) -> bool:
        """Whether this row is a candidate somebody is being asked about."""
        return self.reason == "" and not self.mapped


@dataclass(frozen=True, slots=True)
class Proposal:
    """Every duty in scope, and the few worth a person's glance.

    obligations is the accounting and its length always equals in_scope --
    refuse_a_short_proposal enforces it rather than trusting it. candidates is
    the ranked shortlist for a caller that wants the best few rather than all of
    them; the same objects appear in both, so nothing can disagree between them.
    A caller showing this to a person should walk obligations and use candidates
    only for the order -- see MAX_CANDIDATES.

    searched is how many duties were actually compared. It is lower than
    in_scope only when nothing was compared at all, and it is zero rather than
    absent in that case, because a proposal that searched nothing must not be
    countable as a proposal that found nothing.
    """

    obligations: tuple[ObligationMatch, ...]
    candidates: tuple[ObligationMatch, ...]
    reason: str
    note: str
    in_scope: int
    searched: int

    @property
    def recorded(self) -> tuple[ObligationMatch, ...]:
        """The duties this change is already mapped to, in id order."""
        return tuple(row for row in self.obligations if row.mapped)

    @property
    def missed(self) -> tuple[ObligationMatch, ...]:
        """Every duty with no candidate and no mapping. The explicit zeroes."""
        return tuple(
            row for row in self.obligations if not row.mapped and row.reason
        )


def refuse_a_short_proposal(
    rows: tuple[ObligationMatch, ...] | list[ObligationMatch],
    scope: tuple[str, ...] | list[str],
) -> None:
    """Raise unless every obligation in scope has a row. The invariant, enforced.

    A PROPOSAL WITH A DUTY MISSING IS NOT A SHORTER ANSWER, IT IS A WRONG ONE.
    It reads exactly like a complete proposal over a company with fewer duties,
    and the reader draws the conclusion the missing row would have contradicted.
    Cheap, runs on every call, and it is what lets the tests in
    tests/test_change_mapping.py be trusted: a future change that filters the
    zeroes out of the list stops the read instead of quietly tidying it.
    """
    reported = [row.obligation_id for row in rows]
    seen, wanted = set(reported), set(scope)
    missing = [row_id for row_id in scope if row_id not in seen]
    extra = [row_id for row_id in reported if row_id not in wanted]
    if missing or extra:
        raise RuntimeError(
            "the proposal does not cover its scope: "
            f"missing {sorted(missing)}, unexpected {sorted(extra)}. A proposal "
            "with a duty missing reads as a complete proposal over a smaller "
            "company, so it is refused rather than returned."
        )
    if len(reported) != len(set(reported)):
        raise RuntimeError(
            "the proposal reported a duty twice, so its counts would "
            "double-count it."
        )


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------


def words_of(text: str) -> tuple[str, ...]:
    """Every word of a piece of text, folded the way the index folds it.

    index_body() is search.py's own builder and is imported rather than
    reproduced: it is what turns a ligature into two letters, deletes a soft
    hyphen and repairs a hyphenated line break, all of which arrive in
    regulatory text from PDF extraction. Case is folded here and not there,
    which matches what FTS5's tokeniser does to the index anyway -- and matches
    what search.py's own fallback scan does when it compares.
    """
    return tuple(_WORD.findall(index_body(text or "").casefold()))


def terms_of(text: str) -> tuple[str, ...]:
    """The words of a duty long enough to be evidence, in order and distinct."""
    seen: dict[str, None] = {}
    for word in words_of(text):
        if len(word) >= PREFIX_MIN:
            seen.setdefault(word, None)
    return tuple(seen)


def discriminating_terms(titles: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Each duty's words, with the ones every duty shares taken out.

    A word more than half the company's obligations use cannot say which of them
    a change is about, so it is not evidence -- see decision three in the module
    docstring, and the measurement behind it. A word only one duty uses is kept
    whatever the arithmetic says, which is what stops a company with one or two
    obligations from being unable to propose anything at all.

    Counted with the same prefix rule the comparison uses, so "costs" in one
    duty and "cost" in another count as the same word here exactly as they would
    against a passage. Counting them as two would let a plural sneak a shared
    word past the rule.
    """
    words = {key: words_of(title) for key, title in titles.items()}
    terms = {key: terms_of(title) for key, title in titles.items()}
    total = len(titles)

    kept: dict[str, tuple[str, ...]] = {}
    for key, own in terms.items():
        surviving = []
        for term in own:
            held = sum(1 for other in words.values() if _carries(term, other))
            if held >= 2 and held * 2 > total:
                continue
            surviving.append(term)
        kept[key] = tuple(surviving)
    return kept


def _carries(term: str, words: tuple[str, ...]) -> bool:
    """Whether any of these words starts with the term.

    A PREFIX AND NOT A SUBSTRING. "work" is a substring of "network", and the
    substring rule proposed the security-posting duty against every passage
    about network upgrades before this was written. The prefix is what FTS5
    itself does with a trailing star, so this stays the same rule retrieval
    applies rather than a second opinion about what matching means.
    """
    return any(word.startswith(term) for word in words)


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------


def _spans_of(change) -> tuple[tuple[str, int, int], ...]:
    """The two sides of a change, whichever of them exist.

    BOTH SIDES, NOT ONLY THE NEW ONE. A removal has no after side and its
    meaning is entirely in what went; a modification's bearing on a duty can sit
    in the sentence that was deleted as easily as in the one that replaced it.
    Reading only the after side would make every removal look like a change that
    affects nothing, which is the reading that costs the most.
    """
    sides = []
    if change.before_start is not None and change.before_end is not None:
        sides.append((change.from_version_id, change.before_start, change.before_end))
    if change.after_start is not None and change.after_end is not None:
        sides.append((change.to_version_id, change.after_start, change.after_end))
    return tuple(sides)


def _touched_passages(
    session: Session, company_id: str, change
) -> list[Passage]:
    """Every stored passage either side of this change overlaps.

    THE PASSAGE AND NOT THE CHANGED SLICE, and the choice matters. A change that
    turns "equals or exceeds" into "is equal to or greater than" is six words
    long and says nothing on its own; the paragraph it sits in is the definition
    of a Large Load Customer, and that is what bears on a duty. Comparing the
    slice alone would make every small edit look like it affects nothing, and
    the small edit inside a load-bearing clause is exactly the case this product
    exists for.

    It also means every span this module hands back is a Passage -- already in
    citation shape, with offsets into a stored filing -- which is the property
    app/state/search.py chose the same table for.

    Read through app/state/queries.py::passages_for_company, which joins
    passages to document_versions and filters on the caller's company. Passage
    carries no company_id of its own, so that join IS the tenant scope, and a
    version id naming another company's filing therefore returns nothing rather
    than that company's text.
    """
    found: list[Passage] = []
    cached: dict[str, list[Passage]] = {}
    for version_id, start, end in _spans_of(change):
        if version_id not in cached:
            cached[version_id] = passages_for_company(session, company_id, version_id)
        found.extend(
            passage
            for passage in cached[version_id]
            if passage.char_start < end and passage.char_end > start
        )
    return found


def _span_of(passage: Passage) -> MatchSpan:
    return MatchSpan(
        version_id=passage.version_id,
        ordinal=passage.ordinal,
        section=passage.section,
        char_start=passage.char_start,
        char_end=passage.char_end,
        text=passage.text,
    )


def _zero(
    obligation: Obligation,
    reason: str,
    *,
    matched: tuple[str, ...] = (),
    span: MatchSpan | None = None,
    mapped: bool = False,
    mapped_by_kind: str = "",
) -> ObligationMatch:
    return ObligationMatch(
        obligation_id=obligation.id,
        title=obligation.title,
        project_id=obligation.project_id,
        document_ref=obligation.source_document_ref,
        matched_terms=matched,
        span=span,
        mapped=mapped,
        mapped_by_kind=mapped_by_kind,
        reason=reason,
        note=_NOTES[reason],
    )


def _refused(reason: str) -> Proposal:
    return Proposal(
        obligations=(),
        candidates=(),
        reason=reason,
        note=_PROPOSAL_NOTES[reason],
        in_scope=0,
        searched=0,
    )


def propose_obligations_for_change(
    session: Session, company_id: str, *, change_id: str
) -> Proposal:
    """The duties this change might bear on, with the words that suggested each.

    Deterministic, offline, and checkable by hand: every branch below is a
    comparison between two lists of words. No model runs, nothing is written,
    and the answer to the same question twice is the same answer -- which is the
    argument for building this rather than something cleverer. A reviewer can
    take a candidate, open the span it names, and see the words for themselves.

    Scoped like every other tenant read. The change is resolved through
    app/state/claims.py::change_for_company, the duties through
    app/state/routing.py::obligations_for_company and the passages through
    app/state/queries.py::passages_for_company, so there is no fourth path to
    the scope for somebody to get wrong. Another company's change id resolves to
    nothing and answers NO_SUCH_CHANGE, which is the same answer an id belonging
    to nobody gets.

    THE COST IS ONE READ OF EACH TOUCHED VERSION'S PASSAGES, in Python. On the
    synthetic docket that is nothing; on the 1,024,000-character testimony in
    data/real it is a real read, and it is the same read the change screen
    already makes to name the section a citation sits in. If this ever gets
    called in a loop over a docket, that is the line to change -- not the
    matching, which is cheap.
    """
    _require_scope(company_id)

    change = change_for_company(session, company_id, change_id)
    if change is None:
        return _refused(PROPOSAL_NO_CHANGE)

    obligations = obligations_for_company(session, company_id)
    if not obligations:
        return _refused(PROPOSAL_NO_OBLIGATIONS)

    stored = {
        row.obligation_id: row
        for row in mappings_for_change(session, company_id, change_id)
    }
    scope = [obligation.id for obligation in obligations]

    def _mapped(obligation: Obligation) -> tuple[bool, str]:
        row = stored.get(obligation.id)
        return (row is not None, row.mapped_by_kind if row is not None else "")

    passages = _touched_passages(session, company_id, change)
    if not passages:
        # NOT a proposal that found nothing. The offsets reach no stored
        # passage, so no text was read, and every duty is reported as unsearched
        # rather than as unaffected -- the same distinction search.py draws
        # between VERSION_NOT_SEARCHED and NO_PASSAGE_MATCHED, one layer up.
        rows = tuple(
            _zero(
                obligation,
                MATCH_NOT_SEARCHED,
                mapped=_mapped(obligation)[0],
                mapped_by_kind=_mapped(obligation)[1],
            )
            for obligation in obligations
        )
        refuse_a_short_proposal(rows, scope)
        return Proposal(
            obligations=rows,
            candidates=(),
            reason=PROPOSAL_NO_PASSAGES,
            note=_PROPOSAL_NOTES[PROPOSAL_NO_PASSAGES],
            in_scope=len(rows),
            searched=0,
        )

    terms = discriminating_terms(
        {obligation.id: obligation.title for obligation in obligations}
    )
    passage_words = {passage.id: words_of(passage.text) for passage in passages}

    rows: list[ObligationMatch] = []
    for obligation in obligations:
        mapped, kind = _mapped(obligation)
        own = terms[obligation.id]
        if not own:
            rows.append(
                _zero(
                    obligation,
                    MATCH_NO_WORDS,
                    mapped=mapped,
                    mapped_by_kind=kind,
                )
            )
            continue

        # THE PASSAGE THAT CARRIES THE MOST OF THIS DUTY'S WORDS, and ties go to
        # the earlier passage in document order. A tie broken by whatever order
        # the rows arrived in would make the span a reader is shown move between
        # two renders of the same page, which is the kind of instability that
        # makes somebody stop trusting a screen they cannot see a reason for.
        best_terms: tuple[str, ...] = ()
        best_span: MatchSpan | None = None
        for passage in passages:
            matched = tuple(
                term for term in own if _carries(term, passage_words[passage.id])
            )
            if len(matched) > len(best_terms):
                best_terms, best_span = matched, _span_of(passage)

        if len(best_terms) >= MIN_MATCHED_TERMS:
            rows.append(
                ObligationMatch(
                    obligation_id=obligation.id,
                    title=obligation.title,
                    project_id=obligation.project_id,
                    document_ref=obligation.source_document_ref,
                    matched_terms=best_terms,
                    span=best_span,
                    mapped=mapped,
                    mapped_by_kind=kind,
                    reason="",
                    note="",
                )
            )
        else:
            rows.append(
                _zero(
                    obligation,
                    MATCH_ONE_WORD if best_terms else MATCH_NO_OVERLAP,
                    matched=best_terms,
                    span=best_span,
                    mapped=mapped,
                    mapped_by_kind=kind,
                )
            )

    refuse_a_short_proposal(rows, scope)

    # Ranked by how many of the duty's own words the filing carries, then by id.
    # The second half is not decoration: two duties matching three words each is
    # ordinary on this corpus, and which of them the cap keeps must not depend on
    # the order rows came back in.
    candidates = sorted(
        (row for row in rows if row.offered),
        key=lambda row: (-len(row.matched_terms), row.obligation_id),
    )
    return Proposal(
        obligations=tuple(rows),
        candidates=tuple(candidates[:MAX_CANDIDATES]),
        reason="",
        note="",
        in_scope=len(rows),
        searched=len(rows),
    )


# ---------------------------------------------------------------------------
# The confirmation
# ---------------------------------------------------------------------------

#: What the audit reason says about where a confirmed mapping came from. Two
#: sentences, because the difference between them is the whole argument for
#: proposing rather than asserting: one records a person agreeing with the
#: words, the other records a person knowing something the words could not
#: reach. Folding them would make "how often is the proposer right" a question
#: nobody can answer from the record.
CONFIRMED_A_CANDIDATE = "confirmed a candidate proposed on {terms}"
CONFIRMED_UNPROPOSED = "not proposed by these words; a person mapped it anyway"

#: What the chain says when a person confirms a mapping the pipeline had already
#: written. A SECOND obligation.mapped row for the same pair, and that is the
#: honest record rather than an untidiness: the pipeline decided at one moment
#: and a person decided at another, and both happened.
#:
#: THE ACTION CODE IS BORROWED AND THE MISSING ONE IS NAMED HERE.
#: app/state/audit.py owns the vocabulary and has obligation.mapped and
#: obligation.owner_set; it has nothing for "somebody stood behind a mapping the
#: machine proposed". obligation.confirmed is the code that fits, and inventing
#: a second spelling in this file is the exact failure the ACTION_ constants
#: were consolidated to stop -- a row that verifies perfectly and that no query
#: for the right code ever returns. So this reuses obligation.mapped, the reason
#: text carries the distinction, and whoever next edits audit.py knows what to
#: add.
CONFIRMED_A_STORED_CANDIDATE = (
    "confirmed a mapping the pipeline had proposed and stored (was {was})"
)


def confirm_obligation_for_change(
    session: Session,
    company_id: str,
    *,
    change_id: str,
    obligation_id: str,
    actor: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> ChangeObligation:
    """A person says this change bears on this duty. Written as theirs.

    The row carries AUTHOR_ANALYST rather than AUTHOR_SYSTEM, which is the
    entire reason ChangeObligation.mapped_by_kind exists: a mapping a person
    made and a mapping the pipeline proposed must not read alike, and the
    difference is not recoverable later from a name.

    ANY DUTY OF THIS COMPANY MAY BE CONFIRMED, NOT ONLY A PROPOSED ONE, and that
    is the point rather than a gap in the check. The corpus is built so that
    OBL-002 shares no words with the change that moves its compliance date; a
    product that only let a person accept what its own words found would be a
    product that can never be told it was wrong. What the check does instead is
    RECORD which of the two happened, so "how often did the proposer find the
    thing a person went on to map" is a question the chain can answer.

    A MAPPING THE PIPELINE ALREADY STORED IS PROMOTED, NOT LEFT ALONE, and this
    file argued the opposite for an afternoon. map_change_to_obligation is
    idempotent on the pair, so delegating the whole job to it meant a person
    pressing "this change bears on OBL-005" against a candidate the seed had
    already written got a 303, no row written, no chain entry, and a page that
    still said "proposed by the pipeline, not confirmed". Every seeded candidate
    in the workspace had a dead button on it. The argument for that behaviour --
    that a button writing nothing must not turn a machine's guess into a
    person's finding -- had the failure backwards: the person made a judgement
    and the product threw it away.

    So a stored AUTHOR_SYSTEM row has its attribution moved to the person, and
    all four of its columns move together, because a row carrying the machine's
    timestamp beside the person's name describes an act nobody performed. What
    is NOT lost is when the pipeline proposed it: the chain already holds that
    event, and this appends a second one rather than editing the first. The
    append-only record keeps both decisions in the order they were taken, which
    is the property ChangeObligation's "nothing here unmaps" note is protecting.

    Confirming a pair a person has ALREADY confirmed is a double click: nothing
    is written and nothing is appended. Re-stating a fact is not a decision, and
    set_obligation_owner takes the same line for the same reason.
    """
    _require_scope(company_id)
    stamp = now or _utcnow()

    # The proposal is computed BEFORE the write and only to describe it. It is a
    # read of the same rows the write is about to check, so it cannot disagree
    # with them, and its answer goes into the reason rather than into a gate.
    proposal = propose_obligations_for_change(session, company_id, change_id=change_id)
    if proposal.reason == PROPOSAL_NO_CHANGE:
        # The promotion path below never reaches map_change_to_obligation, so
        # the cross-tenant refusal has to be made here as well. Same words a
        # missing change gets: nothing about whose it is.
        raise ValueError(f"no change {change_id!r} for this company")

    proposed = {row.obligation_id: row for row in proposal.candidates}
    if obligation_id in proposed:
        note = CONFIRMED_A_CANDIDATE.format(
            terms=", ".join(proposed[obligation_id].matched_terms)
        )
    else:
        note = CONFIRMED_UNPROPOSED

    stored = {
        row.obligation_id: row
        for row in mappings_for_change(session, company_id, change_id)
    }
    held = stored.get(obligation_id)
    if held is not None:
        if held.mapped_by_kind == AUTHOR_ANALYST:
            return held
        was = held.mapped_by
        held.mapped_by = _require_actor(actor)
        held.mapped_by_kind = AUTHOR_ANALYST
        held.mapped_at = stamp
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_OBLIGATION_MAPPED,
            subject_type="change",
            subject_id=change_id,
            reason=(
                f"bears on {obligation_id} ({AUTHOR_ANALYST}); "
                + CONFIRMED_A_STORED_CANDIDATE.format(was=was)
            ),
            occurred_at=stamp,
            actor_user_id=actor_user_id,
            actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
        )
        session.flush()
        return held

    return map_change_to_obligation(
        session,
        company_id,
        change_id=change_id,
        obligation_id=obligation_id,
        mapped_by=actor,
        mapped_by_kind=AUTHOR_ANALYST,
        mapped_at=stamp,
        actor_user_id=actor_user_id,
        note=note,
    )


__all__ = [
    "CONFIRMED_A_CANDIDATE",
    "CONFIRMED_UNPROPOSED",
    "MATCH_NOT_SEARCHED",
    "MATCH_NO_OVERLAP",
    "MATCH_NO_WORDS",
    "MATCH_ONE_WORD",
    "MAX_CANDIDATES",
    "MIN_MATCHED_TERMS",
    "PROPOSAL_NO_CHANGE",
    "PROPOSAL_NO_OBLIGATIONS",
    "PROPOSAL_NO_PASSAGES",
    "MatchSpan",
    "ObligationMatch",
    "Proposal",
    "confirm_obligation_for_change",
    "discriminating_terms",
    "propose_obligations_for_change",
    "refuse_a_short_proposal",
    "terms_of",
    "words_of",
]
