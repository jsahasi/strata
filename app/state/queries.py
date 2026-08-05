"""Every tenant-scoped read goes through here, so there is one place to audit.

A read with no company_id is refused rather than answered. A query that returns
everything when the caller meant nothing is how tenant isolation fails in
practice, and it fails silently.

TWO GUARDS, AND THEY ANSWER DIFFERENT QUESTIONS.

_require_scope is the guard on the way IN. It reads the company_id the caller
supplied and refuses any value that could match more than the caller meant. It
knows nothing about rows.

row_for_company is the guard on the way OUT, and it exists because the first one
was mistaken for the second. Three call sites fetched a row by primary key with
session.get, and each was then supposed to remember to compare company_id
afterwards. Two of them did. app/state/routing.py::ensure_obligation did not: it
called _require_scope, which passed because the string was well formed, and then
handed back whatever row carried that id -- another tenant's included. Ids are
unique across tenants and the corpus supplies them, so the check that mattered
was the one nobody was forced to write. A rule three people have to remember is
a rule that fails on the fourth call site, so the fetch and the check are now
one call and there is nothing left to forget.
"""

from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.state.models import DocumentVersion, Passage

T = TypeVar("T")


class CrossTenantRow(ValueError):
    """A row was fetched by its id and it belongs to another company.

    A subclass of ValueError rather than a new exception hierarchy, so the call
    sites that already refuse with ValueError keep refusing with ValueError and
    every `except ValueError` around them still catches this. Callers that need
    to tell a cross-tenant fetch apart from an ordinary bad argument -- the audit
    chain does, because there the row is evidence of tampering rather than a
    typo -- catch this name instead.
    """


#: The characters a LIKE pattern reads as "any". A company_id carrying either is
#: refused, and the reason is not that any query here uses LIKE today -- none
#: does, and `==` treats "%" as the literal string it is. It is that the day
#: somebody writes a search, a report filter or a raw string comparison against
#: company_id, a "%" that had been travelling harmlessly through the guard turns
#: into every tenant at once, and nothing would fail loudly at the moment it
#: happened. The guard is the cheapest place to make that impossible, and the
#: existing isolation tests could not tell whether it was there at all: they
#: accepted an empty list as proof, which is exactly what a DELETED guard also
#: produces. Both halves are fixed together; see tests/test_isolation.py.
SCOPE_WILDCARDS = ("%", "_")


def _require_scope(company_id: str) -> str:
    """The one definition of a usable company_id. Refuses; never narrows.

    Refused, and why each one:

      None, "" -- no scope at all. The caller meant a tenant and named none.
      "   " -- truthy, so the old `not company_id` let it through, and it then
        matched no row and returned []. An empty answer that means "your scope
        was broken" is indistinguishable from one that means "this tenant has
        nothing", and the second is a sentence somebody acts on.
      "%", "_" and anything containing them -- see SCOPE_WILDCARDS above.
      " MEP", "MEP " -- padding queries for a tenant that does not exist and
        answers [] for the same reason whitespace-only does. The value is
        refused rather than trimmed: trimming here would fix the guard's copy
        and leave every caller filtering on the padded original, which is a
        quieter version of the same bug.
      anything that is not a str -- 1, b"MEP", ["MEP"], True. A non-string
        compares unequal to every stored id and reports the whole tenant as
        empty.

    Returns the value unchanged so a caller may write `cid = _require_scope(x)`,
    but nothing is normalised. This function's job is to refuse, and a guard
    that silently rewrites its input is a second, hidden decision.
    """
    if company_id is None or not isinstance(company_id, str):
        raise ValueError(
            f"company_id must be a string naming one tenant; got {type(company_id).__name__}. "
            "Refusing an unscoped read."
        )
    if not company_id.strip():
        raise ValueError(
            "company_id is required; refusing an unscoped read. A blank scope "
            "matches nothing and reports it as an empty tenant."
        )
    if company_id.strip() != company_id:
        raise ValueError(
            f"company_id {company_id!r} is padded with whitespace. It matches no "
            "stored id, so the read would answer 'nothing here' rather than "
            "'your scope is wrong'."
        )
    for wildcard in SCOPE_WILDCARDS:
        if wildcard in company_id:
            raise ValueError(
                f"company_id {company_id!r} contains {wildcard!r}, which is a LIKE "
                "wildcard. Refusing it here rather than on the day a query uses "
                "LIKE and one scope becomes every tenant."
            )
    return company_id


