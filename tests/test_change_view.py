"""The change screen: what it renders, and the one thing it must never render.

The test this file exists for is the first one. A claim whose citation does not
verify must not put its statement in the HTML at all -- not hidden, not greyed,
not behind a caveat. It is asserted against `response.text` rather than against
a screenshot or a CSS class, because "it looked greyed out" is a property of a
stylesheet and a stylesheet can be edited by someone who never read ADR-003.
The string is either in the bytes on the wire or it is not.

The rest of the file holds the screen to the same standard elsewhere: the
verified quote comes from the stored source rather than from the claim, the
marked span in the citation viewer is the real bytes at the cited offsets, a
change id belonging to another company answers 404 rather than another tenant's
rows, and rendering the page writes nothing.

The suite builds its own FastAPI app around the router. That is deliberate: the
router has to be mountable on its own, and a test that reached for a
module-level application would pass or fail for reasons that have nothing to do
with this screen.
"""

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from markupsafe import escape

from app.seed import CLAIM_AMBIGUOUS, CLAIM_MISQUOTE, load
from app.state.audit import event_count
from app.state.claims import (
    REASON_CODE_QUOTE_MISMATCH,
    change_for_company,
    verified_claims,
)
from app.state.db import init_db, session_scope
from app.state.models import Claim
from app.state.queries import versions_for_company
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.deps import current_company
from app.web.views import changes

COMPANY = "MEP"

# The paired demonstration the whole submission turns on: one change carrying a
# claim that verifies and a claim that does not, side by side on one screen.
PAIRED = "CHG-v1-v2-003"

# A real change nobody wrote a claim about. Most of the 27 look like this, and
# the screen has to say so rather than render an empty region.
UNCLAIMED = "CHG-v1-v2-000"

# FINAL, and the diff is not confident it matched the passage (0.01).
FINAL_LOW = "CHG-v2-v3-013"

# DRAFT, and the alignment is not in doubt (0.98).
DRAFT_SURE = "CHG-v1-v2-006"

# The second planted failure: a quote that appears three times, cited without
# saying which occurrence it means.
AMBIGUOUS = "CHG-v2-v3-011"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(changes.router)
    return app


@pytest.fixture
def client() -> TestClient:
    """A fresh database, the real corpus, and the router mounted on its own."""
    init_db()
    with session_scope() as session:
        load(session)
    return TestClient(_build_app())


def _html(text: str) -> str:
    """The same text as the template would emit it.

    The corpus is full of double quotes -- every defined term in the filing
    carries a pair -- and Jinja escapes them. Comparing raw source bytes against
    the response would fail for a reason that has nothing to do with what is
    being tested, so the expected string is escaped the same way the template
    escapes it.
    """
    return str(escape(text))


def _statement(claim_id: str) -> str:
    """The stored statement, read from the database rather than typed here.

    Hardcoding it would let the corpus and the test drift apart, and the drift
    would show up as a passing test.
    """
    with session_scope() as session:
        claim = session.get(Claim, claim_id)
        assert claim is not None, f"{claim_id} is not in the seeded corpus"
        return claim.statement


def _source(version_id: str) -> str:
    with session_scope() as session:
        for version in versions_for_company(session, COMPANY):
            if version.id == version_id:
                return version.source_text
    raise AssertionError(f"{version_id} is not stored for {COMPANY}")


# ---------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------


def test_a_withheld_claims_statement_never_appears_in_the_response_body(client):
    """ADR-003, asserted against the bytes.

    CLM-MISQUOTE cites real offsets with words that are not there. Its stored
    statement -- "The Large Load Customer threshold moved to 10 megawatts (MW)."
    -- is a fluent, plausible, wrong sentence, which is exactly the failure a
    model produces. It must not reach the page in any form.
    """
    fabricated = _statement(CLAIM_MISQUOTE)

    response = client.get(f"/changes/{PAIRED}")
    assert response.status_code == 200

    assert fabricated not in response.text
    assert _html(fabricated) not in response.text

    # Not vacuous: the claim IS on the page, as a refusal. If the withheld claim
    # were dropped from the screen entirely, the assertion above would pass and
    # mean nothing.
    assert CLAIM_MISQUOTE in response.text
    assert "claim--withheld" in response.text
    assert REASON_CODE_QUOTE_MISMATCH in response.text


