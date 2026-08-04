# Strata Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic core of Strata — ingestion with stable offsets, a citation verifier that refuses to assert an unverified claim, and a version diff — with `make run` and `make test` green on a fresh clone.

**Architecture:** A pipeline of small deterministic stages, each a pure function or a thin store access, with typed objects crossing every boundary. No model is called anywhere in this plan. That is deliberate: the modules a reviewer audits to decide whether to trust Strata are exactly the modules that need no API key and no network.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 over SQLite, pytest. No Node, no build step, no external service.

## Global Constraints

- Python 3.12. No dependency may be added beyond those already in `requirements.txt`.
- Tests run offline. No network call in any test in this plan. No `ANTHROPIC_API_KEY` required.
- `make run` and `make test` must work from a fresh clone after every task. `make fresh-check` is the proof.
- Offsets are zero-based Python string indices into UTF-8-decoded text, **end-exclusive**: `text[start:end] == exact_text`.
- Source text is stored exactly as ingested. Ingestion never trims, rewrites or re-encodes. Normalization happens only at comparison time, on both sides.
- Absence is denial. Any failure to verify degrades to escalation, never to a confident guess.
- Prose in code comments and docstrings follows the repo's Orwell rules: short words, active voice. No emoji.
- Corpus ground truth is `data/manifest.json`. Tests assert against it, never against an impression.

---

### Task 1: Skeleton — the two commands a reviewer runs

The Makefile already calls `app.main:app`, `app.seed` and `app.evals.run`. None exist. Until they do, nothing else in this repo can be evaluated.

**Files:**
- Create: `app/__init__.py`, `app/main.py`, `app/seed.py`, `app/evals/__init__.py`, `app/evals/run.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.main.app` (a `fastapi.FastAPI` instance) and `GET /healthz` returning `{"status": "ok", "corpus_loaded": bool, "versions": int}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_smoke.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


def test_healthz_reports_status_and_corpus_state():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "corpus_loaded" in body
    assert isinstance(body["versions"], int)
```

Create `tests/__init__.py` (empty) and `tests/conftest.py`.

**This file must set the database URL before anything under `app.` is imported.**
`app/state/db.py` builds its engine at import time from the environment, and `init_db()`
drops tables. Without this, running `make test` would destroy the developer's `strata.db`
on every run — a test suite that damages the thing it is testing.

```python
"""Test configuration. Read the note on ordering before editing.

The first two statements run before any `app.` import, on purpose: app.state.db
reads STRATA_DATABASE_URL at import time and init_db() drops tables. Point the
suite at a scratch file and the developer's strata.db is never touched.
"""

import os
import tempfile
from pathlib import Path

os.environ["STRATA_DATABASE_URL"] = (
    "sqlite:///" + str(Path(tempfile.gettempdir()) / "strata-test.db")
)

import pytest  # noqa: E402


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(repo_root: Path) -> Path:
    return repo_root / "data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make test`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Write minimal implementation**

Create `app/__init__.py` (empty). Create `app/main.py`:

```python
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
```

Create `app/seed.py`:

```python
"""Load the synthetic corpus. Idempotent: safe to run on every `make run`."""


def main() -> None:
    print("seed: nothing to load yet (ingestion lands in Task 3)")


if __name__ == "__main__":
    main()
```

Create `app/evals/__init__.py` (empty) and `app/evals/run.py`:

```python
"""Eval harness. Prints scores rather than adjectives."""


def main() -> None:
    print("eval: no evals defined yet")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test`
Expected: PASS, 1 passed

Run: `make fresh-check`
Expected: `fresh clone: tests pass at /tmp/...`

- [ ] **Step 5: Commit**

```bash
git add app/ tests/
git commit -m "Skeleton: make run and make test work before any feature does"
```

---

### Task 2: Normalization — the function both sides of every comparison call

A citation comparison is only meaningful if both sides were folded the same way. One function, called by the verifier on the stored text and on the claimed quote alike.

**Files:**
- Create: `app/text/__init__.py`, `app/text/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.text.normalize.normalize(text: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_normalize.py`:

```python
from app.text.normalize import normalize


def test_folds_smart_quotes_to_ascii():
    assert normalize("“Large Load Customer”") == '"Large Load Customer"'
    assert normalize("the Utility’s system") == "the Utility's system"


def test_collapses_internal_whitespace_and_strips_ends():
    assert normalize("  shall   allocate\n\n100%  ") == "shall allocate 100%"


def test_folds_ligatures_and_nonbreaking_space():
    assert normalize("the ﬁrst oﬀer") == "the first offer"
    assert normalize("20 MW") == "20 MW"


def test_folds_dash_variants_to_hyphen():
    assert normalize("2026–2027") == "2026-2027"
    assert normalize("cost — causation") == "cost - causation"


def test_is_idempotent():
    once = normalize("  “Requested  Load” — 20 MW ")
    assert normalize(once) == once


def test_preserves_meaning_bearing_difference():
    # 20 MW and 10 MW must never normalize to the same string. If they did,
    # the material threshold change in the corpus would verify against the
    # wrong version and the product's core claim would be false.
    assert normalize("20 megawatts (MW)") != normalize("10 megawatts (MW)")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.text'`

- [ ] **Step 3: Write minimal implementation**

Create `app/text/__init__.py` (empty). Create `app/text/normalize.py`:

```python
"""The single normalization used on both sides of every citation comparison.

Two rules govern what belongs here. Fold a difference only when a regulator,
a PDF extractor or a word processor could have introduced it without anyone
intending a change of meaning. Never fold a difference that could carry
meaning: digits, units, dates and case are all left alone, because "20 MW"
and "10 MW" are the whole point.
"""

import re
import unicodedata

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}

_DASHES = {
    "‐": "-", "‑": "-", "‒": "-",
    "–": "-", "—": "-", "―": "-", "−": "-",
}

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold the differences that must not decide whether a citation verifies."""
    if text is None:
        raise TypeError("normalize() requires a string, not None")

    # NFKC folds ligatures (fi -> fi) and non-breaking space to space.
    folded = unicodedata.normalize("NFKC", text)

    folded = "".join(_QUOTES.get(ch, ch) for ch in folded)
    folded = "".join(_DASHES.get(ch, ch) for ch in folded)

    return _WHITESPACE.sub(" ", folded).strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/text/ tests/test_normalize.py
git commit -m "Normalization: one function, called on both sides of every comparison"
```

---

### Task 3: Ingestion — hashed source, offset-addressed passages

**Files:**
- Create: `app/state/__init__.py`, `app/state/models.py`, `app/state/db.py`
- Create: `app/ingestion/__init__.py`, `app/ingestion/ingest.py`
- Test: `tests/test_ingestion.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `app.state.db.session_scope()` — context manager yielding a `Session`.
  - `app.state.db.init_db(engine=None) -> None`.
  - `app.state.models.DocumentVersion` with columns `id (str, pk)`, `company_id (str)`, `docket (str)`, `label (str)`, `status (str)`, `source_text (str)`, `source_sha256 (str)`.
  - `app.state.models.Passage` with columns `id (int, pk)`, `version_id (str, fk)`, `ordinal (int)`, `char_start (int)`, `char_end (int)`, `text (str)`, `section (str | None)`.
  - `app.ingestion.ingest.ingest_version(session, *, version_id, company_id, docket, label, status, source_text) -> DocumentVersion`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingestion.py`:

```python
import hashlib
import json
from pathlib import Path

from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.models import Passage

DATA = Path(__file__).resolve().parent.parent / "data"


def _manifest():
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def test_every_passage_offset_addresses_its_own_text_exactly():
    init_db()
    text = _v1_text()
    with session_scope() as session:
        ingest_version(
            session,
            version_id="v1",
            company_id="MEP",
            docket="MPUC-2026-0142",
            label="Notice of Proposed Rulemaking",
            status="DRAFT",
            source_text=text,
        )
        passages = session.query(Passage).filter_by(version_id="v1").all()
        assert len(passages) > 10
        for passage in passages:
            assert text[passage.char_start:passage.char_end] == passage.text


def test_source_sha_matches_the_bytes_ingested():
    init_db()
    text = _v1_text()
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with session_scope() as session:
        version = ingest_version(
            session,
            version_id="v1",
            company_id="MEP",
            docket="MPUC-2026-0142",
            label="Notice of Proposed Rulemaking",
            status="DRAFT",
            source_text=text,
        )
        assert version.source_sha256 == expected


def test_source_text_is_stored_byte_for_byte_unmodified():
    init_db()
    text = _v1_text()
    with session_scope() as session:
        version = ingest_version(
            session,
            version_id="v1",
            company_id="MEP",
            docket="MPUC-2026-0142",
            label="Notice of Proposed Rulemaking",
            status="DRAFT",
            source_text=text,
        )
        assert version.source_text == text


def test_manifest_offsets_resolve_against_the_stored_source():
    # The manifest's offsets were computed independently of this code.
    # If ingestion ever rewrites the source, this test fails loudly.
    init_db()
    text = _v1_text()
    manifest = _manifest()
    with session_scope() as session:
        version = ingest_version(
            session,
            version_id="v1",
            company_id="MEP",
            docket="MPUC-2026-0142",
            label="Notice of Proposed Rulemaking",
            status="DRAFT",
            source_text=text,
        )
        for change in manifest["changes"]:
            before = change["before"]
            if before.get("version") != "v1":
                continue
            span = version.source_text[before["start"]:before["end"]]
            assert span == before["exact_text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingestion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.state'`

