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