_NO_COMPANY = object()


def row_for_company(
    session: Session, company_id: str, model: type[T], row_id: Any
) -> T | None:
    """Fetch one row by primary key and hand it back only to the company that owns it.

    THE CHOKEPOINT. Fetching by id and checking the tenant are one call, so the
    check cannot be forgotten, skipped in a hurry, or written three subtly
    different ways. session.get is kept underneath rather than a fresh query
    because the callers depend on the identity map: a row added earlier in the
    same session and not yet flushed still resolves, which is what makes the
    idempotent loaders idempotent.

    Three outcomes, and each is a different fact:

      the row is this company's -- it comes back;
      no row carries that id -- None, so a loader may create it;
      the row is another company's -- CrossTenantRow, and nothing comes back.

    THE THIRD OUTCOME RAISES RATHER THAN ANSWERING None, and that is deliberate.
    Returning None would collapse "nobody holds this id" into "somebody else
    does", and the two callers that create-if-absent would then try to write a
    row under a primary key another tenant already holds -- an IntegrityError at
    flush, arriving somewhere unrelated, or a silent second row on a schema
    without the constraint. Absence is denial: the caller is refused, told, and
    left to decide what to say.

    THE MESSAGE NAMES NOTHING ABOUT THE OTHER TENANT. Not the company, not the
    title, not a field. Confirming that some other company holds that id is the
    fact the scope exists to withhold -- app/state/rollback.py makes the same
    argument for the same reason -- so the refusal reads exactly as a missing
    row does.

    A model with no company_id raises TypeError rather than refusing quietly. Its
    tenancy lives on a parent row (Passage on its version, WorkflowStep on its
    workflow), and asking this function about one is a mistake at the call site.
    Answering None for ever would look like "no such row" and hide the fact that
    the check never ran.
    """
    _require_scope(company_id)
    if row_id is None:
        return None
    if isinstance(row_id, str) and not row_id.strip():
        return None

    row = session.get(model, row_id)
    if row is None:
        return None

    scope = getattr(row, "company_id", _NO_COMPANY)
    if scope is _NO_COMPANY:
        raise TypeError(
            f"{model.__name__} carries no company_id, so this cannot say whose "
            "row it is. Its tenancy is on the row above it; read it through a "
            "query that joins to whatever carries the scope."
        )
    if scope != company_id:
        raise CrossTenantRow(
            f"no {model.__name__.lower()} {row_id!r} on this company's record"
        )
    return row


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


def all_passages_for_company(session: Session, company_id: str) -> list[Passage]:
    """Every passage this company owns, across all its versions, in document order.

    The same join as passages_for_company and deliberately not a wrapper around
    it: that one answers "this version's passages" and would need the caller to
    enumerate versions first, which is a second place to forget the scope.

    ITS ONE CALLER IS THE RETRIEVAL FALLBACK in app/state/search.py, and it is a
    whole-corpus read per call. That cost is the point rather than an oversight:
    it is what a complete answer costs when the index cannot be trusted, and a
    complete slow answer beats a fast short one nobody was told was short.
    """
    _require_scope(company_id)
    return (
        session.query(Passage)
        .join(DocumentVersion, Passage.version_id == DocumentVersion.id)
        .filter(DocumentVersion.company_id == company_id)
        .order_by(Passage.version_id, Passage.ordinal)
        .all()
    )


