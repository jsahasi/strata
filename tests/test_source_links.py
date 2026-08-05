"""The link from a claim back to the filing it came from.

An analyst who reads "Exhibit JS-3 at 12" opens the exhibit. This product shows
the source extract with the cited characters marked, which is better than a page
number, and until now it stopped there: no way out to the commission's own copy.
An extract with no way to reach the document behind it asks the reader to trust
us, and trust is the one thing this product refuses to ask for.

Four states, and the reader must be able to tell them apart:

  a real filing        a link to the commission's copy of the PDF
  a synthetic fixture  a sentence saying there is nothing to open
  a refused address    a sentence saying the recorded address was not a web one
  an unreadable source a sentence saying nothing is known about where it came from

The last three are the ones worth testing hardest. A missing link and a dead
link are different facts, and a blank space says neither. The synthetic corpus
has no URL and must never be given one -- a plausible docket search page would
be the product inventing provenance, which is the failure everything else here
exists to prevent.

THE URL IS UNTRUSTED. Today it comes from a JSON file on disk; tomorrow from a
source an administrator typed. `javascript:` in an href is the oldest trick
there is, so the scheme is checked in app/state/claims.py -- one place, before
either template sees it -- and the refused address is not rendered at all, not
even as text. These tests assert against the bytes on the wire for that reason.

The last test in this file runs the real ingest and then asks the product what
it will say about the Kentucky claim. Every other test stamps a URL onto a
version by hand, and a feature that only works when a test sets it up is a
feature that works nowhere. That one proves the path from the provenance file on
disk to the link on the screen.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.seed import CLAIM_MISQUOTE, load
from app.state import sharing
from app.state.claims import (
    NOTE_NONE_RECORDED,
    NOTE_REFUSED,
    NOTE_UNREADABLE,
    SOURCE_LINK_AVAILABLE,
    SOURCE_LINK_NONE_RECORDED,
    SOURCE_LINK_REFUSED,
    SOURCE_LINK_UNREADABLE,
    safe_source_url,
    source_filing,
    verified_claims,
)
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role
from app.state.models import (
    ROLE_ANALYST,
    SHARE_SUBJECT_CLAIM,
    Claim,
    DocumentVersion,
)
from app.state.queries import versions_for_company
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.views import changes as changes_view
from app.web.views import share as share_view

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ingest_real import PAIRS, REAL, provenance, provenance_columns  # noqa: E402
from ingest_real import main as ingest_real_main  # noqa: E402

COMPANY = "MEP"

# The change carrying both planted claims: one that verifies and one that cites
# real offsets with words that are not there.
PAIRED = "CHG-v1-v2-003"
VERIFIED_CLAIM = "CLM-CHG-2"

SHARER_EMAIL = "source.links@meridian.example"
SHARER_PASSWORD = "source-links-password"

# A real address, taken from the corpus rather than invented here.
KENTUCKY = (
    "https://psc.ky.gov/pscecf/2025-00113/rateintervention%40ky.gov/"
    "09092025021845/25.09.09_KOLLEN_Direct_and_Exhibits.pdf"
)
FILER = "Kentucky Office of the Attorney General, Office of Rate Intervention"
FILED = "2025-09-09"
RETRIEVED = datetime(2026, 8, 4, 16, 47, 36, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus():
    """A fresh database and the synthetic corpus, which carries no URLs."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_system_roles(session)
        sharer = create_user(
            session,
            COMPANY,
            email=SHARER_EMAIL,
            display_name="A sharing analyst",
            password=SHARER_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session,
            COMPANY,
            user_id=sharer.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )
        return {"sharer": sharer.id}


@pytest.fixture
def client(corpus) -> TestClient:
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(changes_view.router)
    return TestClient(app)


@pytest.fixture
def share_client(corpus) -> TestClient:
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(share_view.router)
    return TestClient(app)


def _stamp_every_version(url: str | None, **rest) -> None:
    """Record where the company's versions came from. The test's ingest step."""
    with session_scope() as session:
        for version in versions_for_company(session, COMPANY):
            version.source_url = url
            version.filer = rest.get("filer")
            version.filing_date = rest.get("filing_date")
            version.source_retrieved_at = rest.get("retrieved_at")


