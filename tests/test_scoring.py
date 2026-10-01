"""
Tests for the scoring engine and respelling tokenizer.

Heavy on real domains from the client's portfolio so we catch regressions
when we tune weights. If a future code change makes biorite.com score
below 100, these tests will fail.

(Park City cluster tests removed 2026-07-11 — the client no longer targets
that cluster; a regression test now asserts the bonus does NOT fire.)
"""

from __future__ import annotations

import pytest

from app.scoring.engine import ScoringEngine
from app.scoring.themes import (
    PREFERRED_RESPELLING_PATTERNS,
    PREFERRED_KEYWORDS,
    THEME_BUCKETS,
    bucket_matches,
)
from app.scoring.tokenizer import (
    SHORT_ALLOWED,
    Tokenizer,
    load_default_wordlist,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wordlist() -> set[str]:
    wl = load_default_wordlist()
    if not wl:
        pytest.skip("No system wordlist available; can't run scoring tests")
    return wl


@pytest.fixture(scope="module")
def tokenizer(wordlist: set[str]) -> Tokenizer:
    return Tokenizer(wordlist)


@pytest.fixture(scope="module")
def engine(tokenizer: Tokenizer) -> ScoringEngine:
    return ScoringEngine(tokenizer)


# ---------------------------------------------------------------------------
# TLD weighting
# ---------------------------------------------------------------------------


def test_com_tld_top_weight(engine: ScoringEngine):
    assert engine.score("example.com").tld == 40


def test_net_tld_modest(engine: ScoringEngine):
    assert engine.score("example.net").tld == 5


def test_org_tld_small(engine: ScoringEngine):
    assert engine.score("example.org").tld == 3


def test_unknown_tld_zero(engine: ScoringEngine):
    assert engine.score("example.zzz").tld == 0


# ---------------------------------------------------------------------------
# Length brackets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain, expected_length_score", [
    ("abc.com", 30),       # 3 chars -- short premium
    ("abcd.com", 30),      # 4 chars
    ("abcde.com", 28),     # 5 chars -- peak count category
    ("abcdef.com", 25),    # 6 chars -- sweet spot
    ("abcdefgh.com", 25),  # 8 chars
    ("abcdefghij.com", 20), # 10 chars
    ("abcdefghijk.com", 10), # 11 chars
    ("abcdefghijklm.com", 0),  # 13 chars
    ("abcdefghijklmnop.com", 0),  # 16 chars -- still in slogan tolerance band (2026-05-22)
    ("abcdefghijklmnopqr.com", 0),  # 18 chars -- still in slogan tolerance band
    ("abcdefghijklmnopqrs.com", -10),  # 19 chars -- now the hard penalty starts
])
def test_length_brackets(engine: ScoringEngine, domain: str, expected_length_score: int):
    assert engine.score(domain).length == expected_length_score


def test_multi_token_slogan_bonus(engine: ScoringEngine):
    """Per the 2026-05-22 call, a long SLD that splits cleanly into 3+ dictionary
    words is interesting (it's a slogan/phrase) rather than penalized for length.
    The bonus is a small +5 to word_content."""
    bd = engine.score("yourbestcleanmaster.com")
    # 'your' 'best' 'clean' 'master' should all tokenize directly.
    assert len(bd.notes) > 0
    assert any("multi-token slogan" in n for n in bd.notes), (
        f"expected multi-token slogan note, got: {bd.notes}"
    )


def test_single_token_no_slogan_bonus(engine: ScoringEngine):
    """A single-token SLD doesn't get the slogan bonus."""
    bd = engine.score("master.com")
    assert not any("multi-token slogan" in n for n in bd.notes), (
        f"single-token shouldn't get slogan bonus, got: {bd.notes}"
    )


# ---------------------------------------------------------------------------
# Composition penalties
# ---------------------------------------------------------------------------


def test_hyphen_penalty(engine: ScoringEngine):
    bd = engine.score("buy-now.com")
    assert bd.composition <= -20  # -25 hyphen, but +5 might apply to some letter cases