- [ ] **Step 3: Write minimal implementation**

Create `app/state/__init__.py` (empty). Create `app/state/models.py`:

```python
"""SQLAlchemy models. company_id is on every tenant-scoped row by design."""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    docket: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(256))
    # DRAFT or FINAL. Explicit per ADR-005; never inferred at read time.
    status: Mapped[str] = mapped_column(String(16))
    source_text: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))


class Passage(Base):
    __tablename__ = "passages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)


Index("ix_passages_version_ordinal", Passage.version_id, Passage.ordinal)
```

Create `app/state/db.py`:

```python
"""Engine and session handling. SQLite by default; the URL is the seam to Postgres."""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import Base

DATABASE_URL = os.environ.get("STRATA_DATABASE_URL", "sqlite:///strata.db")

_engine = create_engine(DATABASE_URL, future=True)
_SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)


def get_engine():
    return _engine


def init_db(engine=None) -> None:
    """Create tables. Drops first when running against an in-memory test database."""
    target = engine or _engine
    Base.metadata.drop_all(target)
    Base.metadata.create_all(target)


@contextmanager
def session_scope() -> Session:
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

Create `app/ingestion/__init__.py` (empty). Create `app/ingestion/ingest.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingestion.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/state/ app/ingestion/ tests/test_ingestion.py
git commit -m "Ingestion: hash the source, address every passage by offset, change nothing"
```

---

### Task 4: The citation verifier — the module the product rests on

**Files:**
- Create: `app/verification/__init__.py`, `app/verification/verifier.py`
- Test: `tests/test_verification.py`

**Interfaces:**
- Consumes: `app.text.normalize.normalize`, `app.state.models.DocumentVersion`.
- Produces:
  - `app.verification.verifier.Citation` — frozen dataclass with `version_id: str`, `char_start: int`, `char_end: int`, `quoted_text: str`.
  - `app.verification.verifier.VerificationResult` — frozen dataclass with `verified: bool`, `reason: str | None`, `actual_text: str | None`.
  - `app.verification.verifier.verify_citation(citation: Citation, source_text: str) -> VerificationResult`.
  - Reason constants: `REASON_OUT_OF_RANGE`, `REASON_EMPTY_SPAN`, `REASON_QUOTE_MISMATCH`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verification.py`:

```python
import json
from pathlib import Path

from app.verification.verifier import (
    REASON_EMPTY_SPAN,
    REASON_OUT_OF_RANGE,
    REASON_QUOTE_MISMATCH,
    Citation,
    verify_citation,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def _manifest():
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def test_verifies_a_true_citation_from_the_manifest():
    text = _v1_text()
    # CHG-2 before: the Large Load Customer definition in v1.
    citation = Citation(
        version_id="v1",
        char_start=1970,
        char_end=2066,
        quoted_text='"Large Load Customer" means a Customer whose Requested Load equals or exceeds 20 megawatts (MW).',
    )
    result = verify_citation(citation, text)
    assert result.verified is True
    assert result.reason is None


def test_rejects_a_fabricated_quote_at_real_offsets():
    # The single most persuasive test in the submission: correct offsets,
    # invented words. A model that misquotes fluently is caught here.
    text = _v1_text()
    citation = Citation(
        version_id="v1",
        char_start=1970,
        char_end=2066,
        quoted_text='"Large Load Customer" means a Customer whose Requested Load equals or exceeds 10 megawatts (MW).',
    )
    result = verify_citation(citation, text)
    assert result.verified is False
    assert result.reason == REASON_QUOTE_MISMATCH
    assert "20 megawatts" in result.actual_text


def test_rejects_offsets_past_the_end_of_the_source():
    text = _v1_text()
    citation = Citation(
        version_id="v1",
        char_start=len(text) - 5,
        char_end=len(text) + 500,
        quoted_text="anything",
    )
    result = verify_citation(citation, text)
    assert result.verified is False
    assert result.reason == REASON_OUT_OF_RANGE


def test_rejects_negative_and_inverted_offsets():
    text = _v1_text()
    assert verify_citation(Citation("v1", -1, 20, "x"), text).reason == REASON_OUT_OF_RANGE
    assert verify_citation(Citation("v1", 200, 100, "x"), text).reason == REASON_OUT_OF_RANGE


def test_rejects_an_empty_span():
    text = _v1_text()
    result = verify_citation(Citation("v1", 1970, 1970, ""), text)
    assert result.verified is False
    assert result.reason == REASON_EMPTY_SPAN


def test_accepts_a_quote_differing_only_in_normalizable_ways():
    text = _v1_text()
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-2")
    exact = change["before"]["exact_text"]
    noisy = "  " + exact.replace('"', "“", 1).replace('"', "”", 1) + "\n"
    result = verify_citation(
        Citation("v1", change["before"]["start"], change["before"]["end"], noisy), text
    )
    assert result.verified is True


def test_every_manifest_change_verifies_at_its_recorded_offsets():
    manifest = _manifest()
    texts = {
        "v1": (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8"),
        "v2": (DATA / "v2_revised_proposed_rule.txt").read_bytes().decode("utf-8"),
        "v3": (DATA / "v3_final_order.txt").read_bytes().decode("utf-8"),
    }
    checked = 0
    for change in manifest["changes"]:
        for side in ("before", "after"):
            entry = change[side]
            if not entry.get("version"):
                continue
            result = verify_citation(
                Citation(
                    entry["version"], entry["start"], entry["end"], entry["exact_text"]
                ),
                texts[entry["version"]],
            )
            assert result.verified is True, f"{change['id']} {side} failed to verify"
            checked += 1
    assert checked >= 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.verification'`

- [ ] **Step 3: Write minimal implementation**

Create `app/verification/__init__.py` (empty). Create `app/verification/verifier.py`:

```python
"""The gate. Nothing becomes fact without passing through here.

This module calls no model and makes no network request, on purpose. The code
that decides whether the product may assert something must be auditable by a
reviewer who trusts nothing about the AI, and must run in CI with no API key.

It compares for equality after normalization, not for similarity. A paraphrase
is exactly what an auditor will not accept, so a threshold would defeat the
point of having the gate at all.
"""

from dataclasses import dataclass

from app.text.normalize import normalize

REASON_OUT_OF_RANGE = "citation offsets fall outside the source text"
REASON_EMPTY_SPAN = "citation span is empty"
REASON_QUOTE_MISMATCH = "quoted text does not match the source at the cited offsets"


@dataclass(frozen=True)
class Citation:
    version_id: str
    char_start: int
    char_end: int
    quoted_text: str


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: str | None = None
    actual_text: str | None = None


def verify_citation(citation: Citation, source_text: str) -> VerificationResult:
    """Re-read the source at the cited offsets and confirm the quote matches."""
    start, end = citation.char_start, citation.char_end

    if start < 0 or end < 0 or end > len(source_text) or start > end:
        return VerificationResult(False, REASON_OUT_OF_RANGE, None)

    actual = source_text[start:end]

    if not normalize(actual) or not normalize(citation.quoted_text):
        return VerificationResult(False, REASON_EMPTY_SPAN, actual)

    if normalize(actual) != normalize(citation.quoted_text):
        return VerificationResult(False, REASON_QUOTE_MISMATCH, actual)

    return VerificationResult(True, None, actual)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_verification.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/verification/ tests/test_verification.py
git commit -m "Verifier: exact match after normalization, or it does not become fact"
```

---

### Task 5: Occurrence identity — where the verifier alone is not enough

The corpus contains one sentence appearing three times per version, in three sections, about three different subjects. A claim can quote it correctly, cite real offsets, verify — and still describe the wrong occurrence. This task makes that catchable, and is the honest answer to "where does your approach fail?"

**Files:**
- Modify: `app/verification/verifier.py`
- Test: `tests/test_occurrence.py`

**Interfaces:**
- Consumes: `Citation`, `VerificationResult` from Task 4.
- Produces:
  - `app.verification.verifier.REASON_AMBIGUOUS_OCCURRENCE`.
  - `app.verification.verifier.occurrence_index(citation: Citation, source_text: str) -> int` — zero-based index of the cited span among all occurrences of that text.
  - `app.verification.verifier.occurrence_count(quoted_text: str, source_text: str) -> int`.
  - `verify_citation(..., expected_occurrence: int | None = None)` — when the quote appears more than once and `expected_occurrence` is `None`, the result is unverified with `REASON_AMBIGUOUS_OCCURRENCE`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_occurrence.py`:

```python
import json
from pathlib import Path

