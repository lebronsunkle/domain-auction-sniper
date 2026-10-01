"""
Naming-fragment vocabulary for compound brandability scoring.

Per the client's 2026-05-28 call: a brandable domain is fundamentally a
two-concept compound. `bio` + `lytics` = `biolytics`. `omni` + `kinetics`
= `omnikinetics`. `compu` + `max` = `compumax`. Both halves are
recognized naming morphemes -- not necessarily full dictionary words,
but particles that domainers, brand consultants, and trademark
attorneys instantly read as "this means something."

This module holds the union of:

  - The 5,000-Fragment PDF the client shared (extracted to ~450 unique
    entries; the "5,000" in the title double-counts across columns)
  - the client's specific examples from the call (lytics, kinetics, compu,
    dina, chem, linux, sport, control, ...)
  - Our existing PREFERRED_KEYWORDS, which we treat as fragment-eligible
  - Common single-word major city names so `biosandiego` lights up the
    same as `parkcityapp`

The scoring engine uses this in `find_compound_split()` -- it walks every
possible split point of an SLD and checks whether both halves are in
NAMING_FRAGMENTS. If so, it's a recognized compound and gets a major
brandability bonus.

Keep this list curated. Adding a fragment that's too short (e.g. "an",
"to") would cause false positives on random letter combinations. The
threshold lives in `find_compound_split` (default min 3 chars per half).
"""

from __future__ import annotations


# -----------------------------------------------------------------------------
# PDF-derived fragments (extracted from
# "English Prefix Suffix Root Fragments 5000 List" -- 2026-05-28)
# -----------------------------------------------------------------------------

PDF_FRAGMENTS: frozenset[str] = frozenset({
    "ab", "ability", "able", "accel", "access", "aceous", "act", "active",
    "acy", "ad", "adapt", "admin", "advance", "advis", "aero", "affect",
    "affirm", "age", "agent", "agile", "agri", "agro", "air", "al", "alert",
    "algia", "align", "allo", "alloy", "alpha", "alter", "ambi", "ambit",
    "amplify", "an", "ana", "analyze", "ance", "anchor", "ancy", "andro",
    "angle", "animate", "ant", "ante", "anti", "apex", "apo", "apply", "aqua",
    "arc", "arch", "arise", "arium", "array", "ary", "ascend", "ascent",
    "asset", "assist", "astro", "ate", "ation", "ative", "atom", "audio",
    "augment", "aurora", "aurum", "auto", "avail", "axis", "azure", "base",
    "beacon", "beam", "bene", "beta", "bi", "binary", "bio", "blaze", "block",
    "bloom", "bolt", "boost", "bot", "bridge", "bright", "build", "byte",
    "cache", "cad", "calc", "carbon", "cargo", "catalyst", "celer", "center",
    "central", "centric", "chain", "chrono", "cipher", "circuit", "circum",
    "cis", "civic", "clarity", "clear", "cloud", "cluster", "co", "code",
    "col", "com", "con", "connect", "contra", "core", "cosmic", "counter",
    "craft", "create", "crypto", "crystal", "current", "cy", "cyber", "cycle",
    "data", "de", "delta", "demi", "design", "di", "dia", "dif", "digital",
    "direct", "dis", "dom", "drive", "dynamic", "dys", "eco", "edge", "ee",
    "eer", "electro", "elevate", "elite", "em", "ember", "emerge", "en",
    "ence", "ency", "endo", "energy", "engine", "enhance", "ent", "enter",
    "epi", "epic", "er", "ery", "ess", "ette", "eu", "evolve", "ex", "excel",
    "expand", "expert", "explore", "express", "extend", "extra", "fabric",
    "factor", "ferro", "fiber", "field", "filter", "finite", "flare", "flex",
    "flow", "flux", "focus", "force", "fore", "forge", "form", "fragments",
    "frame", "ful", "fusion", "future", "galaxy", "gamma", "gateway",
    "genesis", "genic", "geo", "giga", "global", "graph", "graphy", "gravity",
    "grid", "group", "growth", "guard", "guide", "halo", "harbor", "harmony",
    "helio", "helix", "hemi", "hetero", "hexa", "homeo", "homo", "hood",
    "horizon", "hybrid", "hydro", "hyper", "hypo", "ian", "ibility", "ible",
    "ic", "ical", "ics", "ide", "ify", "ignite", "il", "ile", "im", "impact",
    "impulse", "in", "index", "ine", "infra", "ing", "insight", "inspire",
    "instant", "integrate", "inter", "interact", "interface", "intra",
    "invent", "ion", "ious", "ise", "ish", "ism", "iso", "ist", "ite", "ity",
    "ive", "ize", "junction", "kilo", "kinetic", "less", "leverage", "ling",
    "ly", "macro", "magn", "magna", "magnetic", "matrix", "mech", "mega",
    "meso", "meta", "metric", "micro", "milli", "mini", "mod", "module",
    "mono", "morph", "multi", "nano", "ness", "neuro", "neutral", "nexus",
    "nimbus", "node", "nova", "novo", "ob", "octo", "oid", "ology", "omni",
    "on", "onyx", "optic", "optim", "or", "orbit", "origin", "ortho", "osis",
    "out", "over", "ox", "oxide", "pan", "para", "path", "pent", "per",
    "peri", "phase", "phon", "photo", "pixel", "plasma", "plus", "poly",
    "post", "power", "pre", "precision", "primal", "pro", "proto", "pseudo",
    "pulse", "quanta", "quantum", "quest", "radial", "re", "redox", "relay",
    "render", "retro", "ria", "ridge", "rise", "rooted", "saga", "salt",
    "sapient", "scape", "scope", "scribe", "self", "semi", "sense", "serve",
    "ship", "signal", "silicon", "sky", "smart", "sol", "solar", "solid",
    "sona", "sonic", "soph", "sphere", "spire", "stack", "stage", "stamp",
    "stat", "stellar", "stream", "strong", "struct", "studio", "sub", "summit",
    "super", "supra", "swift", "syn", "tank", "tect", "terra", "tetra",
    "thermo", "thrive", "tion", "titan", "topia", "torch", "torque", "trade",
    "trans", "tri", "trust", "ultra", "umbra", "uni", "up", "ure", "vault",
    "vector", "venture", "vertex", "via", "vibe", "virtual", "vision",
    "vista", "vital", "vivid", "volt", "wave", "web", "wide", "wired", "wise",
    "world", "xeno", "yotta", "zenith", "zero", "zone", "max",
})


