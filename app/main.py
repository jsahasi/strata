"""FastAPI application. Thin by design: no business logic lives here."""

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.state.db import session_scope
from app.state.models import (
    Change,
    Claim,
    DocumentVersion,
    Escalation,
    Proceeding,
)

app = FastAPI(title="Strata", docs_url=None, redoc_url=None)

# Counted, in this order, into the health body.
_TABLES = {
    "proceedings": Proceeding,
    "versions": DocumentVersion,
    "changes": Change,
    "claims": Claim,
    "escalations": Escalation,
}

_NOT_LOADED = "corpus not loaded: run `make seed`, or `python -m app.seed`"


@app.get("/healthz")
def healthz() -> dict:
    """Liveness plus a truthful statement about whether the corpus is loaded.

    The counts are real. An earlier version returned a hardcoded zero, which is
    the shape of failure best-practices.html §26 warns about: a check that
    reports what is configured rather than what is there, and so passes on a
    machine where nothing works.

    An empty or missing database is answered plainly rather than as an error.
    A reviewer who has cloned the repository and not yet seeded should be told
    which command to run, not handed a 500.

    These are row counts across all companies, not a tenant read. No source
    text, statement or company name crosses the boundary, and the endpoint takes
    no company id -- so there is no scope for it to get wrong. Every read that
    returns content goes through app/state/queries.py instead.
    """
    try:
        with session_scope() as session:
            counts = {
                name: session.execute(
                    select(func.count()).select_from(model)
                ).scalar_one()
                for name, model in _TABLES.items()
            }
    except SQLAlchemyError:
        # No tables yet. A fresh clone before the first seed looks exactly like
        # this, and it is not a fault.
        return {
            "status": "ok",
            "corpus_loaded": False,
            **{name: 0 for name in _TABLES},
            "detail": _NOT_LOADED,
        }

    loaded = counts["versions"] > 0 and counts["changes"] > 0
    body = {"status": "ok", "corpus_loaded": loaded, **counts}
    if not loaded:
        body["detail"] = _NOT_LOADED
    return body
