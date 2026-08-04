from app.text.normalize import normalize, normalized_projection


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


# --- PDF extraction artefacts -------------------------------------------------


def test_deletes_the_soft_hyphen_without_leaving_a_gap():
    # A PDF carries U+00AD as a rendering hint. It is not a space and it is not
    # a hyphen, so the words either side of it must close up.
    assert normalize("main­tain") == "maintain"
    assert normalize("inter­con­nection") == "interconnection"
    assert normalize("years.­") == "years."


def test_a_soft_hyphen_does_not_strand_an_accent():
    # The mark must still belong to its letter, or a second pass through
    # normalize() would compose what the first left apart.
    once = normalize("re­́sume")
    assert normalize(once) == once


def test_a_hyphen_against_a_line_break_loses_the_break_and_keeps_the_hyphen():
    assert normalize("cost-\ncausation") == "cost-causation"
    assert normalize("cost-\n            causation") == "cost-causation"
    assert normalize("cost-\r\ncausation") == "cost-causation"
    # Every dash a PDF extractor emits at a line break, not just the ASCII one.
    assert normalize("cost‑\ncausation") == "cost-causation"


def test_a_true_word_break_is_not_rejoined():
    # This is the guess the module refuses to make. "demon-" at a line end is
    # more likely a broken word than a hyphenated one, and acting on "more
    # likely" is how a citation asserts a word nobody wrote. The hyphen stays,
    # the quote fails to match, and the claim goes to review.
    assert normalize("demon-\nstrate") == "demon-strate"
    assert normalize("demon-\nstrate") != "demonstrate"


def test_a_spaced_dash_is_left_alone():
    # The rule fires only when the break touches the hyphen. A dash used as
    # punctuation between words keeps its spacing.
    assert normalize("cost - \ncausation") == "cost - causation"
    assert normalize("cost —\ncausation") == "cost -causation"


# --- what NFKC is and is not allowed to fold ---------------------------------


def test_superscripts_and_subscripts_keep_their_value():
    # normalize("20<superscript two>") used to return "202": a footnote marker
    # silently became a digit of the number beside it. In tariff text that is a
    # changed quantity produced by the function whose job is to preserve one.
    assert normalize("20²") == "20²"
    assert normalize("20²") != "202"
    assert normalize("H₂O") == "H₂O"


def test_vulgar_fractions_are_not_expanded():
    assert normalize("½") == "½"
    assert normalize("2½ cents per kWh") == "2½ cents per kWh"
    assert normalize("½") != normalize("1/2")


def test_still_folds_the_compatibility_forms_that_carry_no_value():
    assert normalize("the ﬁrst oﬀer") == "the first offer"   # ligatures
    assert normalize("20 MW") == "20 MW"                          # no-break space
    assert normalize("２０ ＭＷ") == "20 MW"           # full-width
    assert normalize("30 ㎡") == "30 m2"                           # squared unit
    assert normalize("résume") == normalize("résume")       # composition


def test_the_protected_forms_survive_a_second_pass():
    for text in ("20²", "H₂O", "2½ cents", "½"):
        assert normalize(normalize(text)) == normalize(text)


# --- the projection the occurrence check depends on --------------------------


def test_the_projection_maps_every_character_back_to_its_source():
    raw = "  The  Utility­ shall\nmain­tain “records”  "
    projection = normalized_projection(raw)

    assert projection.text == normalize(raw)
    assert len(projection.starts) == len(projection.text)
    assert len(projection.ends) == len(projection.text)
    assert all(0 <= s < e <= len(raw) for s, e in zip(projection.starts, projection.ends))


def test_a_normalized_span_maps_back_to_the_raw_offsets_that_produced_it():
    raw = "SECTION 4.\n\nThe  Utility shall main­tain records for five (5) years."
    projection = normalized_projection(raw)
    needle = "The Utility shall maintain records"

    position = projection.text.find(needle)
    assert position != -1
    start, end = projection.raw_span(position, position + len(needle))

    # Raw offsets, not normalized ones: the slice is longer than the needle
    # because the source has a doubled space and a soft hyphen in it.
    assert raw[start:end] == "The  Utility shall main­tain records"
    assert end - start != len(needle)
    assert normalize(raw[start:end]) == needle


