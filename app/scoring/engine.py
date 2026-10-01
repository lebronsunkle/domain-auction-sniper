"""
Scoring engine — applies the v2 rubric (plan section 5) to each auction listing.

Weights are calibrated to the client's actual portfolio composition. See
`portfolio-analysis.md` for the derivation.

Design principle — the client's brandability rubric ("sign, sound, sight, meaning"):
    Captured on the 2026-05-21 call. The client evaluates a domain the way a
    trademark examiner would, on four axes:

      sign    — how it reads as a written mark (length, composition, TLD).
                Surfaced via the `length`, `composition`, and `tld` components.
      sound   — how it speaks aloud (phonetic patterns, deliberate respellings).
                Surfaced via the respelling-aware tokenizer and the
                respelling-pattern signal.
      sight   — what mental image / sector it evokes (theme cluster fit).
                Surfaced via the `themes` component and Park City cluster check.
      meaning — what dictionary content the SLD actually carries.
                Surfaced via the `word_content` component.

    The 2026-05-21 call also clarified that the *properly-spelled* canonical
    form of a respelling target (truezero, cleanlight) is brandable, while the
    respelled twin alone (truzero, cleanlite) is "decent but brand-thin."
    See `contains_canonical_respelling_target` in themes.py.

    Valuation signals from Brook (registration age, sector tag, TLD spread,
    plural availability, organic search) are present on ScoreBreakdown as
    placeholder fields. They aren't summed into `total` in v1 because we
    don't have a data source for them yet — Estibot integration is the path
    to lighting them up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .fragments import (
    find_city_compound,
    find_compound_split,
    has_alliteration,
    max_fragment_concepts,
)
from .themes import (
    bucket_matches,
    contains_canonical_respelling_target,
    contains_preferred_keyword,
    PREFERRED_RESPELLING_PATTERNS,
)
from .tokenizer import Tokenizer, TokenizeResult


# Slogan-detection vocabulary. A domain that reads as a slogan vs. a
# buzzword-stack almost always contains at least one of these function words.
# "haulitpro" reads as "haul it pro" because of "it"; "maxprobook" doesn't
# — it's just three nouns crammed together. 2026-06-03 calibration: this
# set gates the multi-token slogan bonus so we stop pumping junk like
# bookpromax / suredocpro / maxprobook to the top of the rankings.
_SLOGAN_CONNECTORS = frozenset({
    # articles
    "a", "an", "the",
    # pronouns
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "us", "them",
    "my", "your", "his", "her", "its", "our", "their",
    "mine", "yours", "ours", "theirs",
    # short prepositions
    "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "into", "onto", "out", "off", "up", "down", "over", "under",
    # conjunctions
    "and", "or", "but", "so", "yet", "nor", "if",
    # interrogatives
    "why", "what", "when", "where", "how", "who", "which", "whose",
    # common aux/copula verbs
    "is", "are", "was", "were", "be", "am", "been", "being",
    "do", "did", "does", "done",
    "can", "will", "would", "should", "could", "may", "might", "must",
    "has", "have", "had",
    # negations
    "not", "no",
    # "let" / "lets" appear in plenty of taglines
    "let", "lets",
})


@dataclass
class ScoreBreakdown:
    """Granular score components, so we can show 'why' in the UI and tune later.

    Components that contribute to `total`:
      tld, length, composition, word_content, themes, preference_signals, demand.

    Valuation placeholder fields (Brook's framework, captured 2026-05-21) —
    do NOT contribute to `total` in v1. They surface in the UI as "N/A —
    Estibot not connected" until we wire a valuation data source. Once
    populated, they'll feed a separate `valuation_score` component and
    be folded into total in a tuning pass:

      registration_age_years    — older registrations signal brand stability
      sector_tag                — industry classification (financial, medical, hobby...)
      tld_spread_count          — how many TLDs of this SLD are already taken
      plural_taken              — whether the plural form is already registered
      organic_search_estimate   — searches per month for the SLD as a query
    """

    tld: int = 0
    length: int = 0
    composition: int = 0
    word_content: int = 0
    themes: int = 0
    preference_signals: int = 0
    demand: int = 0
    notes: list[str] = field(default_factory=list)

    # --- Valuation placeholders (not summed into total in v1) ---
    registration_age_years: Optional[int] = None
    sector_tag: Optional[str] = None
    tld_spread_count: Optional[int] = None
    plural_taken: Optional[bool] = None
    organic_search_estimate: Optional[int] = None

    @property
    def total(self) -> int:
        return self.tld + self.length + self.composition + self.word_content + self.themes + self.preference_signals + self.demand

    def to_json(self) -> str:
        return json.dumps({
            "tld": self.tld,
            "length": self.length,
            "composition": self.composition,
            "word_content": self.word_content,
            "themes": self.themes,
            "preference_signals": self.preference_signals,
            "demand": self.demand,
            "total": self.total,
            "notes": self.notes,
            "valuation": {
                "registration_age_years": self.registration_age_years,
                "sector_tag": self.sector_tag,
                "tld_spread_count": self.tld_spread_count,
                "plural_taken": self.plural_taken,
                "organic_search_estimate": self.organic_search_estimate,
            },
        })


class ScoringEngine:
    def __init__(self, tokenizer: Tokenizer):
        self.tokenizer = tokenizer

    def score(
        self,
        domain: str,
        *,
        searches_365d: Optional[int] = None,
        has_external_estimate: bool = False,
        active_bid_count: int = 0,
        gd_estimated_value_dollars: Optional["Decimal"] = None,
        current_price_dollars: Optional["Decimal"] = None,
    ) -> ScoreBreakdown:
        sld = domain.split(".")[0].lower() if "." in domain else domain.lower()
        tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""

        bd = ScoreBreakdown()

        # --- TLD --------------------------------------------------------
        if tld == "com":
            bd.tld = 40
        elif tld == "net":
            bd.tld = 5
        elif tld == "org":
            bd.tld = 3
        else:
            bd.tld = 0
            bd.notes.append(f"non-standard tld: .{tld}")

        # --- Length -----------------------------------------------------
        # Length brackets. Per the 2026-05-22 call, the client is interested in
        # longer slogan-style domains ("he's sluggo, the slogan-y stuff is
        # interesting"). We pushed the hard penalty out from 16+ to 19+ chars
        # to stop docking slogan-shaped domains. The multi-token slogan
        # bonus below adds an explicit positive signal when the SLD splits
        # cleanly into 3+ dictionary words.
        L = len(sld)
        if 3 <= L <= 4:
            bd.length = 30
        elif L == 5:
            bd.length = 28
        elif 6 <= L <= 8:
            bd.length = 25
        elif 9 <= L <= 10:
            bd.length = 20
        elif 11 <= L <= 12:
            bd.length = 10
        elif 13 <= L <= 18:
            bd.length = 0
        else:
            bd.length = -10
            bd.notes.append(f"very long ({L} chars)")

        # --- Character composition --------------------------------------
        has_hyphen = "-" in sld
        has_digit = any(c.isdigit() for c in sld)
        is_all_numeric = sld.replace("-", "").isdigit()
        is_all_letter = sld.isalpha()

        if has_hyphen:
            bd.composition -= 25
            bd.notes.append("contains hyphen")

        # Numeric / short-alphanumeric niche: the client explicitly keeps pursuing these.
        # The rule is:
        #   - <=5 chars + any digit -> niche lane, +10, no digit penalty
        #   - >5 chars + any digit  -> standard digit penalty
        short_numeric_niche = has_digit and L <= 5
        if short_numeric_niche:
            bd.composition += 10
            bd.notes.append("short-numeric niche lane")
        elif has_digit:
            bd.composition -= 15
            bd.notes.append("contains digit")

        if is_all_letter:
            bd.composition += 5

        # --- Word content (via respelling-aware tokenizer) --------------
        tokens_result: TokenizeResult = self.tokenizer.tokenize(sld)

        if tokens_result.direct_tokens:
            bd.word_content += 25
            bd.notes.append(f"direct words: {tokens_result.direct_tokens}")
        elif tokens_result.respelled_tokens:
            bd.word_content += 20
            bd.notes.append(f"respelled words: {tokens_result.respelled_tokens}")
        # Note: Double Metaphone phonetic fallback intentionally deferred — wire
        # in once we have the `metaphone` package available. Worth +10 when
        # neither direct nor respelled match found.

        # Multi-token slogan bonus (per 2026-05-22 + 28 + 2026-06-03 calls).
        # Awards +15 when an SLD reads as a real phrase / slogan rather
        # than a buzzword-stack. The client: long slogan-style domains stay
        # interesting; three-noun stacks (maxprobook, bookpromax,
        # suredocpro) do NOT.
        #
        # 2026-06-03 recalibration: the previous gate "depth < token_count"
        # was firing for EVERY domain whose tokens happened to not be in
        # the NAMING_FRAGMENTS list — which incorrectly elevated buzzword
        # stacks. The new heuristic requires a slogan-style **connector
        # word** ("it", "the", "of", "my", "your", "for", a question word,
        # an aux verb, etc.) to be present, OR the SLD to be genuinely long
        # (>= 13 chars). That way:
        #   "whyyouwant"     -> slogan ✓  (has "why" / "you")
        #   "haulitpro"      -> slogan ✓  (has "it") — the client accepts these
        #   "yourbestcleanmaster" -> slogan ✓ (long + has "your")
        #   "maxprobook"     -> NOT slogan  (no connector, len < 13)
        #   "bookpromax"     -> NOT slogan
        #   "suredocpro"     -> NOT slogan
        #   "nextridenow"    -> NOT slogan
        token_count = len(tokens_result.direct_tokens)
        if token_count >= 3:
            has_connector = any(
                t in _SLOGAN_CONNECTORS for t in tokens_result.direct_tokens
            )
            if L >= 13 or has_connector:
                bd.word_content += 15
                bd.notes.append(
                    f"multi-token slogan ({token_count} words"
                    f"{', has connector' if has_connector else ''})"
                )

        # --- Theme buckets ----------------------------------------------
        all_tokens = tokens_result.all_word_tokens
        buckets = bucket_matches(all_tokens)
        bd.themes = min(15, 5 * len(buckets))
        if buckets:
            bd.notes.append(f"themes: {buckets}")

        # --- Preference signals ------------------------------------
        if contains_preferred_keyword(all_tokens):
            bd.preference_signals += 10
            bd.notes.append("matches preferred keyword")
        # Park City cluster bonus REMOVED 2026-07-11: # The client no longer targets the Park City geographic cluster. The
        # generic one-word-city compound bonus in fragments.py still applies
        # to parkcity like any other city name; only the +15 special-case
        # and the park_city_strategic theme bucket were dropped.
        if any(p in PREFERRED_RESPELLING_PATTERNS for p in tokens_result.respelling_patterns_used):
            bd.preference_signals += 10
            bd.notes.append(
                f"uses the client's preferred respelling: {tokens_result.respelling_patterns_used}"
            )

        # Properly-spelled canonical premium: per the 2026-05-21 call, the
        # canonical full-letter form of a respelling target (true, light,
        # night, phone, tech, photo) is worth more than the respelled twin.
        # truezero.com > truzero.com, cleanlight.com > cleanlite.com.
        #
        # Calibration note (2026-05-22): the first live run of the expiry
        # feed surfaced 16/30 top hits containing "true" because the
        # canonical premium stacked on top of the existing top-50 bonus
        # (true is already in PREFERRED_KEYWORDS) and the brandable_prefix
        # theme. We dropped the premium from +15 to +5 so it functions as
        # a tiebreaker between proper-spelling and respelled twins without
        # dominating the list. A ~20-point gap between truezero (115) and
        # truzero (95) is still plenty to express the preference.
        if contains_canonical_respelling_target(tokens_result.direct_tokens):
            bd.preference_signals += 5
            bd.notes.append("properly-spelled canonical form (vs respelled twin)")

        # Brand-thin diagnostic: when the SLD's word content is *entirely*
        # respelled (no direct dictionary anchor at all), the client's framing
        # is "decent but brand-thin — doesn't pop for brandability." This is
        # a note, not a penalty — we still want the alert. The note lets
        # the UI explain why this is an alert vs. a high-confidence pick.
        if tokens_result.respelled_tokens and not tokens_result.direct_tokens:
            bd.notes.append(
                "brand-thin: phonetic match only (no direct dictionary anchor)"
            )

        # Compound brandability (per the client's 2026-05-28 + 2026-06-03 calls).
        # The strongest brandability signal is a two-concept compound:
        # bio+lytics, omni+kinetics, compu+max. The client's follow-ups clarified
        # that 3-concept combos (haulitpro, suredocpro, maxprobook) are
        # over-stuffed and should NOT score anywhere near clean 2-concepts.
        # 2026-06-03 recalibration: bumped the depth=3 penalty from -5 to
        # -25 and extended the length gate from L<14 to L<16. The previous
        # -5 was getting drowned out by the demand/length/word-content
        # bonuses (.com + 9-10 chars + direct tokens + $1000+ GD est = ~135
        # baseline), leaving "X+Y+pro" patterns near the top of the rankings.
        #
        # We use max_fragment_concepts (DP) for the actual decomposition
        # depth, then bracket by depth AND length. The length gate prevents
        # us from penalizing real multi-word slogans like
        # "yourbestcleanmaster" (depth=4 but 19 chars = a legitimate phrase,
        # not a smushed compound).
        #
        #   depth = 2                  -> +15  (sweet spot; +3 if alliteration)
        #   depth = 3, sld < 16 chars  -> -25  (over-stuffed; the client hates these)
        #   depth = 3, sld >= 16 chars -> 0    (could be a phrase, no penalty)
        #   depth >= 4, sld < 18 chars -> -25  (heavy stacking; also unwanted)
        #   depth >= 4, sld >= 18 chars-> 0    (likely a real slogan)
        depth = max_fragment_concepts(sld)
        compound = find_compound_split(sld) if depth >= 2 else None
        if depth == 2 and compound:
            left, right = compound
            bd.preference_signals += 15
            bd.notes.append(
                f"two-concept compound: '{left}' + '{right}' (brandable pair)"
            )
            # Alliteration sub-bonus: clubcraft, krispy-kreme-style hard
            # leading consonant match adds a small extra hit.
            if has_alliteration(left, right):
                bd.preference_signals += 3
                bd.notes.append(
                    f"alliteration: both halves start with '{left[0]}'"
                )
        elif depth == 3:
            if L < 16:
                bd.preference_signals -= 25
                bd.notes.append(
                    "three-concept compound over-stuffed in short SLD "
                    "(The client prefers clean 2-concept brands; -25)"
                )
            # else: depth=3 in a longer SLD might be a real phrase; no penalty
        elif depth >= 4:
            if L < 18:
                bd.preference_signals -= 25
                bd.notes.append(
                    f"four-plus-concept stacking in short SLD (depth={depth}; -25)"
                )
            # else: long depth=4+ is plausibly a slogan; no penalty
        else:
            # No fragment+fragment compound. Try fragment + city.
            city_compound = find_city_compound(sld)
            if city_compound:
                frag, city = city_compound
                bd.preference_signals += 12
                bd.notes.append(
                    f"fragment + city geo: '{frag}' + '{city}'"
                )

        # --- Demand signals --------------------------------------------
        if has_external_estimate:
            bd.demand += 5
        if searches_365d is not None and searches_365d > 50:
            bd.demand += 10
            bd.notes.append(f"high search activity ({searches_365d}/yr)")
        if active_bid_count > 0:
            bd.notes.append(f"has active bids: {active_bid_count}")

        # GoDaddy estimated value signals (per 2026-05-29 call). The feed
        # gives us GoDaddy's own appraisal of each domain. Two signals:
        #
        # (a) ABSOLUTE VALUE TIER -- higher GD estimate = higher score.
        #     GoDaddy's own people think the domain is worth real money,
        #     that's signal we should respect.
        #         GD est >= $5000   -> +15
        #         GD est >= $1000   -> +10
        #         GD est >= $500    -> +7
        #         GD est >= $200    -> +5
        #         GD est >= $100    -> +3
        #         GD est <  $100    -> 0
        #
        # (b) DISCOUNT RATIO -- when we'd pay much less than GD estimates
        #     it's worth, that's a "good deal" demand signal.
        #         ratio >= 50x  -> +10  (massive discount)
        #         ratio >= 20x  -> +7
        #         ratio >= 10x  -> +5
        #         ratio >= 5x   -> +3
        #         ratio <  5x   -> 0
        if gd_estimated_value_dollars is not None:
            try:
                est = float(gd_estimated_value_dollars)
            except (TypeError, ValueError):
                est = 0.0

            if est >= 5000:
                bd.demand += 15
                bd.notes.append(f"GD est ${est:.0f} (premium tier)")
            elif est >= 1000:
                bd.demand += 10
                bd.notes.append(f"GD est ${est:.0f} (high tier)")
            elif est >= 500:
                bd.demand += 7
                bd.notes.append(f"GD est ${est:.0f} (above $500)")
            elif est >= 200:
                bd.demand += 5
                bd.notes.append(f"GD est ${est:.0f} (above $200)")
            elif est >= 100:
                bd.demand += 3
                bd.notes.append(f"GD est ${est:.0f} (above $100)")

            # Discount ratio: only meaningful if we have a non-trivial price.
            if current_price_dollars is not None and est > 0:
                try:
                    price = float(current_price_dollars)
                except (TypeError, ValueError):
                    price = 0.0
                if price >= 1:
                    ratio = est / price
                    if ratio >= 50:
                        bd.demand += 10
                        bd.notes.append(f"steep discount: {ratio:.0f}x GD est")
                    elif ratio >= 20:
                        bd.demand += 7
                        bd.notes.append(f"good discount: {ratio:.0f}x GD est")
                    elif ratio >= 10:
                        bd.demand += 5
                        bd.notes.append(f"moderate discount: {ratio:.0f}x GD est")
                    elif ratio >= 5:
                        bd.demand += 3
                        bd.notes.append(f"slight discount: {ratio:.0f}x GD est")

        return bd
