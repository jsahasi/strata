"""Retrieval over docket text, built to serve the assistant and nothing else.

WHAT THIS IS NOT, BECAUSE THE DISTINCTION IS THE WHOLE POINT. ADR-002 rejected
search as the product's wedge, and the reason still holds word for word: search
assumes the analyst already knows what to look for, and the expensive part --
the interpretation -- is exactly what search skips. docs/future-enhancements.html
lists general regulatory search as deliberately excluded. None of that is
reversed here. There is no search box, no search screen and no nav item, and
adding one would be a product decision this module does not make.

What is built is an internal capability with one caller: the assistant's tool
layer, so that Clarke can FIND THE PASSAGE IT NEEDS TO CITE rather than scanning
substrings over the claims the product has already written. The difference is
not cosmetic. A search feature hands a person a ranked list and asks them to
judge it, which puts the ranking in front of the evidence and makes a good rank
look like an answer. Retrieval inside the citation path hands the model a
candidate, and the candidate then has to earn its place through the same gate
every claim goes through. The first would undercut the citation story; the
second is what makes it work on a corpus bigger than a page.

A RANK IS A CANDIDATE, NEVER EVIDENCE. bm25 says a passage uses the query's
words often and other passages do not. It says nothing about whether the passage
supports a claim, and a highly ranked passage whose citation does not verify is
still withheld. Nothing in this module returns a verdict, and nothing downstream
may read a score as one.

WHY SQLITE FTS5 AND NO NEW DEPENDENCY. FTS5 is compiled into the SQLite this
project already runs (3.53.1 here, ENABLE_FTS5 in its compile options), so the
index is a table in the database the product already opens. No service, no
process, no package in requirements.txt, nothing for a reviewer to install
before `make run` works. The alternatives and what they cost are in the ADR.

WHY THE INDEX POINTS AT Passage AND NOT AT A CHUNKER OF ITS OWN. app/state/models.py
already stores every passage with version_id, ordinal, char_start, char_end,
text and section -- so a hit is ALREADY IN CITATION SHAPE and composes straight
into verify_citation. Most retrieval systems chunk text without keeping offsets
back into the source, and then cannot cite what they retrieve. That property is
the reason this is cheap here, and losing it would cost far more than the index
is worth. tests/test_search.py takes a hit out of the index and runs it through
the real verifier for exactly that reason.

--------------------------------------------------------------------------
THE THREE THINGS THAT MAKE THIS RISKIER THAN IT LOOKS
--------------------------------------------------------------------------

ONE. TENANCY. An FTS5 virtual table is its own object and the guard in
app/state/queries.py does not reach inside it. Passage carries no company_id at
all -- tenancy lives on the DocumentVersion that owns it -- so an index queried
on its own terms hands back every company's rowids. The read therefore lives in
app/state/queries.py with every other tenant read, joins passages to
document_versions, and filters on the caller's company. Nothing in this file
queries the index directly. A second copy of the tenant join is a second thing
to get wrong, and the copy nobody audited is the one that leaks.

The ledger table below deliberately does NOT carry a company_id, though it
easily could. A denormalised tenant column is a second answer to "who owns this
row", and on the day the two disagree there is no way to say which is right.
The join is the answer; there is one of it.

TWO. THE INDEX IS A DERIVED CORPUS. best-practices.html section 27: derived data
is comparable only within the scheme that produced it, and half-migrated it does
not fail -- it answers every query, plausibly, and wrongly. So the index is not
a migration and app/state/migrate.py never backfills by design. It has its own
build step, it is written inside a single transaction so it lands whole or not
at all, and a read that finds it missing or stale REFUSES TO ANSWER FROM IT.

Three states, three named reasons, and every one of them degrades to a complete
answer rather than a short one:

  INDEX_MISSING       nothing has built it. A fresh checkout is in this state.
  INDEX_STALE         it does not cover the passages this company now owns, or a
                      passage it returned no longer matches its fingerprint.
  INDEX_UNAVAILABLE   this database is not SQLite, or FTS5 is not compiled in.

In all three the answer comes from a scan of every passage in scope, the result
says which path answered and why, and the caller passes that on. Returning
fewer results with no announcement is the worst available outcome here, because
the caller cannot tell "nothing matched" from "the index was empty" -- that is
best-practices.html section 26, and this is the shape it warns about: a value of
the right type and shape, and not the right quality.

WHAT THE STALENESS CHECK DOES NOT CATCH, said plainly. Coverage is counted, and
every hit that comes back is checked against the fingerprint recorded when it
was indexed. What is not checked is a passage that was edited in place, indexed
under its old words, and never returned -- the counts do not move and nothing
looks at it. Catching that means fingerprinting the whole corpus on every query,
which is the cost the index exists to avoid. It is inert today because nothing
in app/ updates a Passage row: app/ingestion/ingest.py inserts them and no code
path edits or deletes one. It stops being inert the day something does, and
whoever writes that path owns this paragraph.

THREE. TOKENISATION HAS TO AGREE WITH app/text/normalize.py. Regulatory text
arrives from PDF extraction full of ligatures, soft hyphens, hyphenated line
breaks, full-width digits and section marks. If the index folds them differently
from the verifier, retrieval and citation stop describing the same document.

The rule that makes "agree" precise: THE INDEX MAY BE MORE PERMISSIVE THAN THE
VERIFIER AND NEVER LESS. A permissive index offers extra candidates and the gate
rejects them, which costs a little work. A restrictive index cannot find a
passage that exists and would have verified -- a silent miss, invisible to
everyone, and the failure this whole design is arranged against.

So both sides of the index are normalized by normalize(), the same function that
runs on both sides of every citation comparison, and FTS5 is left to do nothing
but split words on already-folded text. All folding is ours; the word splitting
is symmetric. Measured on this machine, against unicode61 over raw text:

  "ﬁle"           raw tokenises to one token spelled with U+FB01, so a query
                  for "file" finds nothing. Normalized it is "file".
  "main<AD>tain"  raw splits at the soft hyphen into "main" and "tain", so
                  "maintain" matches neither. Normalized it is "maintain".
  "２０ MW"        raw keeps the full-width pair as its own token, so "20 MW"
                  misses. Normalized it is "20 MW".
  "cost-\ncausation"
                  agrees either way -- both spellings split into "cost" and
                  "causation" -- so this one is repaired before it reaches us.

Three silent misses out of four PDF shapes, all in the restrictive direction,
all removed by normalizing first. And in the other direction the index keeps
what normalize() keeps: a superscript two is a footnote marker, normalize()
refuses to fold it, unicode61 keeps "20²" as one token, and a query for "202"
does not reach it. A plain NFKC index would have joined them.

Two differences remain and both are permissive, which is the safe side:

  case          FTS5 case-folds and normalize() deliberately does not, because
                "20 MW" and "20 mw" are the same words while digits and units
                are not. The substring scan this replaced was case-insensitive
                too, so nothing changed for a caller.
  punctuation   unicode61 splits on stops and section marks, so "§ 5.4.1" holds
                one searchable term. It is searched as a PHRASE -- the tokens 5,
                4 and 1 adjacent -- rather than as three separate numbers, or
                every table reference in the corpus would match it.
  word endings  a term ending in four letters or more is also matched as a
                prefix, so "curtailment" reaches "Curtailments". Without it the
                index found 9 of the 15 passages the substring scan found on the
                seeded corpus, and said nothing about the six. See PREFIX_MIN,
                including why a term ending in a digit is never widened.

The scheme is named in SCHEME and recorded on every indexed row, so an index
built under an older scheme is refused rather than mixed with a newer one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.text.normalize import normalize

#: The FTS5 virtual table. Contentless: it stores the term index and nothing
#: else, and its rowid is passages.id.
#:
#: CONTENTLESS IS A SAFETY PROPERTY HERE, not a saving. A stored copy would hold
#: the NORMALIZED text -- whitespace collapsed, ligatures expanded, soft hyphens
#: gone -- and the first person to quote from it would mint a citation whose
#: offsets point into the raw source and whose words came from somewhere else.
#: With content='' there is nothing in the index to quote, so the only text a
#: hit can carry is the passage row's own, which is the source verbatim.
INDEX_TABLE = "passage_index"

#: What was indexed, and under which scheme. An ordinary table beside the
#: virtual one, created and dropped with it, because a contentless FTS5 table
#: cannot store a column it does not index.
LEDGER_TABLE = "passage_index_row"

#: remove_diacritics 0 on purpose: unicode61 must add no folding of its own.
#: normalize() composes accents and leaves them standing, so the index leaves
#: them standing too. Every fold in this system has one implementation.
TOKENISER = "unicode61 remove_diacritics 0"

#: The name of the tokenisation scheme, stored on every indexed row. Change the
#: folding or the tokeniser and change this string in the same edit: every row
#: written under the old name is then refused, the coverage check reports the
#: index as not covering the corpus, and the whole thing is rebuilt. That is
#: what stops two schemes sharing one index, which is the failure best-practices
#: section 27 is about.
SCHEME = "normalize-then-unicode61-v1"

#: Which path answered.
SOURCE_INDEX = "index"
SOURCE_SCAN = "scan"

#: Why the index did not answer. Codes for a caller to branch on; the sentence
#: beside each is for a person to read.
REASON_INDEX_MISSING = "INDEX_MISSING"
REASON_INDEX_STALE = "INDEX_STALE"
REASON_INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
REASON_NO_TERMS = "QUERY_HAS_NO_TERMS"

#: How many candidates a caller gets by default. A model handed fifty passages
#: spends its turn on passages nobody asked for, and the cap is counted and said
#: rather than applied quietly.
DEFAULT_LIMIT = 10

#: A term worth searching for holds at least one letter or digit. "§" alone does
#: not, and neither does "--". Dropping them is permissive, which is the safe
#: direction, and the terms that survived come back in the result so a caller can
#: see what was actually searched.
_HAS_WORD = re.compile(r"\w", re.UNICODE)

#: The run of letters a term ends with, if any. Digits are excluded on purpose:
#: see PREFIX_MIN.
_TRAILING_LETTERS = re.compile(r"[^\W\d_]+$", re.UNICODE)

#: How many letters a term must end with before it is also searched as a prefix.
#:
#: MEASURED, NOT CHOSEN. Against the seeded corpus of 8,707 passages, "curtailment"
#: as an exact token found 9 passages and the substring scan it replaced found 15.
#: The six it missed all said "Curtailments". A retrieval layer that answers a
#: question about curtailment with two thirds of the passages about curtailment,
#: and says nothing, is the silent miss this whole module is arranged against --
#: and it would have been introduced by the change meant to improve retrieval.
#:
#: So the last token of a term is also matched as a prefix, which recovers the
#: plural, the participle and the gerund without a stemmer, a word list or a
#: dependency. Two limits keep it from becoming noise:
#:
#:   the tail must be LETTERS. "20" must not reach "2000" and "5.4.1" must not
#:   reach "5.4.10". Digits and units are the values this product exists to
#:   quote exactly, and ADR-003's whole argument is that a number changing
#:   quietly is the failure that matters. A superscript two is not a letter
#:   either, so "20²" stays exact and the footnote marker keeps its meaning.
#:
#:   four letters, not three. Below that a prefix stops narrowing anything --
#:   "the", "and", "may" -- while the cost of a term that matches half the
#:   corpus is paid by every other term ANDed with it.
#:
#: It only ever widens what an exact token would have found, so it stays on the
#: permissive side of the rule this module is built on.
PREFIX_MIN = 4


@dataclass(frozen=True, slots=True)
class PassageHit:
    """One candidate passage, in the shape a citation needs.

    text is the passage row's own text -- the source verbatim, at char_start to
    char_end -- and never the normalized form that was indexed. That is what
    lets a caller build a Citation from a hit and have it verify. The test that
    pins it is the one this module was hardest to get right.

    rank is bm25, where LOWER IS BETTER, which is SQLite's convention and not
    the one most people expect. It is None when the scan answered, because the
    scan does not rank and a fabricated score would read as one.
    """

    passage_id: int
    version_id: str
    ordinal: int
    section: str | None
    char_start: int
    char_end: int
    text: str
    rank: float | None


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What came back, which path produced it, and why if it was not the index.

    reason is "" only when the index answered. Every other value is a named
    code, and note is the sentence that goes in front of a person. A caller that
    passes hits on without passing reason on has thrown away the announcement,
    which is the whole mechanism.
    """

    hits: tuple[PassageHit, ...]
    terms: tuple[str, ...]
    source: str
    reason: str
    note: str
    omitted: int
    """How many matching passages the limit left out, and the two paths know
    different amounts.

    The scan reads everything in scope, so it knows the exact remainder. The
    index asks for one row beyond the limit, which is enough to learn THAT there
    are more and not how many -- so on that path this is 0 or 1, and a 1 means
    "at least one". A caller reporting it as a count would be inventing the
    number; a caller ignoring it would be hiding the fact. Say "more", not "one".
    """

    @property
    def ranked(self) -> bool:
        return self.source == SOURCE_INDEX


