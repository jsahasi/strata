"""FastAPI application. Thin by design: no business logic lives here."""

from fastapi import FastAPI

app = FastAPI(title="Strata", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness plus a truthful statement about whether the corpus is loaded.

    Reports zero rather than pretending, so an empty database is visible
    instead of looking like a proceeding with no changes.
    """
    return {"status": "ok", "corpus_loaded": False, "versions": 0}