from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    Citation,
    occurrence_count,
    occurrence_index,
    verify_citation,
)

DATA = Path(__file__).resolve().parent.parent / "data"
BOILERPLATE = (
    "The Utility shall maintain records sufficient to demonstrate compliance "
    "with this Order for a period of not less than five (5) years."
)


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def test_the_trap_sentence_occurs_exactly_three_times_in_v1():
    assert occurrence_count(BOILERPLATE, _v1_text()) == 3


def test_each_recorded_occurrence_reports_its_own_index():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    v1_spans = [
        o for o in manifest["repeated_boilerplate"]["occurrences"] if o["version"] == "v1"
    ]
    text = _v1_text()
    indices = [
        occurrence_index(Citation("v1", o["start"], o["end"], BOILERPLATE), text)
        for o in sorted(v1_spans, key=lambda o: o["start"])
    ]
    assert indices == [0, 1, 2]


def test_a_repeated_quote_without_a_stated_occurrence_is_not_verified():
    # Textually perfect, substantively ambiguous. The product must not assert it.
    text = _v1_text()
    result = verify_citation(Citation("v1", 4930, 5063, BOILERPLATE), text)
    assert result.verified is False
    assert result.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_stating_the_correct_occurrence_verifies():
    text = _v1_text()
    result = verify_citation(
        Citation("v1", 4930, 5063, BOILERPLATE), text, expected_occurrence=0
    )
    assert result.verified is True


def test_stating_the_wrong_occurrence_is_rejected():
    # Section 4.4 offsets, claimed as the Section 7.3 recordkeeping occurrence.
    text = _v1_text()
    result = verify_citation(
        Citation("v1", 4930, 5063, BOILERPLATE), text, expected_occurrence=2
    )
    assert result.verified is False
    assert result.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_a_unique_quote_needs_no_occurrence_and_still_verifies():
    text = _v1_text()
    result = verify_citation(
        Citation(
            "v1",
            1970,
            2066,
            '"Large Load Customer" means a Customer whose Requested Load equals or exceeds 20 megawatts (MW).',
        ),
        text,
    )
    assert result.verified is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_occurrence.py -v`
Expected: FAIL — `ImportError: cannot import name 'REASON_AMBIGUOUS_OCCURRENCE'`

- [ ] **Step 3: Write minimal implementation**

In `app/verification/verifier.py`, add the constant beside the others:

```python
REASON_AMBIGUOUS_OCCURRENCE = (
    "quoted text appears more than once and the cited occurrence was not stated "
    "or does not match"
)
```

Add these two functions after the dataclasses:

```python
def _spans_of(quoted_text: str, source_text: str) -> list[tuple[int, int]]:
    """Every span whose normalized text equals the normalized quote.

    Scans candidate spans by raw substring search first, which is exact for
    this corpus. Normalization then decides equality, so the two paths agree.
    """
    needle = normalize(quoted_text)
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = source_text.find(quoted_text)
    while start != -1:
        spans.append((start, start + len(quoted_text)))
        start = source_text.find(quoted_text, start + 1)
    if spans:
        return spans
    # Fall back to a normalized scan for quotes that differ only in whitespace.
    width = len(quoted_text)
    for index in range(0, max(0, len(source_text) - width + 1)):
        window = source_text[index:index + width]
        if normalize(window) == needle:
            spans.append((index, index + width))
    return spans


def occurrence_count(quoted_text: str, source_text: str) -> int:
    """How many times this text appears in the source."""
    return len(_spans_of(quoted_text, source_text))


def occurrence_index(citation: Citation, source_text: str) -> int:
    """Zero-based index of the cited span among all occurrences. -1 if absent."""
    spans = _spans_of(citation.quoted_text, source_text)
    for index, (start, _end) in enumerate(spans):
        if start == citation.char_start:
            return index
    return -1