# ---------------------------------------------------------------------------
# The scheme: one definition of what gets indexed and what gets searched
# ---------------------------------------------------------------------------


def index_body(passage_text: str) -> str:
    """The text that goes into the index, for one passage.

    Called by the build AND by nothing else, because the query side goes through
    match_terms below and both start from the same normalize(). Two spellings of
    "what a document looks like to the index" is the drift that makes retrieval
    quietly worse in a way nobody can date -- best-practices section 27 says to
    rebuild derived text with the app's own builders, imported, and this is that
    builder.
    """
    return normalize(passage_text or "")


def match_terms(query: str) -> tuple[str, ...]:
    """The searchable words of a query, folded exactly as the index folded the text.

    Whitespace-separated, because that is the unit a person types; each term is
    then handed to FTS5 as a phrase, so a term that holds punctuation -- "5.4.1",
    "cost-causation" -- is searched as its tokens in order rather than as tokens
    scattered anywhere in the passage.
    """
    return tuple(
        term for term in normalize(query or "").split() if _HAS_WORD.search(term)
    )


def _phrase(term: str) -> str:
    """One term as an FTS5 phrase, with a prefix star where PREFIX_MIN allows it."""
    quoted = '"' + term.replace('"', '""') + '"'
    tail = _TRAILING_LETTERS.search(term)
    if tail is not None and len(tail.group(0)) >= PREFIX_MIN:
        return quoted + "*"
    return quoted