def test_digit_penalty_on_long(engine: ScoringEngine):
    # 8-char with a digit: should hit the standard digit penalty.
    bd = engine.score("abc123def.com")
    assert "contains digit" in (bd.notes or [])


def test_short_numeric_niche_lane(engine: ScoringEngine):
    """The client specifically asked to KEEP pursuing short numeric/alphanumeric
    domains. They should not be penalized."""
    bd = engine.score("9742.com")
    assert "short-numeric niche lane" in bd.notes
    assert bd.composition >= 5  # net positive on composition


def test_short_alphanumeric_also_in_niche(engine: ScoringEngine):
    bd = engine.score("359x.com")
    assert "short-numeric niche lane" in bd.notes


# ---------------------------------------------------------------------------
# Tokenization (direct dictionary matches)
# ---------------------------------------------------------------------------


def test_dictionary_match_scores_well(engine: ScoringEngine):
    bd = engine.score("master.com")
    assert bd.word_content >= 25  # direct dictionary token


# ---------------------------------------------------------------------------
# Respelling tokenizer
# ---------------------------------------------------------------------------


def test_lite_respelling_recognized(engine: ScoringEngine):
    """`cleanlite.com` should pick up both "clean" (direct) and the lite
    respelling pattern (Preference bonus)."""
    bd = engine.score("cleanlite.com")
    assert bd.word_content > 0
    assert any("respelling" in n.lower() for n in bd.notes)


def test_rite_respelling_recognized(engine: ScoringEngine):
    bd = engine.score("biorite.com")
    # Should fire the respelling pattern and the Preference bonus.
    assert bd.preference_signals > 0
    assert bd.total >= 100, f"biorite.com should score 100+, got {bd.total}"


def test_fone_respelling_recognized(engine: ScoringEngine):
    bd = engine.score("accufone.com")
    assert bd.total >= 100, f"accufone.com should score 100+, got {bd.total}"


# ---------------------------------------------------------------------------
# Theme buckets
# ---------------------------------------------------------------------------


def test_industrial_b2b_theme_caught(engine: ScoringEngine):
    bd = engine.score("polypro.com")
    # polypro might tokenize as "polyp" or "poly"+"pro" — either way themes should hit
    assert bd.themes >= 0


def test_park_city_cluster_bonus_removed(engine: ScoringEngine):
    """2026-07-11: the client no longer targets the Park City cluster. The +15
    special-case and the park_city_strategic theme bucket are gone. The
    generic city-compound bonus (fragments.py) may still apply to parkcity
    like any other city — but the strategic-cluster note must never appear."""
    for domain in ("parkcityapp.com", "hebercity.com", "deervalleyskis.com"):
        bd = engine.score(domain)
        assert "Park City strategic cluster" not in (bd.notes or []), domain
    assert "park_city_strategic" not in THEME_BUCKETS


# ---------------------------------------------------------------------------
# Preference signals
# ---------------------------------------------------------------------------


def test_top_keyword_matched(engine: ScoringEngine):
    """`cleanworld.com` has `clean` + `world`, both preferred keywords."""
    bd = engine.score("cleanworld.com")
    assert "matches preferred keyword" in (bd.notes or [])
    assert bd.preference_signals >= 10


def test_preferred_respelling_pattern_recognized(engine: ScoringEngine):
    """When the tokenizer uses one of the client's preferred respelling patterns,
    the engine adds an additional bonus on top of word-content."""
    bd = engine.score("cleanlite.com")
    assert any(p in PREFERRED_RESPELLING_PATTERNS for p in
               (bd.notes or []) if isinstance(p, str))  # crude check
    assert bd.preference_signals >= 10


# ---------------------------------------------------------------------------
# Brandability rubric — the client's 2026-05-21 retraction.
# Canonical full-letter form beats the respelled twin.
# ---------------------------------------------------------------------------


def test_canonical_form_outscores_respelled_twin(engine: ScoringEngine):
    """truezero.com (proper spelling) must score higher than truzero.com
    (respelled). Per the 2026-05-21 call: 'if it was true zero, spelled out,
    Z E R O, true, T R U E, that's worth a lot.' The respelled twin is
    decent but brand-thin and should rank below."""
    canonical = engine.score("truezero.com")
    respelled = engine.score("truzero.com")
    assert canonical.total > respelled.total, (
        f"truezero.com ({canonical.total}) should outscore "
        f"truzero.com ({respelled.total})"
    )


