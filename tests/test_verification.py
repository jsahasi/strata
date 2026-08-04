import json
import random
from pathlib import Path

from app.ingestion.ingest import ingest_version
from app.state.db import init_db, session_scope
from app.state.models import DocumentVersion
from app.text.normalize import normalize, normalized_projection
from app.verification import verifier
from app.verification.verifier import (
    REASON_EMPTY_SPAN,
    REASON_OUT_OF_RANGE,
    REASON_QUOTE_MISMATCH,
    REASON_VERSION_CHANGED,
    REASON_VERSION_UNREADABLE,
    Citation,
    _covers_whole_characters,
    _spans_of,
    occurrence_count,
    verify_citation,
    verify_citation_for_version,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def _v2_text() -> str:
    return (DATA / "v2_revised_proposed_rule.txt").read_bytes().decode("utf-8")


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


# --- the citation must be verified against the version it names --------------
#
# Citation.version_id was carried everywhere and checked nowhere. verify_citation
# takes a string and so cannot check it; that is a real hole, not a theoretical
# one, because the same words sit at different offsets in every version of a
# proceeding. These tests cover the entry point that closes it.

BOILERPLATE = (
    "The Utility shall maintain records sufficient to demonstrate compliance "
    "with this Order for a period of not less than five (5) years."
)
# The Section 4.4 occurrence, per data/manifest.json. Same sentence, 200
# characters further into v2 than into v1, because v2 inserted text above it.
V1_OCCURRENCE_0 = (4930, 5063)
V2_OCCURRENCE_0 = (5130, 5263)


def _seed_a_proceeding(session) -> None:
    for version_id, status, text in (
        ("v1", "DRAFT", _v1_text()),
        ("v2", "DRAFT", _v2_text()),
    ):
        ingest_version(
            session,
            version_id=version_id,
            company_id="MEP",
            docket="MPUC-2026-0142",
            label=version_id,
            status=status,
            source_text=text,
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
    session.flush()


def test_the_two_argument_form_cannot_catch_a_mispaired_version():
    # Stated as a test rather than left implicit, because it is the reason the
    # version-aware entry point exists. A citation naming v2, handed v1's text,
    # verifies -- the words are there, at those offsets, in that document. The
    # function has no way to know it is the wrong document.
    start, end = V1_OCCURRENCE_0
    misfiled = Citation("v2", start, end, BOILERPLATE)
    result = verify_citation(misfiled, _v1_text(), expected_occurrence=0)
    assert result.verified is True


def test_the_version_aware_form_rejects_that_same_citation():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V1_OCCURRENCE_0
        misfiled = Citation("v2", start, end, BOILERPLATE)
        result = verify_citation_for_version(
            session, misfiled, "MEP", expected_occurrence=0
        )
        assert result.verified is False
        assert result.reason == REASON_QUOTE_MISMATCH
        # It read v2, which is the whole point: those offsets land mid-sentence
        # in Section 4.2 of v2, nowhere near the recordkeeping requirement.
        assert result.actual_text == _v2_text()[start:end]
        assert BOILERPLATE not in result.actual_text


def test_the_version_aware_form_verifies_the_citation_when_it_names_its_own_version():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V2_OCCURRENCE_0
        correct = Citation("v2", start, end, BOILERPLATE)
        result = verify_citation_for_version(
            session, correct, "MEP", expected_occurrence=0
        )
        assert result.verified is True
        assert result.reason is None


def test_the_version_aware_form_still_applies_the_occurrence_rule():
    # Loading the right version must not weaken the checks that already ran on
    # it. The Section 4.4 occurrence claimed as the Section 7.3 one is still
    # refused, and an unstated occurrence is still refused.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V2_OCCURRENCE_0
        citation = Citation("v2", start, end, BOILERPLATE)
        assert not verify_citation_for_version(session, citation, "MEP").verified
        assert not verify_citation_for_version(
            session, citation, "MEP", expected_occurrence=2
        ).verified


def test_a_version_this_company_cannot_read_is_refused_not_answered():
    # A citation naming another tenant's version, and one naming a version that
    # does not exist, must be indistinguishable from outside. Telling them apart
    # tells a caller which version ids exist.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        for version_id in ("rival-v1", "v9-does-not-exist"):
            result = verify_citation_for_version(
                session, Citation(version_id, 0, 20, "RIVAL confidential"), "MEP"
            )
            assert result.verified is False
            assert result.reason == REASON_VERSION_UNREADABLE
            assert result.actual_text is None


def test_the_other_tenant_is_refused_even_when_the_quote_is_correct():
    # The quote below really is the first 18 characters of rival-v1. Reading it
    # back would confirm another tenant's source text to a caller who guessed a
    # version id, which is the failure the chokepoint exists to prevent.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        result = verify_citation_for_version(
            session, Citation("rival-v1", 0, 18, "RIVAL confidential"), "MEP"
        )
        assert result.verified is False
        assert result.reason == REASON_VERSION_UNREADABLE

        # And the same citation, read by the tenant that owns it, verifies --
        # so the refusal above is scope, not a broken lookup.
        allowed = verify_citation_for_version(
            session, Citation("rival-v1", 0, 18, "RIVAL confidential"), "RIVAL"
        )
        assert allowed.verified is True


def test_an_unscoped_read_is_refused_rather_than_treated_as_a_wildcard():
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        for company_id in ("", None):
            try:
                verify_citation_for_version(
                    session, Citation("v1", *V1_OCCURRENCE_0, BOILERPLATE), company_id
                )
            except ValueError:
                continue
            raise AssertionError(f"company_id {company_id!r} should have been refused")


# --- the verdict is bound to the version's bytes, not to its id --------------
#
# best-practices.html principle 28. A version id names a row; the row's text can
# move underneath it. Verifying against whatever the row holds now answers "does
# this quote sit in that row" and reads as "does this quote sit in the filed
# document", which is a different question once the two have parted.


def _change_the_source_leaving_the_cited_span_alone(session) -> tuple[str, str]:
    """Rewrite v2 far from the cited offsets, same length. Returns (before, after)."""
    version = session.get(DocumentVersion, "v2")
    original = version.source_text
    start, _end = V2_OCCURRENCE_0
    edited = original[:100] + "X" * 20 + original[120:]
    assert len(edited) == len(original)
    assert edited[start:] == original[start:], "the edit must not touch the quote"
    version.source_text = edited
    session.flush()
    return original, edited


def test_a_version_whose_text_no_longer_matches_its_hash_is_refused():
    # The demonstration, as a test. Mint a citation against v2, verify it, then
    # overwrite twenty characters near the top. The quote is untouched
    # and still sits at the cited offsets, so every check the verifier ran
    # before this one still passes -- and the document is no longer the document
    # that was ingested.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        start, end = V2_OCCURRENCE_0
        citation = Citation("v2", start, end, BOILERPLATE)
        assert verify_citation_for_version(
            session, citation, "MEP", expected_occurrence=0
        ).verified

        _change_the_source_leaving_the_cited_span_alone(session)

        result = verify_citation_for_version(
            session, citation, "MEP", expected_occurrence=0
        )
        assert result.verified is False
        assert result.reason == REASON_VERSION_CHANGED
        # No excerpt. Bytes from a version we cannot vouch for are not evidence,
        # and showing them beside a refusal invites the reader to read them as
        # the source.
        assert result.actual_text is None


def test_the_two_argument_form_still_passes_the_changed_source_it_cannot_see():
    # Stated so the gap is a fact in the suite rather than an inference. The
    # string form has no version row and therefore no hash to check; it is the
    # caller's job to hand it the right text, and it cannot tell that the text
    # has drifted from what was ingested.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        _before, after = _change_the_source_leaving_the_cited_span_alone(session)
        start, end = V2_OCCURRENCE_0
        result = verify_citation(
            Citation("v2", start, end, BOILERPLATE), after, expected_occurrence=0
        )
        assert result.verified is True


def test_a_changed_version_is_not_reported_as_a_quote_that_did_not_match():
    # Distinct reasons, because they call for different work. A quote mismatch
    # is one claim to re-read. A version that has drifted from its hash is every
    # offset in the corpus derived from it, and re-writing the claim would not
    # fix it.
    assert REASON_VERSION_CHANGED != REASON_QUOTE_MISMATCH
    assert REASON_VERSION_CHANGED != REASON_VERSION_UNREADABLE

    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        version = session.get(DocumentVersion, "v2")
        start, end = V2_OCCURRENCE_0
        # This edit lands on the cited span itself, so the quote no longer
        # matches either. The drift is reported, not the mismatch.
        version.source_text = (
            version.source_text[:start]
            + "Z" * (end - start)
            + version.source_text[end:]
        )
        session.flush()

        result = verify_citation_for_version(
            session, Citation("v2", start, end, BOILERPLATE), "MEP", expected_occurrence=0
        )
        assert result.reason == REASON_VERSION_CHANGED


def test_an_untouched_version_is_not_reported_as_changed():
    # The tripwire has to be silent on the corpus as loaded, or it is a fallback
    # that announces itself on every read and gets switched off.
    init_db()
    with session_scope() as session:
        _seed_a_proceeding(session)
        for version_id, (start, end) in (("v1", V1_OCCURRENCE_0), ("v2", V2_OCCURRENCE_0)):
            result = verify_citation_for_version(
                session,
                Citation(version_id, start, end, BOILERPLATE),
                "MEP",
                expected_occurrence=0,
            )
            assert result.verified is True, version_id


# --- the boundary test that replaced a re-normalization per hit --------------
#
# _spans_of used to re-normalize every hit's raw span to prove the hit covered
# whole source characters. That is exact, and it costs O(m) per hit while hits
# scale with the source, so the function was O(n*m) under a docstring promising
# O(n). The projection already carries the answer: characters produced by one
# raw character share both offsets, so a hit begins or ends part-way through an
# expansion exactly when it shares an offset with its neighbour.
#
# The tests below measure that claim rather than repeating it. They found it
# sound but not equivalent -- see the pinned case further down -- which is why
# the re-read survives as a fallback instead of being deleted.

_ADVERSARIAL_POOL = list("abc 12.-_\n\t\f ") + [
    "ﬁ",  # fi ligature: one raw character, two normalized
    "㎡",  # squared metre: one raw character, "m2"
    "­",  # soft hyphen: deleted, so the characters either side join
    "—",  # em dash
    "“",
    "”",
    " ",  # no-break space
    "　",  # ideographic space
    "²",  # superscript two: never folded, it is a footnote marker
    "½",  # one half: never folded
    "①",  # circled one: a marker, never folded
    "́",  # combining acute, bare
    # These fold to a space FOLLOWED BY a combining mark. Fifty characters in
    # Unicode do. They are the shape that separates the boundary test from the
    # re-read, and a pool without them proves nothing about the difference.
    "¨",
    "´",
    "ͺ",
    "΅",
    "‾",
    "゛",
]


def _random_pairs(seed: int, trials: int):
    """(source, quote) pairs. Most quotes are cut from the source, so they hit."""
    rng = random.Random(seed)
    for _ in range(trials):
        source = "".join(
            rng.choice(_ADVERSARIAL_POOL) for _ in range(rng.randint(1, 40))
        )
        if len(source) > 2 and rng.random() < 0.6:
            start = rng.randrange(len(source) - 1)
            quote = source[start : rng.randrange(start + 1, len(source) + 1)]
        else:
            quote = "".join(
                rng.choice(_ADVERSARIAL_POOL) for _ in range(rng.randint(1, 6))
            )
        yield source, quote


def _spans_by_re_reading(quoted_text: str, source_text: str) -> list[tuple[int, int]]:
    """The check _spans_of used to run: re-normalize each hit's raw span.

    Exact and slow, and kept here rather than in the module so the fast path
    cannot be marked correct by agreeing with itself.
    """
    needle = normalize(quoted_text)
    if not needle:
        return []
    projection = normalized_projection(source_text)
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    position = projection.text.find(needle)
    while position != -1:
        span = projection.raw_span(position, position + len(needle))
        if span not in seen:
            seen.add(span)
            if normalize(source_text[span[0] : span[1]]) == needle:
                spans.append(span)
        position = projection.text.find(needle, position + 1)
    return spans


def test_the_boundary_test_never_clears_a_hit_the_re_read_would_reject():
    """Soundness of the fast path, measured over randomized adversarial text.

    This is the only property the fallback needs. A hit the boundary test
    clears is returned without being re-read, so if the test could clear a hit
    that does not really normalize to the quote, _spans_of would report a span
    that is not an occurrence -- and occurrence_index would then bless the wrong
    one.
    """
    cleared = 0
    rescued = 0
    for source, quote in _random_pairs(20260804, 3000):
        needle = normalize(quote)
        if not needle:
            continue
        projection = normalized_projection(source)
        position = projection.text.find(needle)
        while position != -1:
            end = position + len(needle)
            span = projection.raw_span(position, end)
            re_read = normalize(source[span[0] : span[1]]) == needle
            if _covers_whole_characters(projection, position, end):
                assert re_read, (
                    f"the boundary test cleared a hit the re-read rejects: "
                    f"source={source!r} quote={quote!r} at {position}"
                )
                cleared += 1
            elif re_read:
                rescued += 1
            position = projection.text.find(needle, position + 1)
    assert cleared > 1000, f"only {cleared} hits reached the assertion; pool is too tame"
    # And the fallback is not dead code: these are the hits the boundary test
    # refuses and the re-read keeps. Delete the re-read and _spans_of loses
    # every one of them.
    assert rescued > 0


def test_spans_match_the_re_reading_implementation_over_randomized_sources():
    # The whole function, not just the check: same spans, same order, on every
    # pair. This is what makes the boundary test a speed change rather than a
    # behaviour change.
    for source, quote in _random_pairs(913, 3000):
        assert _spans_of(quote, source) == _spans_by_re_reading(quote, source), (
            f"source={source!r} quote={quote!r}"
        )


def test_the_boundary_test_is_stricter_than_the_re_read_and_the_fallback_covers_it():
    """The case that stopped the boundary test from replacing the re-read.

    U+00A8 folds to a space and a combining diaeresis: one raw character, two
    normalized ones sharing both offsets. A quote starting at the mark therefore
    begins part-way through an expansion, and the boundary test refuses it. The
    re-read accepts it, because normalize() strips the leading space of a
    substring and the raw span really does normalize to the quote.

    Fifty characters in Unicode fold this way -- the spacing accents. Dropping
    the fallback would lose every occurrence they start, and a lost occurrence
    turns the repeated-boilerplate guard OFF rather than on. That direction is
    the one this codebase does not accept.
    """
    source = "A¨B"
    quote = "̈B"
    projection = normalized_projection(source)
    position = projection.text.find(normalize(quote))

    assert position == 2
    assert _covers_whole_characters(projection, position, position + 2) is False
    assert _spans_of(quote, source) == [(1, 3)]
    assert normalize(source[1:3]) == normalize(quote)


def test_a_quote_starting_mid_expansion_is_still_refused():
    # The squared-metre case the check exists for, pinned. "2" appears in the
    # normalized text of a document whose only "2" comes from the squared-metre
    # glyph -- and there is no raw span for half a character, so it is not an
    # occurrence of "2".
    source = "each site of 500 ㎡ or larger"
    assert "2" in normalize(source)
    assert occurrence_count("2", source) == 0
    assert occurrence_count("m", source) == 0
    assert occurrence_count("m2", source) == 1


def test_a_repeated_rule_line_does_not_re_normalize_once_per_hit(monkeypatch):
    """The cost, as a count rather than a stopwatch.

    A rule line or a table-of-contents leader is a filing full of one repeated
    character, and a short quote against it hits on nearly every offset. Under
    the re-normalizing check those 143,991 hits cost 143,991 normalizations of
    ten characters each, which is what made the function scale with the quote's
    length as well as the source's. Counting calls pins that without asking a
    test machine to be fast.
    """
    lengths: list[int] = []
    real = verifier.normalize

    def counting(text: str) -> str:
        lengths.append(len(text))
        return real(text)

    monkeypatch.setattr(verifier, "normalize", counting)

    assert occurrence_count("_" * 10, "_" * 144_000) == 143_991
    # One call, over the quote. Every hit was settled by the boundary test.
    assert lengths == [10]