```

Replace the body of `verify_citation` with this version, which adds the occurrence check as the final gate:

```python
def verify_citation(
    citation: Citation,
    source_text: str,
    expected_occurrence: int | None = None,
) -> VerificationResult:
    """Re-read the source at the cited offsets and confirm the quote matches.

    Text equality is necessary and not sufficient. When a quote appears more
    than once, the same words in a different section mean a different thing, so
    the claim must also state which occurrence it relies on.
    """
    start, end = citation.char_start, citation.char_end

    if start < 0 or end < 0 or end > len(source_text) or start > end:
        return VerificationResult(False, REASON_OUT_OF_RANGE, None)

    actual = source_text[start:end]

    if not normalize(actual) or not normalize(citation.quoted_text):
        return VerificationResult(False, REASON_EMPTY_SPAN, actual)

    if normalize(actual) != normalize(citation.quoted_text):
        return VerificationResult(False, REASON_QUOTE_MISMATCH, actual)

    if occurrence_count(citation.quoted_text, source_text) > 1:
        found = occurrence_index(citation, source_text)
        if expected_occurrence is None or expected_occurrence != found:
            return VerificationResult(False, REASON_AMBIGUOUS_OCCURRENCE, actual)

    return VerificationResult(True, None, actual)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_occurrence.py tests/test_verification.py -v`
Expected: PASS, 13 passed. Both files must pass — the Task 4 tests use unique quotes and are unaffected.

- [ ] **Step 5: Commit**

```bash
git add app/verification/verifier.py tests/test_occurrence.py
git commit -m "Occurrence identity: the same sentence in a different section is a different claim"
```

---

### Task 6: The deterministic diff

**Files:**
- Create: `app/diff/__init__.py`, `app/diff/engine.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `app.text.normalize.normalize`.
- Produces:
  - `app.diff.engine.PassageRef` — frozen dataclass with `version_id: str`, `char_start: int`, `char_end: int`, `text: str`.
  - `app.diff.engine.Change` — frozen dataclass with `change_type: str` (`"added"`, `"removed"`, `"modified"`), `before: PassageRef | None`, `after: PassageRef | None`, `alignment_confidence: float`.
  - `app.diff.engine.diff(before: list[PassageRef], after: list[PassageRef]) -> list[Change]`.
  - `app.diff.engine.passage_refs(session, version_id) -> list[PassageRef]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_diff.py`:

```python
import json
from pathlib import Path

from app.diff.engine import Change, PassageRef, diff

DATA = Path(__file__).resolve().parent.parent / "data"


def _manifest():
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def _refs(version_id: str, chunks: list[tuple[int, str]]) -> list[PassageRef]:
    return [PassageRef(version_id, s, s + len(t), t) for s, t in chunks]


def test_identical_inputs_produce_no_changes():
    refs = _refs("v1", [(0, "alpha"), (10, "beta")])
    other = _refs("v2", [(0, "alpha"), (10, "beta")])
    assert diff(refs, other) == []


def test_is_deterministic_across_repeated_calls():
    before = _refs("v1", [(0, "alpha"), (10, "beta")])
    after = _refs("v2", [(0, "alpha"), (10, "gamma")])
    assert diff(before, after) == diff(before, after)


def test_finds_a_modified_passage_with_refs_on_both_sides():
    before = _refs("v1", [(0, "the threshold is 20 megawatts")])
    after = _refs("v2", [(0, "the threshold is 10 megawatts")])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == "modified"
    assert changes[0].before.text == "the threshold is 20 megawatts"
    assert changes[0].after.text == "the threshold is 10 megawatts"


def test_finds_an_addition_present_only_on_the_after_side():
    before = _refs("v1", [(0, "alpha")])
    after = _refs("v2", [(0, "alpha"), (10, "a wholly new obligation")])
    changes = diff(before, after)
    assert [c.change_type for c in changes] == ["added"]
    assert changes[0].before is None
    assert changes[0].after.text == "a wholly new obligation"


def test_finds_a_removal_present_only_on_the_before_side():
    before = _refs("v1", [(0, "alpha"), (10, "struck provision")])
    after = _refs("v2", [(0, "alpha")])
    changes = diff(before, after)
    assert [c.change_type for c in changes] == ["removed"]
    assert changes[0].after is None


def test_ignores_a_difference_that_normalization_folds():
    before = _refs("v1", [(0, "the  Utility’s system")])
    after = _refs("v2", [(0, "the Utility's system")])
    assert diff(before, after) == []


def test_detects_the_corpus_deadline_move_at_sub_sentence_granularity():
    # CHG-3: one date token moves inside an otherwise identical sentence.
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-3")
    before = _refs("v1", [(change["before"]["start"], change["before"]["exact_text"])])
    after = _refs("v2", [(change["after"]["start"], change["after"]["exact_text"])])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == "modified"
    assert "March 1, 2027" in changes[0].before.text
    assert "June 1, 2027" in changes[0].after.text


def test_wholesale_restructure_reports_low_alignment_confidence():
    # CHG-5: Section 6 becomes Section 5.4. Naive alignment sees delete+add.
    # The design does not claim to solve this; it must flag it rather than guess.
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-5")
    before = _refs("v2", [(change["before"]["start"], change["before"]["exact_text"])])
    after = _refs("v3", [(change["after"]["start"], change["after"]["exact_text"])])
    changes = diff(before, after)
    assert changes, "restructure must produce at least one change, never silence"
    assert any(c.alignment_confidence < 0.9 for c in changes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_diff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.diff'`