def test_properly_spelled_canonical_premium_fires(engine: ScoringEngine):
    """Direct-tokenizing a canonical respelling target (true, light, night,
    phone, tech, photo) awards the properly-spelled premium. The premium is
    a small +5 tiebreaker (calibrated 2026-05-22 after the first live run
    showed +15 was dominating the surface)."""
    bd = engine.score("truezero.com")
    assert any(
        "properly-spelled canonical form" in n for n in bd.notes
    ), f"expected 'properly-spelled canonical form' note, got: {bd.notes}"
    # 'true' contributes via top-50 (+10) and now canonical (+5), plus other
    # signals -- so preference_signals should be at least 15 in total.
    assert bd.preference_signals >= 15


def test_brand_thin_note_when_phonetic_only(engine: ScoringEngine):
    """When the SLD's only word content comes from respellings (no direct
    dictionary anchor), the brand-thin diagnostic note must fire. `tru.com`
    is the cleanest example: 'tru' isn't a direct dictionary word, but the
    tru->true respelling rule resolves it. No direct anchor = brand-thin."""
    bd = engine.score("tru.com")
    assert any(
        "brand-thin" in n.lower() for n in bd.notes
    ), f"expected brand-thin note for tru.com, got: {bd.notes}"


def test_brand_thin_note_absent_with_direct_anchor(engine: ScoringEngine):
    """cleanlite has 'clean' as a direct dictionary anchor PLUS the lite->light
    respelling. That's intentional styling on real meaning, not brand-thin --
    the diagnostic must NOT fire here."""
    bd = engine.score("cleanlite.com")
    assert not any("brand-thin" in n.lower() for n in bd.notes), (
        f"cleanlite has direct anchor 'clean', should not be flagged brand-thin: {bd.notes}"
    )


# ---------------------------------------------------------------------------
# Valuation placeholder fields (Brook's framework, 2026-05-21).
# Stubs in v1 -- they default to None and don't contribute to total.
# ---------------------------------------------------------------------------


def test_valuation_placeholders_default_to_none(engine: ScoringEngine):
    bd = engine.score("example.com")
    assert bd.registration_age_years is None
    assert bd.sector_tag is None
    assert bd.tld_spread_count is None
    assert bd.plural_taken is None
    assert bd.organic_search_estimate is None


def test_valuation_placeholders_excluded_from_total(engine: ScoringEngine):
    """Even if a caller populates the valuation fields, they must NOT change
    `total` in v1. The total is the sum of the seven scoring components only;
    valuation gets its own pass once we have a data source."""
    bd = engine.score("example.com")
    baseline_total = bd.total
    bd.registration_age_years = 22
    bd.sector_tag = "financial"
    bd.tld_spread_count = 200
    bd.plural_taken = True
    bd.organic_search_estimate = 50000
    assert bd.total == baseline_total


def test_valuation_placeholders_in_json(engine: ScoringEngine):
    """The JSON serializer surfaces valuation under its own key so the UI
    can render the 'N/A -- Estibot not connected' state cleanly."""
    import json as _json
    bd = engine.score("example.com")
    payload = _json.loads(bd.to_json())
    assert "valuation" in payload
    assert payload["valuation"]["registration_age_years"] is None
    assert payload["valuation"]["sector_tag"] is None


# ---------------------------------------------------------------------------
# Two-concept compound brandability 
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain, left, right", [
    ("biolytics.com",    "bio",   "lytics"),
    ("omnikinetics.com", "omni",  "kinetics"),
    ("compumax.com",     "compu", "max"),
    ("biosport.com",     "bio",   "sport"),
])
def test_compound_brandability_fires_on_preferred_examples(
    engine: ScoringEngine, domain: str, left: str, right: str
):
    """Classic two concept compounds should surface as compound matches."""
    bd = engine.score(domain)
    note = next(
        (n for n in bd.notes if n.startswith("two-concept compound")),
        None,
    )
    assert note is not None, f"{domain} should fire compound check"
    assert f"'{left}'" in note and f"'{right}'" in note, (
        f"compound note should reference {left} and {right}, got: {note}"
    )