def _mint(user_id: str, claim_id: str):
    """Real link, real clock, and that is the decision -- see tests/test_sharing.py.

    Listed in ALLOWED in tests/test_clock_pinned.py. Every link this mints is
    opened through `share_client.get(minted.url)`, and the share route reads the
    real now with no way for a test to reach in and say otherwise. Pinning the
    mint alone would stamp a row 2026-08-04 and hand it to a route living on
    today's date: fine for a week, then a failure on somebody else's clean clone
    that nothing in the output explains. That is the exact mechanism of the
    invitation defect this rule came from, so pinning here would plant it rather
    than pull it.

    Both ends stay on the real clock, where they move together and the seven-day
    life never runs out between two lines of one test.
    """
    with session_scope() as session:
        return sharing.create_share_link(
            session,
            company_id=COMPANY,
            user_id=user_id,
            subject_type=SHARE_SUBJECT_CLAIM,
            subject_id=claim_id,
        )


def _claim_articles(body: str, kind: str) -> list[str]:
    """Each claim article on the page, so an assertion cannot wander off it."""
    found = []
    for chunk in body.split(f'<article class="claim claim--{kind}"')[1:]:
        found.append(chunk.split("</article>")[0])
    assert found, f"no {kind} claim on the page; the assertion would be vacuous"
    return found


# ---------------------------------------------------------------------------
# The scheme check, before anything renders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://psc.ky.gov/pscecf/2025-00113/x.pdf",
        "http://psc.ky.gov/pscecf/2025-00113/x.pdf",
        "HTTPS://PSC.KY.GOV/x.pdf",
        "  https://psc.ky.gov/x.pdf  ",
    ],
)
def test_a_web_address_survives(url):
    assert safe_source_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "mailto:clerk@psc.ky.gov",
        "//psc.ky.gov/x.pdf",
        "psc.ky.gov/x.pdf",
        "https://",
        "",
        "   ",
        None,
    ],
)
def test_anything_that_is_not_a_web_address_is_refused(url):
    assert safe_source_url(url) is None


def test_a_refused_address_is_not_carried_anywhere_on_the_filing():
    """Refusing to link is not enough. The string must not leave the module.

    Rendering `javascript:alert(1)` as plain text beside a refusal is a smaller
    mistake than putting it in an href, but it is one edit away from the larger
    one -- someone wraps the text in a link to be helpful. So nothing on the
    filing carries it.
    """
    filing = source_filing(source_url="javascript:alert(1)", filer="Somebody")
    assert filing.state == SOURCE_LINK_REFUSED
    assert filing.url is None
    assert filing.note == NOTE_REFUSED
    for field in (filing.url, filing.host, filing.link_text, filing.note, filing.detail):
        assert "javascript" not in (field or "").lower()


def test_a_real_filing_carries_its_host_and_what_is_known_about_it():
    filing = source_filing(
        source_url=KENTUCKY, filer=FILER, filing_date=FILED, retrieved_at=RETRIEVED
    )
    assert filing.state == SOURCE_LINK_AVAILABLE
    assert filing.url == KENTUCKY
    assert filing.host == "psc.ky.gov"
    assert filing.note == ""
    assert FILED in filing.detail
    assert FILER in filing.detail
    assert "2026-08-04" in filing.detail


def test_the_link_names_the_host_the_address_really_goes_to():
    """"https://psc.ky.gov@evil.example/x" goes to evil.example, and says so.

    Credentials in front of the host are the oldest way to make a link read as
    one place and go to another. The link text is built from the parsed host
    rather than from the string a person typed, so the words the reader sees
    name where the click lands.
    """
    filing = source_filing(source_url="https://psc.ky.gov@evil.example/x.pdf")
    assert filing.host == "evil.example"
    assert filing.link_text == "Open the filing at evil.example"
    assert "psc.ky.gov" not in filing.link_text