def match_expression(terms: tuple[str, ...]) -> str:
    """An FTS5 MATCH expression that can only ever be a conjunction of phrases.

    THE QUERY IS NEVER PASSED THROUGH. FTS5 has an expression language -- AND,
    OR, NOT, NEAR, column filters, prefix stars -- and a bare "AND" is a syntax
    error rather than a search for the word. A person asking about a rule that
    does NOT apply would get an exception; someone probing the box would get
    column filters. Quoting every term turns all of it into ordinary text: a
    double quote is doubled, which is FTS5's own escape, and everything else
    inside a phrase is either a token or a separator.

    THE ONE OPERATOR THAT IS OURS TO USE is the trailing star, and it is added
    by _phrase from the term's own shape rather than copied from what the person
    typed. Someone who types "plan*" gets a phrase containing the token "plan",
    not a prefix search they asked for by knowing the syntax; someone who types
    "curtailment" gets the prefix search because the rule says so. The
    difference matters: the syntax stays unreachable, and the behaviour stays
    the same for everyone whether or not they know FTS5.
    """
    return " AND ".join(_phrase(term) for term in terms)


def fingerprint(
    version_id: str, ordinal: int, char_start: int, char_end: int, passage_text: str
) -> str:
    """What the passage looked like when it was indexed.

    Covers the offsets as well as the words, because the offsets are what makes
    a hit citable and an index row pointing at the right words at the wrong
    place is worse than one pointing at nothing.

    The unit separator between fields is not decoration: joining on a character
    a filing can contain would let two different passages hash the same.
    """
    joined = "\x1f".join(
        (version_id, str(ordinal), str(char_start), str(char_end), passage_text)
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Building it. Once, whole, or not at all.
# ---------------------------------------------------------------------------


def _bind_of(handle):
    """The engine or connection behind a Session, or the handle itself.

    Written because it was got wrong once and the failure was quiet in exactly
    the way this module is about. index_present() was handed a Session, asked it
    for a dialect, found none, and reported the index absent -- so every query
    degraded to a scan, with a reason that was true of nothing. The answers
    stayed correct and complete, which is why nothing failed; the index simply
    never ran. A fallback that announces the wrong reason is still a fallback
    nobody can act on.
    """
    get_bind = getattr(handle, "get_bind", None)
    return get_bind() if get_bind is not None else handle


def _is_sqlite(handle) -> bool:
    bind = _bind_of(handle)
    return getattr(getattr(bind, "dialect", None), "name", "") == "sqlite"


# The four statements that are the index, written once. Both the build and the
# drop run them, so there is no second spelling of what this thing is.
_DDL = (
    f"DROP TABLE IF EXISTS {INDEX_TABLE}",
    f"DROP TABLE IF EXISTS {LEDGER_TABLE}",
    f"CREATE VIRTUAL TABLE {INDEX_TABLE} USING fts5("
    f"body, tokenize = \"{TOKENISER}\", content = '')",
    f"CREATE TABLE {LEDGER_TABLE} ("
    "passage_id INTEGER PRIMARY KEY, "
    "fingerprint TEXT NOT NULL, "
    "scheme TEXT NOT NULL)",
)
_DROP = _DDL[:2]
_CREATE = _DDL[2:]


def drop_index(engine) -> None:
    """Remove the index. Leaves the corpus alone; nothing here is a source of truth."""
    if not _is_sqlite(engine):
        return
    with engine.begin() as connection:
        for statement in _DROP:
            connection.execute(text(statement))


def rebuild_index(engine) -> dict:
    """Throw the index away and build it again from every passage. Returns what it did.

    THE WHOLE THING, IN ONE TRANSACTION, EVERY TIME. Not a backfill of the rows
    that are missing -- best-practices section 27 is about exactly that shape:
    backfill only what is absent and the index ends up holding two schemes at
    once, answering every query plausibly and wrongly, with no error to notice
    and the old behaviour gone. SQLite makes DDL transactional, so a failure
    anywhere in this function leaves yesterday's index standing untouched, which
    is strictly better than half of today's.

    THIS IS NOT A TENANT READ AND MUST NOT BE. It indexes the whole corpus and
    is scoped at query time by the join in app/state/queries.py. One index per
    company would be N objects to build, N to keep current, and one of them
    would be the one somebody forgot -- and a company whose index was forgotten
    would get quiet partial answers, which is the failure this module is arranged
    against.

    It is deliberately not called on a read. An index that rebuilds itself when
    a query finds it stale hides the cost of the rebuild inside somebody's
    question, and turns an explicit build step into a surprise.
    """
    if not _is_sqlite(engine):
        raise RuntimeError(
            "the passage index is SQLite FTS5. This database is "
            f"{getattr(getattr(engine, 'dialect', None), 'name', 'unknown')!r}, so "
            "there is nothing to build; retrieval degrades to a scan and says so."
        )

    indexed = 0

    # THE DRIVER CONNECTION, WITH AN EXPLICIT BEGIN, AND NOT engine.begin().
    # This was written the obvious way first and the test caught it. pysqlite
    # opens a transaction lazily, and only in front of an INSERT, UPDATE, DELETE
    # or REPLACE -- never in front of DDL. So `with engine.begin()` had no
    # transaction open when DROP TABLE ran, SQLite committed the drop on the
    # spot, and the rollback afterwards had nothing left to undo. The failure
    # mode that leaves is precisely the one this function exists to make
    # impossible: an index dropped, half rebuilt, and answering queries.
    #
    # Issuing BEGIN ourselves puts the DDL inside the transaction, where SQLite
    # is happy to have it. isolation_level is set to None first so the driver
    # stops trying to manage transactions at all, and put back afterwards
    # because the connection returns to a pool that other work will draw from.
    raw = engine.raw_connection()
    driver = raw.driver_connection
    previous = driver.isolation_level
    try:
        driver.isolation_level = None
        cursor = raw.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        for statement in _DDL:
            cursor.execute(statement)
        cursor.execute(
            "SELECT id, version_id, ordinal, char_start, char_end, text "
            "FROM passages ORDER BY id"
        )
        for passage_id, version_id, ordinal, start, end, body in cursor.fetchall():
            cursor.execute(
                f"INSERT INTO {INDEX_TABLE}(rowid, body) VALUES (?, ?)",
                (passage_id, index_body(body)),
            )
            cursor.execute(
                f"INSERT INTO {LEDGER_TABLE}(passage_id, fingerprint, scheme) "
                "VALUES (?, ?, ?)",
                (
                    passage_id,
                    fingerprint(version_id, ordinal, start, end, body),
                    SCHEME,
                ),
            )
            indexed += 1
        cursor.execute("COMMIT")
    except BaseException:
        # Roll back and re-raise unchanged. The caller decides what a failed
        # build means; this only guarantees that what is left behind is
        # yesterday's index whole, never today's index in pieces.
        raw.rollback()
        raise
    finally:
        driver.isolation_level = previous
        raw.close()

    return {"indexed": indexed, "scheme": SCHEME, "table": INDEX_TABLE}


def index_present(bind) -> bool:
    """Both objects exist. One without the other is not an index, it is a ruin."""
    if not _is_sqlite(bind):
        return False
    found = bind.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN (:index_table, :ledger_table)"
        ),
        {"index_table": INDEX_TABLE, "ledger_table": LEDGER_TABLE},
    ).scalars().all()
    return len(set(found)) == 2


