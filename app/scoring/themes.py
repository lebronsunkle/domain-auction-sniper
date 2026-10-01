"""
Theme buckets derived from the analysis of a real ~19k domain portfolio.

Each bucket gets a small score bonus if a domain's tokens overlap with it.
Multiple bucket matches stack up to a cap (set in engine.py).
"""

from __future__ import annotations

# Buckets and the keywords that define them. Keywords are lowercase.
# Sourced from the portfolio analysis (top tokens by frequency).
THEME_BUCKETS: dict[str, set[str]] = {
    "brandable_prefix": {
        "master", "masters", "pro", "ultra", "dura", "max", "omni", "accu",
        "perma", "ergo", "nex", "intel", "logic", "quest", "cert", "true",
        "tru", "next", "prime", "apex", "peak", "summit", "top", "best",
        "first", "alpha", "mega", "super", "hyper", "mighty", "royal",
        "grand", "elite", "ace",
    },
    "brandable_suffix": {
        # Morphological suffixes that make a domain sound like a brand.
        # Per 2026-05-22 call: removed "tech"/"tek" because they're already
        # represented in tech_digital, and stacking them caused tech-heavy
        # domains to dominate the top-50 surface in a way the client disliked.
        "matic", "tic", "logic", "graph", "metric", "ware",
        "works", "corp", "inc", "sys", "systems", "group",
    },
    "industrial_b2b": {
        "chem", "poly", "therm", "thermo", "thermal", "carbon", "lube",
        "cote", "coat", "wipe", "glass", "mill", "mate", "flex", "port",
        "heat", "cool", "cold", "weld", "seal", "plast", "clad", "laminate",
        "compound", "resin", "solvent", "detergent", "grease",
    },
    "health_med_bio": {
        "bio", "med", "cardio", "derm", "rx", "health", "care", "cure",
        "heal", "life", "wellness", "clinic", "dental", "optical", "vision",
        "therapy", "pharma", "nutra", "vital",
    },
    "travel_hotel_vacation": {
        "hotel", "resort", "lodge", "cabin", "villa", "stay", "tour",
        "travel", "trip", "vacation", "escape", "cape", "scape", "beach",
        "mountain", "lake",
    },
    "sports_fitness_outdoor": {
        "sport", "sports", "fit", "gym", "run", "ride", "cycle", "ski",
        "surf", "golf", "tennis", "ball", "court", "field", "track", "race",
        "trail", "outdoor", "adventure", "craft",
    },
    "energy_solar_eco": {
        "solar", "energy", "power", "eco", "green", "wind", "hydro", "watt",
        "volt", "amp", "fuel", "gas", "oil", "electric", "battery", "grid",
    },
    "tech_digital": {
        "tech", "data", "app", "apps", "cloud", "online", "net", "web",
        "digital", "cyber", "ai", "io", "code", "dev", "stream", "api",
        "platform", "studio", "lab", "labs",
    },
    "phone_communications": {
        "fone", "phone", "call", "text", "message", "talk", "ring", "tel",
        "wire", "wireless", "mobile",
    },
    "auto_vehicle": {
        "auto", "car", "cars", "kar", "drive", "ride", "motor", "fleet",
        "truck", "trailer", "wheels", "tire", "tires", "engine",
    },
    "real_estate": {
        "realty", "realtor", "realtors", "property", "properties", "home",
        "homes", "house", "houses", "condo", "condos", "apartment", "rent",
        "lease", "listing",
    },
    "pets": {
        # Added 2026-05-22. Pet-industry tokens we want to surface as a
        # dedicated category in the per-theme breakdown. Curated to avoid
        # false positives -- excluded generic terms like "food", "rescue",
        # "treat" that fire too broadly outside the pet context. Includes
        # common -er derivatives because the tokenizer's greedy-longest match
        # picks "groomer" over "groom"+"er".
        "pet", "pets", "dog", "dogs", "doggy", "doggie",
        "cat", "cats", "kitty", "kitties",
        "puppy", "puppies", "kitten", "kittens",
        "pup", "pups", "vet", "vets", "veterinary",
        "kennel", "kennels",
        "groom", "groomer", "groomers", "grooming",
        "paw", "paws", "bark", "barker", "barkers",
        "leash", "collar", "collars",
        "breed", "breeder", "breeders",
        "canine", "feline", "fido",
        "aquarium", "terrarium",
    },
    # "park_city_strategic" bucket REMOVED 2026-07-11: # The client no longer targets the Park City cluster. The city names remain
    # in fragments.CITY_GEO_NAMES for the generic city-compound bonus.
}


# Preferred keywords. Domains containing any of these get a small bonus.
# This is a short EXAMPLE list; in a real deployment it is generated from an
# analysis of the owner's own portfolio (top tokens by frequency).
PREFERRED_KEYWORDS: frozenset[str] = frozenset({
    "pro", "max", "bio", "smart", "true", "logic", "prime", "flex",
    "solar", "next", "craft", "world", "city", "clean", "sport",
})


# The client's preferred respelling pattern labels (from RESPELLING_RULES in tokenizer.py).
# Per 2026-05-22 call: removed "k_c_initial" because the client explicitly rejected
# the 2-K's-and-other-k-prefix pattern (karma, KO tech, kape kafe, etc.) when he
# walked through the first live top-50. The tokenizer still recognizes the
# pattern -- we just don't award the respelling bonus for it anymore.
PREFERRED_RESPELLING_PATTERNS: frozenset[str] = frozenset({
    "lite_light", "rite_right", "nite_night", "tek_tech", "fone_phone",
    "z_oys", "z_ids",
})


# Canonical (fully-spelled) twins of the client's preferred respelling patterns.
# Per the 2026-05-21 call: a domain that uses the PROPER spelling of one of
# these targets is worth more than the respelled variant. truezero.com >
# truzero.com because the canonical form has stronger sign/sound/sight/meaning.
# These are the fully-spelled-out target words that the RESPELLING_RULES
# transform INTO — so if any of them appear as direct dictionary tokens in
# the SLD, we're looking at the canonical form, not the respelling.
#
# 2026-05-22: removed "tech" because the tech surface dominated the first
# live run in a way the client disliked. "tech" is still in tech_digital and
# PREFERRED_KEYWORDS so it still scores well; we just don't give it an
# additional canonical premium.
CANONICAL_RESPELLING_TARGETS: frozenset[str] = frozenset({
    "light", "right", "night", "true", "through", "phone", "photo",
})


def contains_canonical_respelling_target(tokens: list[str]) -> bool:
    """True if any of the domain's direct (non-respelled) tokens is the
    canonical full-letter form of one of the client's preferred respelling
    targets (light, right, night, true, phone, photo, tech, through).

    The caller should only pass `direct_tokens` here, NOT respelled tokens —
    we explicitly want to reward the properly-spelled form over the
    respelled twin.
    """
    return any(t in CANONICAL_RESPELLING_TARGETS for t in tokens)


def bucket_matches(tokens: list[str]) -> list[str]:
    """Return the list of bucket names whose keywords overlap with the tokens."""
    matches = []
    token_set = set(tokens)
    for bucket, keywords in THEME_BUCKETS.items():
        if token_set & keywords:
            matches.append(bucket)
    return matches


def contains_preferred_keyword(tokens: list[str]) -> bool:
    return any(t in PREFERRED_KEYWORDS for t in tokens)


# is_park_city_cluster() REMOVED 2026-07-11 along with the +15 engine bonus
# and the park_city_strategic bucket — the client no longer targets that cluster.
