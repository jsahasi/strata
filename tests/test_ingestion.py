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