def ensure_passage_index(engine) -> dict:
    """Build the index when it is absent or does not cover the corpus. Report either way.

    THE HOOK app/state/migrate.py CALLS, and the tension is worth stating rather
    than hiding. That file is additive and never backfills, on the argument that
    a value invented for an old row is a fact the record cannot support. Nothing
    here contradicts it: this writes no column and invents no value, it discards
    a derived artefact and computes it again from the stored text. A rebuild is
    not a backfill, and running it whole is what keeps section 27 satisfied.

    It exists because of section 29 -- built is not connected. An index nothing
    ever builds is an index that announces itself missing on every query for the
    life of the product, which is honest and useless. deploy/entrypoint.sh runs
    scripts/migrate.py on every start, so this is where the deploy path reaches
    it.

    A REFUSAL HERE MUST NOT STOP A DEPLOY. Retrieval degrades to a complete scan
    and says why, so a database whose FTS5 is missing should serve a slower
    product rather than no product. The refusal is reported, not raised.
    """
    report: dict[str, list[str] | int | str] = {"built": 0, "refused": []}
    if not _is_sqlite(engine):
        report["refused"] = [
            f"{INDEX_TABLE}: needs SQLite FTS5; retrieval will answer from a scan."
        ]
        return report

    try:
        with engine.connect() as connection:
            if index_present(connection):
                total = connection.execute(
                    text("SELECT count(*) FROM passages")
                ).scalar_one()
                current = connection.execute(
                    text(
                        f"SELECT count(*) FROM {LEDGER_TABLE} r "
                        "JOIN passages p ON p.id = r.passage_id "
                        "WHERE r.scheme = :scheme"
                    ),
                    {"scheme": SCHEME},
                ).scalar_one()
                if current == total:
                    return report
        report["built"] = rebuild_index(engine)["indexed"]
    except Exception as exc:  # FTS5 absent, or a corpus mid-write
        report["refused"] = [
            f"{INDEX_TABLE}: {exc}. Retrieval will answer from a scan and say so."
        ]
    return report