- [ ] **Step 3: Write minimal implementation**

Create `app/diff/__init__.py` (empty). Create `app/diff/engine.py`:

```python
"""Pure function: two passage sequences in, typed changes out.

No model, no network, no clock, no randomness. Given the same two sequences it
returns the same list every time, which is what lets the corpus's deliberate
edits be asserted exactly rather than approximately.

The known weakness is wholesale restructuring: when a section is renumbered and
relocated, sequence alignment sees a deletion and an addition. The design does
not pretend otherwise. It reports alignment confidence so a low-confidence
alignment escalates instead of being presented as settled.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.text.normalize import normalize


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
                    Change("modified", old, new, _similarity(old.text, new.text))
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


def passage_refs(session, version_id: str) -> list[PassageRef]:
    """Read a version's passages out of the store, in document order."""
    from app.state.models import Passage

    rows = (
        session.query(Passage)
        .filter_by(version_id=version_id)
        .order_by(Passage.ordinal)
        .all()
    )
    return [
        PassageRef(version_id, row.char_start, row.char_end, row.text) for row in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_diff.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add app/diff/ tests/test_diff.py
git commit -m "Diff: deterministic, testable, and honest about restructuring"
```

---

### Task 7: Draft versus final, dispatched before anything else runs

**Files:**
- Create: `app/interpretation/__init__.py`, `app/interpretation/action.py`
- Test: `tests/test_draft_final.py`

**Interfaces:**
- Consumes: `app.diff.engine.Change`, `app.state.models.DocumentVersion`.
- Produces:
  - `app.interpretation.action.ACTION_MONITOR = "monitor"`, `ACTION_COMMENT = "comment"`, `ACTION_COMPLY = "comply"`.
  - `app.interpretation.action.action_vocabulary(status: str) -> tuple[str, ...]`.
  - `app.interpretation.action.requires_effective_date(status: str) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_draft_final.py`:

```python
import pytest

from app.interpretation.action import (
    ACTION_COMMENT,
    ACTION_COMPLY,
    ACTION_MONITOR,
    action_vocabulary,
    requires_effective_date,
)


def test_draft_offers_only_monitor_and_comment():
    assert action_vocabulary("DRAFT") == (ACTION_MONITOR, ACTION_COMMENT)


def test_final_offers_only_comply():
    assert action_vocabulary("FINAL") == (ACTION_COMPLY,)


def test_the_vocabularies_do_not_overlap():
    # Acting on a draft wastes money on something that may not survive comment.
    # Treating a final order as a draft misses a binding deadline. Neither word
    # may appear in both lists, or the two paths have started to converge.
    assert not set(action_vocabulary("DRAFT")) & set(action_vocabulary("FINAL"))


def test_only_final_requires_an_effective_date():
    assert requires_effective_date("FINAL") is True
    assert requires_effective_date("DRAFT") is False


def test_an_unknown_status_raises_rather_than_defaulting():
    # Defaulting would pick a branch silently, which is the exact error with
    # the highest cost in this domain.
    with pytest.raises(ValueError):
        action_vocabulary("PROPOSED")
    with pytest.raises(ValueError):
        requires_effective_date("")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_draft_final.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.interpretation'`

- [ ] **Step 3: Write minimal implementation**

Create `app/interpretation/__init__.py` (empty). Create `app/interpretation/action.py`:

```python
"""Draft and final are separate code paths, decided before any model runs.

Per ADR-005 the status is read from the version's explicit field and used to
dispatch. The model is never asked which branch it is in, because that is the
distinction an analyst checks first when deciding whether to trust the tool.
"""

ACTION_MONITOR = "monitor"
ACTION_COMMENT = "comment"
ACTION_COMPLY = "comply"

_VOCABULARY = {
    "DRAFT": (ACTION_MONITOR, ACTION_COMMENT),
    "FINAL": (ACTION_COMPLY,),
}


def _checked(status: str) -> str:
    if status not in _VOCABULARY:
        raise ValueError(
            f"unknown version status {status!r}; expected DRAFT or FINAL. "
            "Refusing to default, because guessing this wrong is the most "
            "expensive error available in this domain."
        )
    return status


def action_vocabulary(status: str) -> tuple[str, ...]:
    """The only actions a change at this status may produce."""
    return _VOCABULARY[_checked(status)]


def requires_effective_date(status: str) -> bool:
    """A final order binds from a date. A draft does not bind at all."""
    return _checked(status) == "FINAL"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_draft_final.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/interpretation/ tests/test_draft_final.py
git commit -m "Draft versus final: two vocabularies, no shared path, no default"
```

---

### Task 8: Tenant isolation, proved by test rather than by inspection

`security.html` promises `company_id` as an isolation chokepoint. Until a test asserts it, that promise is a design on paper.

**Files:**
- Create: `app/state/queries.py`
- Test: `tests/test_isolation.py`

**Interfaces:**
- Consumes: `app.state.models.DocumentVersion`, `app.state.db.session_scope`, `app.ingestion.ingest.ingest_version`.
- Produces: `app.state.queries.versions_for_company(session, company_id: str) -> list[DocumentVersion]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_isolation.py`:

```python
from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.queries import versions_for_company


def _seed_two_companies(session):
    ingest_version(
        session,
        version_id="mep-v1",
        company_id="MEP",
        docket="MPUC-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text="MEP confidential load forecast for Monrovia.",
    )
    ingest_version(
        session,
        version_id="rival-v1",
        company_id="RIVAL",
        docket="OTHER-2026-0001",
        label="NOPR",
        status="DRAFT",
        source_text="RIVAL confidential load forecast for Springfield.",
    )


def test_a_company_read_returns_none_of_another_companys_rows():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()

        mep = versions_for_company(session, "MEP")
        assert [v.id for v in mep] == ["mep-v1"]
        assert all(v.company_id == "MEP" for v in mep)
        assert not any("RIVAL" in v.source_text for v in mep)

        rival = versions_for_company(session, "RIVAL")
        assert [v.id for v in rival] == ["rival-v1"]
        assert not any("MEP" in v.source_text for v in rival)


def test_an_unknown_company_sees_nothing_rather_than_everything():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        assert versions_for_company(session, "NOT-A-TENANT") == []


def test_an_empty_company_id_is_refused_not_treated_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_two_companies(session)
        session.flush()
        for value in ("", None, "%"):
            try:
                result = versions_for_company(session, value)
            except ValueError:
                continue
            assert result == [], f"{value!r} behaved as a wildcard"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_isolation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.state.queries'`

- [ ] **Step 3: Write minimal implementation**

Create `app/state/queries.py`:

```python
"""Every tenant-scoped read goes through here, so there is one place to audit.

A read with no company_id is refused rather than answered. A query that returns
everything when the caller meant nothing is how tenant isolation fails in
practice, and it fails silently.
"""

from sqlalchemy.orm import Session

from app.state.models import DocumentVersion


def versions_for_company(session: Session, company_id: str) -> list[DocumentVersion]:
    if not company_id or not isinstance(company_id, str):
        raise ValueError("company_id is required; refusing an unscoped read")
    return (
        session.query(DocumentVersion)
        .filter(DocumentVersion.company_id == company_id)
        .order_by(DocumentVersion.id)
        .all()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test`
Expected: PASS, all tests across all files green.

Run: `make fresh-check`
Expected: `fresh clone: tests pass at /tmp/...`

- [ ] **Step 5: Commit**

```bash
git add app/state/queries.py tests/test_isolation.py
git commit -m "Isolation: company_id proved by test, not promised by a document"
```

---

## What this plan deliberately leaves out

This plan stops where the model starts. It builds every stage that can be tested with no API key and no network — ingestion, normalization, verification, occurrence identity, diff, the draft-and-final split, tenant isolation. That is the part of Strata a reviewer can audit without trusting anything about the AI, and it is the part whose failure would make the product pointless.

A second plan covers the rest: the event log and state projection, the interpretation stage and its single model call, the escalation queue, the four screens from `docs/web-design.html`, and the deployment to `strata.sudama.ai`. It depends on every interface produced above, which is why it comes second.

**Sequencing note carried from the spec:** if the night goes badly, the assets that must survive are the citation verifier, the occurrence check and the deterministic diff. Tasks 4, 5 and 6 are the submission's argument. Everything else is in service of them.
