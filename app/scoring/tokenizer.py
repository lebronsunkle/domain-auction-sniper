"""
Respelling-aware tokenizer.

Splits a domain SLD into recognized tokens, attempting both straight dictionary
matches AND respelling-substituted matches. This is the piece that catches
The client's preferred respelling patterns (lite, rite, nite, tek, fone, k-initial,
z-plural) that a naive dictionary check would miss entirely.

The substitution table is calibrated to the client's actual portfolio — see
the analysis report for how these were derived from his 19k domain corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Substitution rules: (pattern, replacement, label).
# Applied in order during respelling normalization. Patterns are regex against
# the lowercased SLD.
RESPELLING_RULES: list[tuple[str, str, str]] = [
    # The client's dominant patterns from the portfolio analysis:
    (r"lite", "light", "lite_light"),
    (r"rite", "right", "rite_right"),
    (r"nite", "night", "nite_night"),
    (r"\btru\b", "true", "tru_true"),
    (r"\bthru\b", "through", "thru_through"),
    (r"fone", "phone", "fone_phone"),
    (r"foto", "photo", "foto_photo"),
    (r"\bkw", "qu", "kw_qu"),
    (r"\bx(?=[a-z])", "ex", "x_ex_prefix"),
    (r"\btek\b", "tech", "tek_tech"),
    # Less common but present:
    (r"oyz\b", "oys", "z_oys"),
    (r"idz\b", "ids", "z_ids"),
    (r"z\b", "s", "z_s_final"),
    # k-for-c at word boundary (more restrictive to avoid false positives):
    (r"\bk(?=[aeiou])", "c", "k_c_initial"),
]


@dataclass
class TokenizeResult:
    """Outcome of running the tokenizer on a domain SLD."""

    sld: str
    direct_tokens: list[str]  # dictionary matches against the raw SLD
    respelled_tokens: list[str]  # dictionary matches after respelling normalization
    respelling_patterns_used: list[str]  # labels of rules that produced new matches

    @property
    def all_word_tokens(self) -> list[str]:
        # Dedup while preserving order.
        seen = set()
        out = []
        for t in self.direct_tokens + self.respelled_tokens:
            if t not in seen:
                out.append(t)
                seen.add(t)
        return out

    @property
    def has_any_word_match(self) -> bool:
        return bool(self.direct_tokens or self.respelled_tokens)

    @property
    def uses_preferred_respelling(self) -> bool:
        return bool(self.respelling_patterns_used)


# A small set of two-letter words that the dictionary may miss but that we want
# to recognize for domain tokenization (common in business names).
SHORT_ALLOWED: frozenset[str] = frozenset({
    "go", "my", "up", "to", "at", "by", "do", "in", "on", "or",
    "of", "an", "it", "is", "be", "we", "us", "no",
})


class Tokenizer:
    """Greedy left-to-right tokenizer.

    Caller provides the dictionary (set of lowercase words). Recommended dictionary
    is the CMU pronunciation dictionary in /usr/share/pocketsphinx/, available on
    most Linux hosts; failing that, any English wordlist.
    """

    def __init__(self, wordlist: set[str], min_word_len: int = 3, max_word_len: int = 18):
        self.wordlist = wordlist
        self.min_word_len = min_word_len
        self.max_word_len = max_word_len

    def tokenize(self, sld: str) -> TokenizeResult:
        sld = sld.lower()
        # Strip to alphabetic only for tokenization; digits/hyphens are handled
        # separately by the scoring engine.
        alpha = re.sub(r"[^a-z]", "", sld)

        direct = self._greedy_split(alpha)

        # Try respelling normalization. Track which patterns produced *new* word
        # matches (didn't already exist in the direct split).
        respelled = []
        patterns_used = []
        direct_set = set(direct)
        for pattern, repl, label in RESPELLING_RULES:
            if not re.search(pattern, alpha):
                continue
            transformed = re.sub(pattern, repl, alpha)
            if transformed == alpha:
                continue
            new_tokens = self._greedy_split(transformed)
            # New tokens that weren't in the direct split — these are signals
            # that the respelling matters.
            novel = [t for t in new_tokens if t not in direct_set]
            if novel:
                patterns_used.append(label)
                for t in novel:
                    if t not in respelled:
                        respelled.append(t)

        return TokenizeResult(
            sld=sld,
            direct_tokens=direct,
            respelled_tokens=respelled,
            respelling_patterns_used=patterns_used,
        )

    def _greedy_split(self, s: str) -> list[str]:
        """Longest-match-first greedy split into dictionary words."""
        out = []
        i = 0
        n = len(s)
        while i < n:
            matched: str | None = None
            # try longest first
            for L in range(min(self.max_word_len, n - i), self.min_word_len - 1, -1):
                cand = s[i : i + L]
                if cand in self.wordlist:
                    matched = cand
                    break
            if not matched:
                # fall back to short-allowed 2-letter words
                cand = s[i : i + 2]
                if cand in SHORT_ALLOWED:
                    matched = cand
            if matched:
                out.append(matched)
                i += len(matched)
            else:
                i += 1
        return out


def load_default_wordlist() -> set[str]:
    """Load a default wordlist from common system paths.

    Order of preference:
      1. /usr/share/dict/american-english (wamerican package)
      2. /usr/share/dict/words (generic)
      3. /usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict (CMU pronouncing dict)

    Returns an empty set if none of these are available — caller should detect
    that and ship a fallback or fail loudly.
    """
    import os

    paths = [
        "/usr/share/dict/american-english",
        "/usr/share/dict/words",
        "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict",
    ]
    for path in paths:
        if not os.path.exists(path):
            continue
        wordlist: set[str] = set()
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                w = line.split()[0] if line.split() else ""
                # cmudict has variant suffixes like word(2)
                w = re.sub(r"\(\d+\)$", "", w).lower()
                if w.isalpha() and 2 <= len(w) <= 18:
                    wordlist.add(w)
        if wordlist:
            return wordlist
    return set()