# ---------------------------------------------------------------------------
# Reading it. Scoped, checked, and never quietly short.
# ---------------------------------------------------------------------------

_NOTES = {
    REASON_INDEX_MISSING: (
        "The passage index has not been built, so every passage in scope was "
        "read and matched instead. The results are complete and unranked. Run "
        "scripts/build_index.py to build it."
    ),
    REASON_INDEX_STALE: (
        "The passage index does not cover this company's passages as they stand "
        "now, so it was not used. Every passage in scope was read and matched "
        "instead: the results are complete and unranked. Run "
        "scripts/build_index.py to rebuild it."
    ),
    REASON_INDEX_UNAVAILABLE: (
        "This database cannot hold the passage index, so every passage in scope "
        "was read and matched instead. The results are complete and unranked."
    ),
    REASON_NO_TERMS: (
        "That question held no searchable words, so no passage was ranked. This "
        "is not the same as finding nothing."
    ),
}


def _scan_matches(passage_text: str, terms: tuple[str, ...]) -> bool:
    """Every term appears somewhere in the folded passage. The old rule, kept.

    Deliberately the substring test this replaced rather than a second tokeniser,
    so the fallback is the behaviour that was already trusted and not a third
    opinion about what matches. It folds through index_body first, so a query for
    "maintain" reaches a passage the extractor wrote with a soft hyphen in it --
    the fallback must not be worse at the job than the index it stands in for.

    THE TWO PATHS DO NOT RETURN IDENTICAL SETS, and the difference was measured
    on the seeded corpus of 8,707 passages rather than guessed at. Across
    "curtailment", "battery energy storage", "Utility shall", "interconnection
    study" and "rate base reconciliation" the two agree exactly. They part on
    "20 MW": the scan returns four passages the index does not, and all four
    mention docket 2020-00174, where the substring "20" lands inside "2020".

    That is the scan being loose, not the index being short -- which is the
    right way round for a fallback. Every passage the index finds, the scan
    finds; a scan-only hit is a false positive a reader can see through, and an
    index-only hit would be a passage the fallback could not reach. The rule
    this module is built on holds on both paths: neither is ever quietly
    narrower than the other.
    """
    haystack = index_body(passage_text).casefold()
    return all(term.casefold() in haystack for term in terms)