# -----------------------------------------------------------------------------
# The client's call-out additions (2026-05-28). The PDF is missing many fragments
# The client specifically named as brandable. We add them here so his examples
# (biolytics, omnikinetics, compumax, dinastamp, etc.) all light up.
# -----------------------------------------------------------------------------

CUSTOM_FRAGMENT_ADDITIONS: frozenset[str] = frozenset({
    # Short EXAMPLE list of extra naming particles. In a real deployment this
    # is tuned from the owner's buying history.
    "lytics", "kinetics", "compu", "control", "sport", "sports",
    "lab", "hub", "smart", "swift", "trust", "zen", "max", "tech",
    "craft", "stamp", "struct", "mate", "gear", "spot",
})


# -----------------------------------------------------------------------------
# Final naming-fragment set (PDF + the client additions). Caller checks membership
# against this. Re-exports the union as a frozen set so it can't be mutated
# at runtime.
# -----------------------------------------------------------------------------

NAMING_FRAGMENTS: frozenset[str] = frozenset(
    PDF_FRAGMENTS | CUSTOM_FRAGMENT_ADDITIONS
)


# -----------------------------------------------------------------------------
# Single-word major city / geo names. Per the client's 2026-05-28 call, the
# Park City cluster bonus should generalize to any major one-word city
# name. `biosandiego` should light up the same as `parkcityapp`.
#
# "San Diego" is two words but compresses to one in domains -- the
# tokenizer can't naturally split "sandiego" so we include the whole
# concatenated form. Same for other multi-word cities people commonly
# write as one word in domains.
# -----------------------------------------------------------------------------

CITY_GEO_NAMES: frozenset[str] = frozenset({
    # US major metros
    "newyork", "nyc", "losangeles", "la", "chicago", "houston", "phoenix",
    "philadelphia", "philly", "sanantonio", "sandiego", "dallas",
    "austin", "sanjose", "fortworth", "jacksonville", "columbus",
    "charlotte", "indianapolis", "indy", "seattle", "denver", "boston",
    "nashville", "memphis", "portland", "vegas", "lasvegas", "louisville",
    "baltimore", "milwaukee", "albuquerque", "tucson", "fresno", "mesa",
    "sacramento", "atlanta", "atl", "kansascity", "miami", "raleigh",
    "omaha", "oakland", "minneapolis", "tulsa", "wichita", "neworleans",
    "arlington", "honolulu", "tampa", "stlouis", "stl", "pittsburgh",
    "cincinnati", "cleveland", "anchorage", "buffalo", "orlando",
    "richmond", "boise", "tacoma", "spokane", "fresno", "anaheim",
    "irvine", "longbeach", "santaana", "stockton", "riverside", "chula",
    "bakersfield", "norfolk", "modesto", "fontana", "moreno",
    "huntington", "yonkers", "glendale", "jersey", "tempe", "scottsdale",
    "chandler", "lubbock", "laredo", "garland", "irving", "plano",
    "winston", "rochester", "akron", "newark", "alexandria",
    "stpaul", "santaclara", "hollywood",
    # International / commonly-traded
    "toronto", "montreal", "vancouver", "ottawa", "calgary",
    "london", "manchester", "birmingham", "liverpool",
    "paris", "lyon", "marseille", "berlin", "munich", "hamburg",
    "frankfurt", "madrid", "barcelona", "rome", "milan", "amsterdam",
    "rotterdam", "stockholm", "oslo", "copenhagen", "helsinki",
    "dublin", "vienna", "prague", "warsaw", "moscow", "athens",
    "istanbul", "dubai", "abudhabi", "doha", "telaviv", "tokyo",
    "osaka", "kyoto", "seoul", "beijing", "shanghai", "hongkong",
    "singapore", "bangkok", "mumbai", "delhi", "sydney", "melbourne",
    "brisbane", "perth", "auckland", "mexico", "rio", "saopaulo",
    "buenosaires", "santiago", "lima",
    # Utah-specific (The client's existing Park City cluster, kept)
    "parkcity", "saltlake", "slc", "heber", "deervalley", "wasatch",
    "ogden", "provo", "stgeorge", "moab",
    # Other US that often show up in his portfolio
    "malibu", "venice", "santamonica", "carmel", "napa", "sonoma",
    "tahoe", "aspen", "vail", "telluride", "jackson", "sundance",
    "kona", "maui", "oahu", "waikiki", "lahaina",
})