def test_no_url_is_a_fixture_and_says_so():
    filing = source_filing(source_url=None)
    assert filing.state == SOURCE_LINK_NONE_RECORDED
    assert filing.url is None
    assert filing.note == NOTE_NONE_RECORDED


def test_a_filing_never_offers_a_link_and_an_excuse_at_the_same_time():
    for filing in (
        source_filing(source_url=KENTUCKY),
        source_filing(source_url=None),
        source_filing(source_url="data:text/html,x"),
    ):
        assert bool(filing.url) != bool(filing.note)


# ---------------------------------------------------------------------------
# The change screen
# ---------------------------------------------------------------------------


def test_the_change_screen_links_a_claim_to_the_commissions_own_copy(client):
    _stamp_every_version(
        KENTUCKY, filer=FILER, filing_date=FILED, retrieved_at=RETRIEVED
    )
    body = client.get(f"/changes/{PAIRED}").text

    assert f'href="{KENTUCKY}"' in body
    assert 'rel="noopener noreferrer"' in body
    assert "psc.ky.gov" in body
    assert FILED in body


def test_both_the_asserted_and_the_refused_claim_carry_the_link(client):
    """A withheld claim needs the source more than a verified one does.

    The reader is being told the product will not make a statement. The next
    question is what the filing actually says, and the answer is one click away
    or it is a wall.
    """
    _stamp_every_version(KENTUCKY, filer=FILER, filing_date=FILED)
    body = client.get(f"/changes/{PAIRED}").text

    for kind in ("verified", "withheld"):
        for article in _claim_articles(body, kind):
            assert f'href="{KENTUCKY}"' in article


def test_the_synthetic_corpus_says_there_is_no_filing_and_offers_no_link(client):
    """The seeded corpus has no URL, and the screen says which fact that is."""
    body = client.get(f"/changes/{PAIRED}").text

    for kind in ("verified", "withheld"):
        for article in _claim_articles(body, kind):
            assert NOTE_NONE_RECORDED in article
            assert "<a " not in article


def test_the_synthetic_corpus_is_never_given_a_url_by_the_seed(corpus):
    """Not one row. A plausible docket link would be invented provenance."""
    with session_scope() as session:
        for version in versions_for_company(session, COMPANY):
            assert version.source_url is None
            assert version.filer is None
            assert version.filing_date is None


@pytest.mark.parametrize(
    "hostile, scheme",
    [
        ("javascript:alert(document.domain)", "javascript:"),
        ("data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==", "data:"),
    ],
)
def test_a_hostile_address_never_reaches_the_change_screen(client, hostile, scheme):
    _stamp_every_version(hostile, filer=FILER, filing_date=FILED)
    body = client.get(f"/changes/{PAIRED}").text

    # The scheme with its colon, not the bare word: base.html's noscript notice
    # says "javascript" in a sentence, and an assertion that fails on that would
    # be measuring the wrong thing.
    assert hostile not in body
    assert scheme not in body.lower()
    assert NOTE_REFUSED in body
    # The refusal is not a blank space: the reader is told the address was bad,
    # which is a different fact from there being no address at all.
    assert NOTE_NONE_RECORDED not in body


def test_an_address_cannot_break_out_of_the_attribute_it_sits_in(client):
    """A quote in the URL closes the href unless the template escapes it.

    `https://psc.ky.gov/x" onmouseover="alert(1)` is a valid http address, so
    the scheme check passes it -- correctly. What stops it is the escaping, and
    the day somebody marks this value |safe to "fix" a display bug, this test is
    what says no.
    """
    _stamp_every_version('https://psc.ky.gov/x" onmouseover="alert(1)')
    body = client.get(f"/changes/{PAIRED}").text

    assert 'onmouseover="alert(1)"' not in body
    assert "&#34;" in body or "&quot;" in body