def test_no_withheld_claim_carries_a_statement_element(client):
    """The structural half of the same rule, on every withheld claim there is.

    The statement position on a withheld claim is occupied by the reason. A
    p.claim__statement inside a withheld article would mean a template branched
    on a flag instead of iterating two lists, which is the mistake ADR-003 is
    written to prevent.
    """
    for change_id in (PAIRED, AMBIGUOUS):
        body = client.get(f"/changes/{change_id}").text
        for article in body.split('<article class="claim claim--withheld"')[1:]:
            article = article.split("</article>")[0]
            assert "claim__statement" not in article
            assert "citation-chip" not in article


def test_the_statement_disappears_when_the_source_moves_under_the_claim(client):
    """Verification is a read, not a stored verdict.

    CLM-CHG-2 verifies against the corpus. Change the bytes it cites -- leaving
    the offsets and the length alone -- and the very next render must withhold
    it. Nothing is re-run, no job fires: the page re-reads the source.
    """
    statement = _statement("CLM-CHG-2")
    assert statement in client.get(f"/changes/{PAIRED}").text

    with session_scope() as session:
        change = change_for_company(session, COMPANY, PAIRED)
        for version in versions_for_company(session, COMPANY):
            if version.id == change.to_version_id:
                version.source_text = version.source_text.replace(
                    "20 megawatts (MW)", "50 megawatts (MW)"
                )

    body = client.get(f"/changes/{PAIRED}").text
    assert statement not in body
    assert REASON_CODE_QUOTE_MISMATCH in body


# ---------------------------------------------------------------------------
# What a verified claim shows
# ---------------------------------------------------------------------------


def test_the_verified_claim_and_its_quote_are_present(client):
    with session_scope() as session:
        verified, withheld = verified_claims(session, COMPANY, PAIRED)

    assert [claim.claim_id for claim in verified] == ["CLM-CHG-2"]
    assert [claim.claim_id for claim in withheld] == [CLAIM_MISQUOTE]

    body = client.get(f"/changes/{PAIRED}").text
    assert _html(verified[0].statement) in body
    assert "claim--verified" in body
    # The quote shown is the source's own bytes, not the text the claim carried.
    assert _html(verified[0].actual_text) in body


def test_the_source_panel_carries_the_cited_span_marked(client):
    """The citation viewer, server-rendered.

    The marked span must be the characters the citation covers, taken from the
    stored source. Echoing the quote the claim supplied would prove nothing --
    it would mark the claim against itself.
    """
    with session_scope() as session:
        verified, _ = verified_claims(session, COMPANY, PAIRED)
    claim = verified[0]

    body = client.get(f"/changes/{PAIRED}").text

    cited = _source(claim.citation_version_id)[
        claim.citation_start : claim.citation_end
    ]
    assert f'<mark class="source-mark">{_html(cited)}</mark>' in body

    # The chip points at a panel that is really on the page.
    panel_id = changes.panel_id(claim.claim_id)
    assert f'aria-controls="{panel_id}"' in body
    assert f'id="{panel_id}"' in body


def test_the_source_panel_shows_context_around_the_span(client):
    """An extract, not the bare quote. The analyst reads it where it sits."""
    with session_scope() as session:
        verified, _ = verified_claims(session, COMPANY, PAIRED)
    claim = verified[0]

    source = _source(claim.citation_version_id)
    window = changes.source_window(source, claim.citation_start, claim.citation_end)

    assert window.marked == source[claim.citation_start : claim.citation_end]
    assert window.before or window.after, "the panel showed no context at all"
    assert source.startswith(window.before, window.start)
    assert window.start <= claim.citation_start
    assert window.end >= claim.citation_end


def test_the_source_panel_is_visible_without_javascript(client):
    """The evidence never depends on a script running.

    The panel is server-rendered open and the chip says so. citation.js closes
    the panels on load and takes over the toggling. Ship it the other way round
    and a reviewer with scripts blocked sees the claim and none of its proof.
    """
    body = client.get(f"/changes/{PAIRED}").text
    assert 'aria-expanded="true"' in body

    opening_tag = body.split('<section class="source-panel"', 1)[1].split(">", 1)[0]
    assert "hidden" not in opening_tag


# ---------------------------------------------------------------------------
# What a withheld claim shows
# ---------------------------------------------------------------------------