def _hits_by_scan(
    session: Session, company_id: str, terms: tuple[str, ...], limit: int
) -> tuple[list[PassageHit], int]:
    """Read every passage in scope and match it. Complete, unranked, and slow.

    O(the company's corpus) per call, in Python, and that is the honest cost of
    a fallback that cannot be quietly short. On the synthetic corpus it is
    nothing. On thousands of filings it is the reason to build the index rather
    than to live here.
    """
    from app.state.queries import all_passages_for_company

    rows = all_passages_for_company(session, company_id)
    matched = [row for row in rows if _scan_matches(row.text, terms)]
    hits = [
        PassageHit(
            passage_id=row.id,
            version_id=row.version_id,
            ordinal=row.ordinal,
            section=row.section,
            char_start=row.char_start,
            char_end=row.char_end,
            text=row.text,
            rank=None,
        )
        for row in matched[:limit]
    ]
    return hits, max(0, len(matched) - limit)


def _degraded(
    session: Session,
    company_id: str,
    terms: tuple[str, ...],
    limit: int,
    reason: str,
) -> Retrieval:
    hits, omitted = _hits_by_scan(session, company_id, terms, limit)
    return Retrieval(
        hits=tuple(hits),
        terms=terms,
        source=SOURCE_SCAN,
        reason=reason,
        note=_NOTES[reason],
        omitted=omitted,
    )