def test_a_source_that_cannot_be_read_is_not_called_a_fixture(client):
    """Three absences, three sentences. None of them a guess.

    A claim citing a version this company does not hold is already withheld.
    What the screen must not do is add "this is a synthetic fixture", because
    nothing here knows that -- the version may exist and be another tenant's.
    """
    with session_scope() as session:
        session.add(
            Claim(
                id="CLM-NOWHERE",
                company_id=COMPANY,
                change_id=PAIRED,
                statement="A statement about a version this company does not hold.",
                citation_version_id="v-not-here",
                citation_start=0,
                citation_end=10,
                citation_quote="whatever",
                cited_occurrence=None,
                confidence_bp=10000,
            )
        )

    body = client.get(f"/changes/{PAIRED}").text
    article = [
        chunk
        for chunk in _claim_articles(body, "withheld")
        if "CLM-NOWHERE" in chunk
    ]
    assert article, "the claim citing a missing version is not on the page"
    assert NOTE_UNREADABLE in article[0]
    assert NOTE_NONE_RECORDED not in article[0]


def test_the_link_does_not_depend_on_a_script(client):
    """Same rule as the source panel: the evidence survives scripts blocked."""
    _stamp_every_version(KENTUCKY, filer=FILER, filing_date=FILED)
    body = client.get(f"/changes/{PAIRED}").text
    anchor = body.split(f'href="{KENTUCKY}"')[0].rsplit("<a", 1)[1]
    assert "onclick" not in anchor
    assert "hidden" not in anchor


# ---------------------------------------------------------------------------
# The share page, where it matters most
# ---------------------------------------------------------------------------


def test_the_share_page_hands_a_stranger_the_commissions_copy(
    share_client, corpus
):
    """The land-and-expand argument in one link.

    Somebody outside the company, who cannot sign in, is handed a claim and a
    way to check it against the commission's own document. That is the whole
    case for this product, and it is one anchor tag.
    """
    _stamp_every_version(
        KENTUCKY, filer=FILER, filing_date=FILED, retrieved_at=RETRIEVED
    )
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    body = share_client.get(minted.url).text
    assert f'href="{KENTUCKY}"' in body
    assert 'rel="noopener noreferrer"' in body
    assert FILED in body


def test_the_share_page_says_when_there_is_no_filing_to_open(share_client, corpus):
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    body = share_client.get(minted.url).text

    assert NOTE_NONE_RECORDED in body
    assert "psc.ky.gov" not in body


def test_the_share_page_refuses_a_hostile_address_too(share_client, corpus):
    """One check, both surfaces. The share page is the one nobody signs in for."""
    _stamp_every_version("javascript:alert(1)", filer=FILER)
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)

    body = share_client.get(minted.url).text
    assert "javascript" not in body.lower()
    assert NOTE_REFUSED in body


def test_the_filing_is_the_only_way_off_the_share_page_and_it_goes_outward(
    share_client, corpus
):
    """The share page leads nowhere into the product. It now leads one place out.

    tests/test_sharing.py holds the older, stricter form of this: every href on
    the page must be /login or the stylesheet. That test passes today only
    because the seeded corpus has no URL, and it will fail the moment a real
    filing is shared -- so whoever wires the real corpus into that fixture has
    to widen it on purpose, and this test says what "on purpose" means.

    A link to the commission's own site is not a way into the workspace. It
    reveals no id, no docket and no sibling claim; it is the reader checking us
    against the source, which is the entire argument for this page existing.
    """
    _stamp_every_version(KENTUCKY, filer=FILER, filing_date=FILED)
    minted = _mint(corpus["sharer"], VERIFIED_CLAIM)
    body = share_client.get(minted.url).text

    hrefs = {
        href
        for href in re.findall(r'href="([^"]+)"', body)
        if not href.startswith("#")
    }
    assert hrefs == {"/login", "/static/strata.css", KENTUCKY}


def test_a_refused_claim_on_the_share_page_still_points_at_the_filing(
    share_client, corpus
):
    _stamp_every_version(KENTUCKY, filer=FILER, filing_date=FILED)
    minted = _mint(corpus["sharer"], CLAIM_MISQUOTE)

    body = share_client.get(minted.url).text
    assert f'href="{KENTUCKY}"' in body


# ---------------------------------------------------------------------------
# What the claim itself carries
# ---------------------------------------------------------------------------


