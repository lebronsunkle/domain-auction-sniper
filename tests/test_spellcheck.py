"""SP flag tests (2026-08-28) — the $60 misspelled-domain lesson.

Uses a controlled dictionary so results don't depend on the host wordlist."""

import pytest

from app.scoring import spellcheck


FIXTURE_WORDS = frozenset({
    "domain", "home", "bills", "build", "ultra", "credit", "receive",
    "money", "pay", "light", "tech", "true", "night", "right", "photo",
    "phone", "world", "cloud", "market", "homes",
})


@pytest.fixture(autouse=True)
def _fixture_dict(monkeypatch):
    monkeypatch.setattr(spellcheck, "_words", lambda: FIXTURE_WORDS)


def test_flags_the_classic_typo():
    assert spellcheck.check_spelling("domian.com") == "domian→domain"


def test_flags_typo_inside_compound():
    out = spellcheck.check_spelling("mydomian.com")
    assert out is not None and "mydomain" in out


def test_brandable_is_not_flagged():
    # civiar is near no dictionary word — it's a made-up name, not a typo.
    assert spellcheck.check_spelling("civiar.com") is None


def test_clean_compound_is_not_flagged():
    assert spellcheck.check_spelling("myhomebills.com") is None


def test_preferred_respelling_is_not_flagged():
    # "lite" is the client's intentional style (homelite), never a typo flag.
    assert spellcheck.check_spelling("homelite.com") is None


def test_short_names_skipped():
    assert spellcheck.check_spelling("abc.com") is None


def test_empty_wordlist_disables_quietly(monkeypatch):
    monkeypatch.setattr(spellcheck, "_words", lambda: frozenset())
    assert spellcheck.check_spelling("domian.com") is None


def test_junk_dict_scraps_do_not_hide_typos(monkeypatch):
    """Prod dictionary contains junk 3-letter 'words' (com, ian, dom): they
    must not make 'domian' look like a clean compound, and suggestions must
    prefer 'domain' over garbage like 'comian'."""
    W = frozenset({
        "domain", "receive", "caviar", "home", "bills",
        "com", "ian", "reh", "dom", "mia",   # the junk
    })
    monkeypatch.setattr(spellcheck, "_words", lambda: W)
    assert spellcheck.check_spelling("domian.com") == "domian→domain"
    assert spellcheck.check_spelling("recieve.com") == "recieve→receive"
    assert spellcheck.check_spelling("myhomebills.com") is None


def test_legit_short_word_compound_not_flagged(monkeypatch):
    """car+fix is a real compound of short words — choppy but no simpler
    one-edit reading exists, so it stays clean."""
    W = frozenset({"car", "fix", "domain"})
    monkeypatch.setattr(spellcheck, "_words", lambda: W)
    assert spellcheck.check_spelling("carfix.com") is None