def test_the_withheld_claim_shows_the_source_and_names_the_quote_as_quoted(client):
    """What the source says, then what was quoted, each labelled as itself."""
    with session_scope() as session:
        _, withheld = verified_claims(session, COMPANY, PAIRED)
    claim = withheld[0]

    body = client.get(f"/changes/{PAIRED}").text

    assert _html(claim.source_excerpt) in body
    assert claim.reason_text in body
    assert _html(claim.citation_quote) in body

    # The source comes first. The failed quotation never occupies the position
    # where the document's own words belong.
    assert body.index(_html(claim.source_excerpt)) < body.index(
        _html(claim.citation_quote)
    )


def test_the_ambiguous_claim_is_withheld_and_says_why(client):
    ambiguous = _statement(CLAIM_AMBIGUOUS)
    body = client.get(f"/changes/{AMBIGUOUS}").text
    assert ambiguous not in body
    assert CLAIM_AMBIGUOUS in body
    assert "claim--withheld" in body


# ---------------------------------------------------------------------------
# The change itself
# ---------------------------------------------------------------------------


def test_before_and_after_text_come_from_the_stored_source(client):
    with session_scope() as session:
        change = change_for_company(session, COMPANY, PAIRED)
        before_id, after_id = change.from_version_id, change.to_version_id
        before = _source(before_id)[change.before_start : change.before_end]
        after = _source(after_id)[change.after_start : change.after_end]

    body = client.get(f"/changes/{PAIRED}").text
    assert _html(before) in body
    assert _html(after) in body
    assert "modified" in body


def test_the_draft_or_final_badge_follows_the_stored_status(client):
    """ADR-005: the status is a field, and the page reads it rather than guessing."""
    draft = client.get(f"/changes/{PAIRED}").text
    assert "badge--draft" in draft
    assert "badge--final" not in draft

    final = client.get(f"/changes/{FINAL_LOW}").text
    assert "badge--final" in final
    assert "badge--draft" not in final


def test_low_alignment_confidence_is_shown_and_a_sure_one_is_not(client):
    """An uncertain pairing announces itself. A confident one adds no noise."""
    assert changes.ALIGNMENT_LABEL in client.get(f"/changes/{FINAL_LOW}").text
    assert changes.ALIGNMENT_LABEL not in client.get(f"/changes/{DRAFT_SURE}").text


def test_a_change_with_no_claims_says_so(client):
    """Rendering nothing would read as "nothing to report", which is a claim."""
    response = client.get(f"/changes/{UNCLAIMED}")
    assert response.status_code == 200
    assert "empty" in response.text
    assert "claim--verified" not in response.text
    assert "claim--withheld" not in response.text


# ---------------------------------------------------------------------------
# Scope and failure
# ---------------------------------------------------------------------------


def test_another_companys_change_id_is_a_404_and_leaks_nothing(client):
    """The id is real. It belongs to MEP. Another tenant gets a 404, not a row."""
    statement = _statement("CLM-CHG-2")
    client.app.dependency_overrides[current_company] = lambda: "OTHER"
    try:
        response = client.get(f"/changes/{PAIRED}")
    finally:
        client.app.dependency_overrides.clear()

    assert response.status_code == 404
    assert statement not in response.text
    assert "Meridian" not in response.text


def test_an_unknown_change_id_is_a_404(client):
    assert client.get("/changes/CHG-does-not-exist").status_code == 404


def test_the_missing_and_the_forbidden_answer_alike(client):
    """Two different facts, one answer, on purpose.

    A 404 for the unknown id and a 403 for the other tenant's would let anyone
    enumerate which change ids exist somewhere in the system. Same status, same
    body.
    """
    unknown = client.get("/changes/CHG-does-not-exist")

    client.app.dependency_overrides[current_company] = lambda: "OTHER"
    try:
        forbidden = client.get(f"/changes/{PAIRED}")
    finally:
        client.app.dependency_overrides.clear()

    assert unknown.status_code == forbidden.status_code
    assert unknown.json() == forbidden.json()


def test_rendering_the_page_writes_nothing(client):
    """A GET that writes cannot be cached, replayed, or trusted in an audit."""
    with session_scope() as session:
        before = event_count(session, COMPANY)
        claims = session.query(Claim).count()

    for change_id in (PAIRED, UNCLAIMED, AMBIGUOUS, FINAL_LOW):
        assert client.get(f"/changes/{change_id}").status_code == 200

    with session_scope() as session:
        assert event_count(session, COMPANY) == before
        assert session.query(Claim).count() == claims


def test_the_stylesheet_and_the_script_are_served(client):
    """base.html links both at fixed absolute paths. A 404 here is an unstyled page."""
    assert client.get("/static/strata.css").status_code == 200
    assert client.get("/static/citation.js").status_code == 200