def test_every_claim_carries_a_filing_whether_or_not_there_is_one(corpus):
    """No caller has to ask whether the field is there. It always is."""
    with session_scope() as session:
        good, held = verified_claims(session, COMPANY, PAIRED)
    assert good and held
    for claim in (*good, *held):
        assert claim.filing.state in (
            SOURCE_LINK_AVAILABLE,
            SOURCE_LINK_NONE_RECORDED,
            SOURCE_LINK_REFUSED,
            SOURCE_LINK_UNREADABLE,
        )


def test_the_filing_follows_the_version_the_claim_actually_cites(corpus):
    """Stamp one version, not all of them, and only its claims get the link."""
    with session_scope() as session:
        good, _ = verified_claims(session, COMPANY, PAIRED)
        cited = good[0].citation_version_id
        for version in versions_for_company(session, COMPANY):
            if version.id != cited:
                version.source_url = KENTUCKY

    with session_scope() as session:
        good, _ = verified_claims(session, COMPANY, PAIRED)
    assert good[0].filing.url is None

    with session_scope() as session:
        session.get(DocumentVersion, cited).source_url = KENTUCKY

    with session_scope() as session:
        good, _ = verified_claims(session, COMPANY, PAIRED)
    assert good[0].filing.url == KENTUCKY


# ---------------------------------------------------------------------------
# The ingest, which is where the provenance was being thrown away
# ---------------------------------------------------------------------------


def test_the_ingest_carries_the_four_things_the_provenance_file_knows():
    meta = provenance(PAIRS[0]["versions"][0][0])
    columns = provenance_columns(meta)

    assert columns["source_url"] == meta["source_url"]
    assert columns["filer"] == meta["filer"]
    assert columns["filing_date"] == meta["filing_date"]
    assert columns["source_retrieved_at"] == datetime(
        2026, 8, 4, 16, 47, 36, tzinfo=timezone.utc
    )
    # An aware instant, because UtcDateTime refuses a naive one and a naive
    # timestamp in a provenance record cannot be compared across systems.
    assert columns["source_retrieved_at"].tzinfo is not None


def test_the_ingest_refuses_a_provenance_file_with_no_web_address():
    """A file naming no URL, or naming a hostile one, yields no link at all."""
    assert provenance_columns({})["source_url"] is None
    assert provenance_columns({"source_url": "javascript:alert(1)"})["source_url"] is None
    assert provenance_columns({"retrieved_at": "not a date"})["source_retrieved_at"] is None


def test_the_real_ingest_ends_with_a_claim_that_asserts_and_a_link_to_open():
    """The whole path, end to end, on the corpus this was built for.

    A thing that is built and a thing that is connected are different facts.
    Every other test here stamps a URL onto a version by hand; this one runs the
    ingest the way a person runs it, and asks the product what it will say
    afterwards. If the Kentucky offsets drift, or the provenance stops being
    carried, or the claim stops verifying, this fails -- and none of the others
    would.
    """
    init_db()
    ingest_real_main()

    with session_scope() as session:
        claim = session.get(Claim, "CLM-KY-2025-00113-BESS")
        assert claim is not None, "the ingest wrote no claim to link from"
        good, held = verified_claims(session, COMPANY, claim.change_id)

    asserted = {claim.claim_id: claim for claim in good}
    assert "CLM-KY-2025-00113-BESS" in asserted, [
        (item.claim_id, item.reason_code) for item in held
    ]

    filing = asserted["CLM-KY-2025-00113-BESS"].filing
    assert filing.state == SOURCE_LINK_AVAILABLE
    assert filing.host == "psc.ky.gov"
    assert filing.url.startswith("https://psc.ky.gov/pscecf/2025-00113/")
    assert "2025-09-30" in filing.detail


def test_every_pair_the_script_ingests_names_a_file_that_exists():
    for pair in PAIRS:
        for stem, _status in pair["versions"]:
            assert (REAL / f"{stem}.txt").exists()
            assert (REAL / f"{stem}.provenance.json").exists()
            assert provenance(stem)["source_url"].startswith("https://")