# ---------------------------------------------------------------------------
# The passage index, read like every other tenant read
#
# An FTS5 virtual table is its own object and nothing above reaches inside it.
# These two are here rather than in app/state/search.py for exactly that reason:
# a read that crosses tenants is refused in one file, and an index with its own
# private query would be the second file -- the one nobody audited.
#
# THE TABLE NAMES ARE IMPORTED, NOT SPELLED. app/state/search.py owns what the
# index is called and how it is built; a second spelling here would survive a
# rename and query a table that no longer exists, or worse, one that does. The
# import is inside the functions because search.py imports this module, and a
# module-level import either way would be a cycle. app/verification/verifier.py
# defers an import from here for its own reason and says so; this is the same
# shape.
# ---------------------------------------------------------------------------


def passage_index_coverage(
    session: Session, company_id: str, *, scheme: str
) -> tuple[int, int]:
    """How many of this company's passages the index holds, and how many exist.

    SCOPED, THOUGH IT ONLY COUNTS. Measuring coverage across the whole estate
    would let one company's freshly ingested filing declare every other
    company's index stale, and every one of them would drop to the slow path
    over a corpus none of them owns.

    The scheme is part of the count. Rows written under an older tokenisation do
    not count as coverage, so changing the scheme name makes every index built
    under the old one read as absent -- which is what forces the whole thing to
    be rebuilt rather than mixed.
    """
    from app.state.search import LEDGER_TABLE

    _require_scope(company_id)
    total = session.execute(
        text(
            "SELECT count(*) FROM passages p "
            "JOIN document_versions v ON v.id = p.version_id "
            "WHERE v.company_id = :company_id"
        ),
        {"company_id": company_id},
    ).scalar_one()
    indexed = session.execute(
        text(
            f"SELECT count(*) FROM {LEDGER_TABLE} r "
            "JOIN passages p ON p.id = r.passage_id "
            "JOIN document_versions v ON v.id = p.version_id "
            "WHERE v.company_id = :company_id AND r.scheme = :scheme"
        ),
        {"company_id": company_id, "scheme": scheme},
    ).scalar_one()
    return indexed, total


def passage_index_hits(
    session: Session, company_id: str, *, expression: str, limit: int
) -> list[dict]:
    """Ranked passages matching an FTS5 expression, for one company and no other.

    THE JOIN IS THE SCOPE. The index knows only rowids, so the company filter
    can only be applied by walking passages to document_versions -- the same
    walk passages_for_company makes, for the same reason. Take the join out and
    a query returns every tenant's paragraphs that use the words, which is a
    cross-tenant read of source text and the single worst failure this product
    has available to it.

    company_id and the expression both arrive as BOUND PARAMETERS. The table
    names are interpolated and they are module constants, not anything a caller
    supplied; nothing a person or a model typed is ever part of the statement
    text. The expression is built by app/state/search.py::match_expression,
    which quotes every term into a phrase, so FTS5's own operators cannot be
    reached from a question either.

    bm25 is aliased to `score` rather than ordered by the name `rank`, because
    `rank` is a hidden column of an FTS5 table and ordering by it means
    something else. Lower is better, which is SQLite's convention.
    """
    from app.state.search import INDEX_TABLE, LEDGER_TABLE, SCHEME

    _require_scope(company_id)
    rows = session.execute(
        text(
            "SELECT p.id AS passage_id, p.version_id AS version_id, "
            "p.ordinal AS ordinal, p.char_start AS char_start, "
            "p.char_end AS char_end, p.text AS text, p.section AS section, "
            f"r.fingerprint AS fingerprint, bm25({INDEX_TABLE}) AS score "
            f"FROM {INDEX_TABLE} "
            f"JOIN {LEDGER_TABLE} r ON r.passage_id = {INDEX_TABLE}.rowid "
            "JOIN passages p ON p.id = r.passage_id "
            "JOIN document_versions v ON v.id = p.version_id "
            f"WHERE {INDEX_TABLE} MATCH :expression "
            "AND v.company_id = :company_id AND r.scheme = :scheme "
            "ORDER BY score LIMIT :limit"
        ),
        {
            "expression": expression,
            "company_id": company_id,
            "scheme": SCHEME,
            "limit": limit,
        },
    ).mappings().all()
    return [dict(row) for row in rows]