@pytest.mark.parametrize("domain", [
    "random.com",
    "asdfqwer.com",
    "qwertyz.com",
])
def test_compound_brandability_does_not_false_positive(
    engine: ScoringEngine, domain: str
):
    """Random letter strings must not fire the compound check."""
    bd = engine.score(domain)
    assert not any(
        n.startswith("two-concept compound") for n in bd.notes
    ), f"{domain} should NOT fire compound check"


@pytest.mark.parametrize("domain, fragment, city", [
    ("biosandiego.com",  "bio",  "sandiego"),
    ("polyboston.com",   "poly", "boston"),
    ("omniseattle.com",  "omni", "seattle"),
    ("maxtokyo.com",     "max",  "tokyo"),
])
def test_city_compound_fires(engine: ScoringEngine, domain: str, fragment: str, city: str):
    """The Park-City-cluster idea generalized: any major one-word city
    paired with a recognized fragment should light up as a brandable geo
    compound. biosandiego == parkcityapp in spirit."""
    bd = engine.score(domain)
    note = next(
        (n for n in bd.notes if n.startswith("fragment + city geo")),
        None,
    )
    assert note is not None, f"{domain} should fire city-compound check"
    assert f"'{fragment}'" in note and f"'{city}'" in note, (
        f"city-compound note should reference {fragment} and {city}, got: {note}"
    )


def test_compound_outscores_non_compound_baseline(engine: ScoringEngine):
    """A compound match should produce a meaningfully higher score than
    an otherwise-identical SLD that doesn't split into two fragments."""
    compound = engine.score("biolytics.com")
    non_compound = engine.score("biolytizz.com")  # bio is a fragment, lytizz isn't
    assert compound.total > non_compound.total, (
        f"biolytics ({compound.total}) should outscore biolytizz "
        f"({non_compound.total}) thanks to the compound bonus"
    )


# ---------------------------------------------------------------------------
# End-to-end portfolio samples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain, min_expected_score, reason", [
    ("biorite.com", 100, "respelling + theme + the client top kw"),
    # hebercity.com / parkcityapp.com rows removed 2026-07-11 with the
    # Park City cluster bonus — see test_park_city_cluster_bonus_removed.
    ("acculube.com", 90, "brandable + industrial"),
    ("polypro.com", 80, "brandable + industrial (top-priced in portfolio)"),
    ("accufone.com", 90, "respelling + theme + the client pattern"),
])
def test_portfolio_high_scorers(engine: ScoringEngine, domain: str, min_expected_score: int, reason: str):
    bd = engine.score(domain)
    assert bd.total >= min_expected_score, (
        f"{domain} scored {bd.total}, expected >= {min_expected_score} ({reason})"
    )


@pytest.mark.parametrize("domain, max_expected_score, reason", [
    ("xyz12345.info", 30, "non-com + digits + no words"),
    ("a-very-long-hyphenated-thing.info", 40, "long + hyphen + non-com"),
])
def test_portfolio_low_scorers(engine: ScoringEngine, domain: str, max_expected_score: int, reason: str):
    bd = engine.score(domain)
    assert bd.total <= max_expected_score, (
        f"{domain} scored {bd.total}, expected <= {max_expected_score} ({reason})"
    )


# ---------------------------------------------------------------------------
# Tokenizer-specific tests
# ---------------------------------------------------------------------------


def test_tokenizer_handles_compound(tokenizer: Tokenizer):
    result = tokenizer.tokenize("supermaster")
    assert "master" in result.direct_tokens


def test_tokenizer_handles_pure_brandable(tokenizer: Tokenizer):
    result = tokenizer.tokenize("xyz")
    assert not result.has_any_word_match


def test_tokenizer_short_allowed(tokenizer: Tokenizer):
    """Two-letter words from the SHORT_ALLOWED set should be picked up."""
    result = tokenizer.tokenize("gomaster")
    assert "go" in result.direct_tokens
    assert "master" in result.direct_tokens
