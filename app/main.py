"""FastAPI application. Thin by design: no business logic lives here.

The one thing this module decides is what the running product is made of: three
routers and one static mount, named once. `make run` starts this object and a
reviewer typing `uvicorn app.main:app` starts the same one, so a screen that
works in its own test file and a screen that works in the product cannot be two
different questions.

Each screen is still mountable on its own -- tests/test_screens.py and
tests/test_change_view.py build their own applications around the routers -- and
this file adds nothing they do not have. It only puts them together.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
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
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.views import changes, proceedings, review

app = FastAPI(title="Strata", docs_url=None, redoc_url=None)

# The stylesheet and the citation script. base.html links them at fixed absolute
# paths under STATIC_URL_PATH, so this mount point is not a free choice.
app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")

# Screens, in the order an analyst meets them. No prefixes: each router owns its
# own paths, and a prefix here would put the URLs in two files.
app.include_router(proceedings.router)
app.include_router(changes.router)
app.include_router(review.router)

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