def search_passages(
    session: Session,
    *,
    company_id: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
) -> Retrieval:
    """Candidate passages for a question, scoped to one company, ranked when it can be.

    Refuses an unscoped call rather than answering it, through the same guard as
    every other tenant read -- see app/state/queries.py, which refuses None, the
    empty string, whitespace, a padded id, a LIKE wildcard and anything that is
    not a string. Two defences and not one: even were the guard to let a
    wildcard through, the id reaches the database as a bound parameter and never
    as text in a statement, so it would compare as the literal characters it is.

    Four things are checked before the index is believed, in this order, and the
    first that fails sends the whole answer to the scan with its own reason:

      1. the query holds at least one searchable word;
      2. the database can hold an FTS5 index at all;
      3. both index objects exist;
      4. the index covers exactly the passages this company owns right now.

    And one more after: every hit that comes back is checked against the
    fingerprint recorded when it was indexed. A row that disagrees means the
    index is pointing at a passage that has since changed -- reachable the moment
    anything drops and recreates the passages table, because SQLite reuses
    rowids and a stale row then points at somebody else's paragraph. One
    disagreement condemns the whole answer rather than being dropped from it: a
    filtered list is a short list, and a short list with no announcement is the
    outcome this module refuses.
    """
    from app.state.queries import _require_scope, passage_index_coverage, passage_index_hits

    _require_scope(company_id)

    terms = match_terms(query)
    if not terms:
        # Not an empty result. "Nothing to rank on" and "nothing matched" are
        # different facts and a caller that folds them reports an empty corpus.
        return Retrieval(
            hits=(),
            terms=(),
            source=SOURCE_SCAN,
            reason=REASON_NO_TERMS,
            note=_NOTES[REASON_NO_TERMS],
            omitted=0,
        )

    bind = session.get_bind()
    if not _is_sqlite(bind):
        return _degraded(session, company_id, terms, limit, REASON_INDEX_UNAVAILABLE)
    if not index_present(session):
        return _degraded(session, company_id, terms, limit, REASON_INDEX_MISSING)

    indexed, total = passage_index_coverage(session, company_id, scheme=SCHEME)
    if indexed != total:
        return _degraded(session, company_id, terms, limit, REASON_INDEX_STALE)

    rows = passage_index_hits(
        session, company_id, expression=match_expression(terms), limit=limit + 1
    )

    hits: list[PassageHit] = []
    for row in rows[:limit]:
        expected = fingerprint(
            row["version_id"],
            row["ordinal"],
            row["char_start"],
            row["char_end"],
            row["text"],
        )
        if expected != row["fingerprint"]:
            return _degraded(session, company_id, terms, limit, REASON_INDEX_STALE)
        hits.append(
            PassageHit(
                passage_id=row["passage_id"],
                version_id=row["version_id"],
                ordinal=row["ordinal"],
                section=row["section"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                text=row["text"],
                rank=row["score"],
            )
        )

    return Retrieval(
        hits=tuple(hits),
        terms=terms,
        source=SOURCE_INDEX,
        reason="",
        note="",
        # One row over the cap was asked for, so "there are more" is a fact
        # rather than a guess. The true remainder is not known and is not
        # claimed: a caller is told the list was cut, not how deep it went.
        omitted=max(0, len(rows) - limit),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "INDEX_TABLE",
    "LEDGER_TABLE",
    "REASON_INDEX_MISSING",
    "REASON_INDEX_STALE",
    "REASON_INDEX_UNAVAILABLE",
    "REASON_NO_TERMS",
    "SCHEME",
    "SOURCE_INDEX",
    "SOURCE_SCAN",
    "TOKENISER",
    "PassageHit",
    "Retrieval",
    "drop_index",
    "ensure_passage_index",
    "fingerprint",
    "index_body",
    "index_present",
    "match_expression",
    "match_terms",
    "rebuild_index",
    "search_passages",
]
