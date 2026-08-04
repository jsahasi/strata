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
