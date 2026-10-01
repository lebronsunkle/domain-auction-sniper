"""SP flag — the client's misspelling detector (2026-08-28).

The client bought a misspelled domain for $60: "you see it all the time, you
don't even question it." This module flags names that are ONE TYPO away
from a real word or clean compound — "domian" -> domain, "recieve" ->
receive — so the dashboard can hang an amber SP chip on them and the client
double-checks before money moves.

Deliberately conservative (false positives would train him to ignore it):

  * brandables ("civiar") are near NO dictionary word     -> no flag
  * clean dictionary compounds ("myhomebills")            -> no flag
  * The client's intentional respellings (lite/rite/tek/...)  -> no flag
  * only flags when one edit turns the name into real words

Dictionary: the system wordlist (wamerican in the Docker image), via the
scorer's loader. If no wordlist is available we return None for
everything — the flag silently disables rather than guessing.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterator, Optional

from .tokenizer import RESPELLING_RULES, SHORT_ALLOWED, load_default_wordlist

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_MAX_WORD_LEN = 18


@lru_cache(maxsize=1)
def _words() -> frozenset[str]:
    return frozenset(load_default_wordlist())


def _segment(alpha: str, words: frozenset[str], min_len: int = 3) -> tuple[list[str], list[str]]:
    """Greedy longest-first segmentation.

    Returns (matched_words, unmatched_runs). min_len=4 for SUGGESTION
    validation: the system dictionary is full of 3-letter junk ("com",
    "ian") that made garbage like "domian→comian" segment cleanly.
    """
    matched_out: list[str] = []
    out: list[str] = []
    run = ""
    i, n = 0, len(alpha)
    while i < n:
        matched: Optional[str] = None
        for length in range(min(_MAX_WORD_LEN, n - i), min_len - 1, -1):
            cand = alpha[i : i + length]
            if cand in words:
                matched = cand
                break
        if matched is None and alpha[i : i + 2] in SHORT_ALLOWED:
            matched = alpha[i : i + 2]
        if matched:
            if run:
                out.append(run)
                run = ""
            matched_out.append(matched)
            i += len(matched)
        else:
            run += alpha[i]
            i += 1
    if run:
        out.append(run)
    return matched_out, out


def _residues(alpha: str, words: frozenset[str]) -> list[str]:
    return _segment(alpha, words)[1]


def _edit1(token: str) -> Iterator[str]:
    """All strings one edit away (Norvig-style): delete/transpose/sub/insert."""
    splits = [(token[:i], token[i:]) for i in range(len(token) + 1)]
    for a, b in splits:
        if b:
            yield a + b[1:]
        if len(b) > 1:
            yield a + b[1] + b[0] + b[2:]
        for c in _ALPHABET:
            if b:
                yield a + c + b[1:]
            yield a + c + b


def check_spelling(domain: str) -> Optional[str]:
    """Return "typo→intended" when the SLD looks misspelled, else None."""
    words = _words()
    if not words:
        return None

    sld = domain.lower().split(".", 1)[0]
    alpha = re.sub(r"[^a-z]", "", sld)
    if len(alpha) < 4:
        return None

    matched, residues = _segment(alpha, words)

    # The client's respelling patterns are style, not typos — if every residue
    # is explained by a respelling rule, stay quiet. Any unexplained
    # residue, however small, is grounds to investigate: two-letter filler
    # matches ("do", "an") can chew a typo into tiny fragments, so length
    # is no proof of innocence ("domian" -> do + [mi] + an).
    unexplained = [
        r for r in residues
        if not any(re.search(p, r) for p, _repl, _label in RESPELLING_RULES)
    ]
    if residues and not unexplained:
        return None

    # "Clean" segmentations built ONLY from <=3-letter scraps are junk-dict
    # mirages ("domian" = dom + ian): treat them as suspicious, but flag
    # only when one edit yields a STRICTLY simpler interpretation.
    choppy = (not residues) and len(matched) >= 2 and all(len(w) <= 3 for w in matched)
    if not residues and not choppy:
        return None  # confidently clean word/compound

    # Pass 1: one edit on the FULL name that makes it segment cleanly
    # ("mydomian" -> "mydomain" = my + domain). Suggestion validation uses
    # min 4-letter words, and among ALL valid candidates prefers the one
    # with the FEWEST, LONGEST segments — "domain" (one word) beats
    # "comian" (com+ian junk) for domian.
    if len(alpha) <= 15:
        best_cand: Optional[str] = None
        best_key: Optional[tuple] = None
        for cand in _edit1(alpha):
            if len(cand) < 4:
                continue
            cand_matched, leftover = _segment(cand, words, min_len=4)
            if leftover or not cand_matched:
                continue
            if choppy and len(cand_matched) >= len(matched):
                continue  # must be strictly simpler than the scrap reading
            key = (len(cand_matched), -min(len(w) for w in cand_matched), cand)
            if best_key is None or key < best_key:
                best_key = key
                best_cand = cand
        if best_cand:
            return f"{sld}→{best_cand}"

    if choppy:
        return None  # scraps, but no better reading exists — leave it alone

    # Pass 2: one edit on an individual residue ("domian" -> "domain").
    for res in (r for r in unexplained if len(r) >= 4):
        best: Optional[str] = None
        for cand in _edit1(res):
            if len(cand) >= 4 and cand in words:
                if best is None or len(cand) > len(best):
                    best = cand
        if best:
            return f"{res}→{best}"
    return None