def test_a_collapsed_space_never_maps_to_an_empty_raw_span():
    # Regression. The whitespace run used to be closed at the next chunk's
    # start, which is one character too early when a single chunk folds to a
    # space followed by something else -- an ideographic space carrying a stray
    # combining mark, which bad PDF extraction emits. The space then claimed
    # the raw span (1, 1), so a reviewer asked to see the cited characters was
    # shown none, and the projection's own promise -- that every normalized
    # character names the source characters that produced it -- was false.
    raw = "A　́B"
    projection = normalized_projection(raw)

    assert projection.text == "A ́B"
    for index in range(len(projection.text)):
        start, end = projection.starts[index], projection.ends[index]
        assert start < end, f"character {index} claims the empty span ({start}, {end})"
    # The run is the whole chunk, because one chunk produced both the space and
    # the mark after it. Wider than strictly necessary, and honest: those are
    # the source characters the space came out of.
    assert (projection.starts[1], projection.ends[1]) == (1, 3)
    assert raw[1:3].startswith("　")


def test_every_projection_offset_is_a_real_non_empty_run():
    # The same invariant, over text that mixes every rule in the module.
    raw = (
        "  SECTION　4.\n\n\tThe  Utility  shall main­tain "
        "cost-\n   causation records suﬃcient for 2² years. "
    )
    projection = normalized_projection(raw)

    assert projection.text == normalize(raw)
    assert len(projection.starts) == len(projection.ends) == len(projection.text)
    assert all(0 <= s < e <= len(raw) for s, e in zip(projection.starts, projection.ends))
    # Offsets only ever move forward through the source.
    assert list(projection.starts) == sorted(projection.starts)
    assert list(projection.ends) == sorted(projection.ends)


def test_a_soft_hyphen_at_a_line_break_fails_closed_rather_than_rejoining():
    # A documented limit, pinned so it cannot change by accident. A PDF that
    # breaks a word with U+00AD and a newline leaves "main<soft>\ntain". The
    # soft hyphen is deleted, the newline still collapses to a space, and the
    # result is "main tain", which matches neither "maintain" nor anything
    # else. That is the wrong text, and it is the safe wrong text: the citation
    # fails to verify and goes to review. Consuming the break instead would
    # assert "maintain" -- a better guess, still a guess, and a guess that
    # fails open. ADR-003 says we fail toward review, so this stands.
    assert normalize("main­\ntain") == "main tain"
    assert normalize("main­\ntain") != "maintain"
    # Away from a line break the soft hyphen closes the word up, as it must.
    assert normalize("main­tain") == "maintain"


def test_the_folding_boundary_is_exactly_where_the_docstring_puts_it():
    # One test holding both halves of the rule, so neither can drift without
    # the other being read. Left column folds, right column does not, and the
    # difference is whether the character can carry a number's value.
    folds = {
        "ﬁ": "fi",        # ligature
        " ": " ",         # no-break space, between words
        "２": "2",         # full-width two: a digit written wide
        "㍲": "da",        # squared unit
        "№": "No",        # numero sign
    }
    for source, expected in folds.items():
        assert normalize(f"a{source}b") == f"a{expected}b", repr(source)

    for protected in ("²", "₂", "½", "⅓", "¹"):
        assert normalize(f"20{protected}") == f"20{protected}", repr(protected)


def test_a_span_outside_the_projection_is_refused_not_answered():
    projection = normalized_projection("20 MW")
    for start, end in ((0, 0), (-1, 3), (0, 99), (3, 2)):
        try:
            projection.raw_span(start, end)
        except ValueError:
            continue
        raise AssertionError(f"({start}, {end}) should not have been answered")