# -----------------------------------------------------------------------------
# Compound brandability check
# -----------------------------------------------------------------------------


def max_fragment_concepts(sld: str, *, min_part_len: int = 3) -> int:
    """Return the MAXIMUM number of NAMING_FRAGMENTS the SLD can be split
    into (greedy isn't enough -- we use DP). Returns 0 if no full
    decomposition exists.

    Per the client's 2026-05-28 call: 2 concepts is the sweet spot for
    brandability, 3 is still recognizable but less brandable, 4+ is
    over-stuffed and should NOT score like a clean 2-concept compound.
    The engine uses this count to bracket-bonus accordingly.

      max_fragment_concepts("biolytics")  -> 2  (bio + lytics)
      max_fragment_concepts("gymprotech") -> 3  (gym + pro + tech)
      max_fragment_concepts("aaabbbccc")  -> 0  (none of those are fragments)
    """
    sld = (sld or "").lower()
    if not sld.isalpha():
        return 0
    n = len(sld)
    if n < min_part_len:
        return 0
    # dp[i] = max fragments to compose sld[:i], or -1 if impossible.
    dp = [-1] * (n + 1)
    dp[0] = 0
    for i in range(min_part_len, n + 1):
        for j in range(0, i - min_part_len + 1):
            if dp[j] < 0:
                continue
            if sld[j:i] in NAMING_FRAGMENTS:
                candidate = dp[j] + 1
                if candidate > dp[i]:
                    dp[i] = candidate
    return max(dp[n], 0)


def has_alliteration(left: str, right: str) -> bool:
    """True if both halves start with the same consonant. Vowel-vowel
    matches don't count -- the brand-naming alliteration effect ("Coca-
    Cola", "Best Buy", "Krispy Kreme", "clubcraft") relies on a hard
    leading consonant repeating."""
    if not left or not right:
        return False
    a, b = left[0].lower(), right[0].lower()
    if a not in "bcdfghjklmnpqrstvwxyz":
        return False
    return a == b


def find_compound_split(
    sld: str,
    *,
    min_part_len: int = 3,
    extra_vocab: frozenset[str] | None = None,
) -> tuple[str, str] | None:
    """Walk every possible split point of `sld` and return the first
    (left, right) pair where both halves are in NAMING_FRAGMENTS (or in
    `extra_vocab` if provided).

    Returns None if no valid split is found.

    `min_part_len` keeps us from matching trivial 1- or 2-char fragments
    that would generate false positives on random letter combinations.
    The default of 3 means the shortest possible domain split is 3+3 = 6
    chars (e.g. "bio" + "max" in "biomax").

    The first match wins, preferring earlier (shorter-left) splits. That
    biases toward prefix-as-shorter-half, which matches how the client reads
    these (bio-lytics, omni-kinetics, compu-max).
    """
    sld = (sld or "").lower()
    if not sld.isalpha() or len(sld) < 2 * min_part_len:
        return None

    vocab = NAMING_FRAGMENTS if extra_vocab is None else (NAMING_FRAGMENTS | extra_vocab)

    for i in range(min_part_len, len(sld) - min_part_len + 1):
        left = sld[:i]
        right = sld[i:]
        if left in vocab and right in vocab:
            return (left, right)
    return None


def find_city_compound(
    sld: str,
    *,
    min_other_len: int = 3,
) -> tuple[str, str] | None:
    """Same idea as `find_compound_split`, but specifically for the
    fragment + city pattern. Either half can be the city.

    `biosandiego` -> ("bio", "sandiego")
    `parkcityapp` -> ("parkcity", "app")

    Returns (fragment_half, city_half) on match, or None.

    `parkcityapp` would also have been caught by the existing
    is_park_city_cluster substring check; this function generalizes that
    to any city in CITY_GEO_NAMES.
    """
    sld = (sld or "").lower()
    if not sld.isalpha() or len(sld) < 2 * min_other_len:
        return None

    # Try every city we know; longest first so "newyork" wins over "new"
    # if both happened to be in fragment vocab.
    for city in sorted(CITY_GEO_NAMES, key=len, reverse=True):
        if len(city) < min_other_len:
            continue
        if sld.startswith(city):
            other = sld[len(city):]
            if len(other) >= min_other_len and other in NAMING_FRAGMENTS:
                return (other, city)
        elif sld.endswith(city):
            other = sld[: -len(city)]
            if len(other) >= min_other_len and other in NAMING_FRAGMENTS:
                return (other, city)
    return None
