"""
Estibot API client.

Estibot is the domain valuation service the client uses as his second-highest
signal (after Google search volume). Per the 2026-05-21 call he greenlit
the $500/mo subscription; per the 2026-05-22 call he's checking the
specific pricing tier right now.

This module is built as a STUB-READY-TO-FLIP. The endpoint URLs, request
shape, and response field names below come from Estibot's public API docs.
When the client provides the API key, the integration just needs:

  1. ESTIBOT_API_KEY added to .env
  2. Estibot fields wired into ScoreBreakdown.valuation in the engine
  3. A worker enrichment pass mirroring _enrich_with_tld_spread

URL pattern (confirmed 2026-05-22 against the client's Advanced account):

    GET https://www.estibot.com/api?k=KEY&a=ACTION&d=DATA&t=MODE

Note the structure: the ACTION is a query parameter `a=`, NOT a URL path
component. The auth key uses `k=` not `api_key=`. Easy to get wrong --
the api-info page's URL examples are the source of truth.

The three actions we care about:

  - a=appraise: d=<dom1>>>...>>>&t=cache|live|auto
                Returns ~100 fields per domain including appraised_value,
                category, classification_id, com_taken, net_taken, ...
                extensions_taken (= TLD spread count!).

  - a=bid_tool: d=<kw1>>>...>>>
                Returns search volume + CPC per keyword. This is our path
                to the Google search volume signal the client called his #1
                gauge -- Estibot already aggregates Google keyword data.

  - a=zone_diff: date=YYYY-MM-DD
                Daily zone file diff (newly added + newly expired domains).
                Potential replacement for the GoDaddy inventory feed if it
                gives us better coverage; investigate after wiring appraise.

Important caveats to confirm against the live API:
  - The exact URL pattern (might be `api.estibot.com` rather than
    `www.estibot.com/api`)
  - Cache vs live mode semantics (cache=cheap-but-stale; live=accurate-but-slow)
  - Daily query limits per account tier
  - Whether the bid_tool endpoint returns search volume in absolute terms
    or as a "competition" score
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Base URL — UPDATED 2026-07-14. Estibot's current docs (their GitHub repo
# github.com/domainret/estibot-api, which www.estibot.com/api-info now names
# as the source of truth) moved the API to a dedicated host:
#
#     https://public-api.estibot.com/api
#
# CRITICAL HISTORY: the old endpoint (www.estibot.com/api) required a
# support-ticket IP whitelist and returned HTML/403 without it — that is
# what blocked this integration since 2026-05-22 (task #50). The NEW
# endpoint documents API-key-only auth with per-IP RATE LIMITS (3 req/s,
# 100 req/min) and NO whitelist. The month-long wait for support was
# likely unnecessary once this endpoint existed.
#
# Response envelope note (per the new docs): `results` is a top-level
# ARRAY of result objects; `cache`, `bulk`, `item_count`, `not_found` are
# top-level siblings of `results` — NOT nested inside a `results` object.
DEFAULT_BASE_URL = "https://public-api.estibot.com/api"

# Cache modes per docs. "auto" lets Estibot decide based on data age.
CACHE_MODE_LIVE = "live"
CACHE_MODE_CACHE = "cache"
CACHE_MODE_AUTO = "auto"


# ---------------------------------------------------------------------------
# Response dataclasses -- field names mirror Estibot's documented JSON shape
# ---------------------------------------------------------------------------


@dataclass
class DomainAppraisal:
    """Result of an `appraise` call for a single domain.

    Field names mirror Estibot's JSON response. Optional fields will be None
    when Estibot doesn't return them (which happens for some TLDs / inputs).
    Only the most useful fields are pinned here -- the full ~100 fields are
    captured in `raw` for downstream inspection.
    """

    domain: str

    # ---- Valuation (the headline numbers) ----
    appraised_value: Optional[Decimal] = None
    """Primary valuation in USD. The number the client actually looks at."""
    appraised_wholesale_value: Optional[Decimal] = None
    """Wholesale (broker-to-broker) value, typically lower."""
    appraised_no_sales_value: Optional[Decimal] = None
    """Valuation calculated without comparable sales data -- useful when
    appraised_value seems inflated by a single outlier sale."""

    # ---- Classification (sector tagging the client wants) ----
    category: Optional[str] = None
    """Domain's primary category, e.g. 'Health' or 'Real Estate'."""
    category_root: Optional[str] = None
    """Top-level industry classification."""
    classification_id: Optional[int] = None

    # ---- TLD spread (our key signal from picnic example) ----
    extensions_taken: Optional[int] = None
    """How many TLDs of this SLD are already registered."""
    sld_extensions_taken: Optional[int] = None
    """Same as extensions_taken but using Estibot's SLD canonicalization."""
    com_taken: Optional[bool] = None
    net_taken: Optional[bool] = None
    org_taken: Optional[bool] = None
    biz_taken: Optional[bool] = None
    info_taken: Optional[bool] = None
    us_taken: Optional[bool] = None

    # ---- Structural ----
    sld: Optional[str] = None
    tld: Optional[str] = None
    num_words: Optional[int] = None
    num_hyphens: Optional[int] = None
    num_numbers: Optional[int] = None
    sld_length: Optional[int] = None
    first_word: Optional[str] = None
    second_word: Optional[str] = None
    language: Optional[str] = None
    language_probability: Optional[float] = None

    # ---- Flags ----
    is_cctld: Optional[bool] = None
    is_ntld: Optional[bool] = None
    is_adult: Optional[bool] = None
    is_reversed: Optional[bool] = None

    # Full payload, for fields we haven't pinned. Useful for one-off ad-hoc
    # questions ("what does Estibot return for the 'monetization' value?")
    # without having to redeploy.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class KeywordMetrics:
    """Result of a `bid_tool` lookup for a single keyword.

    This is the path to the client's #1 gauge: Google search volume. Estibot
    aggregates Google keyword data and exposes it via this endpoint.
    """

    keyword: str
    search_volume: Optional[int] = None
    """Monthly search volume. Per the client's heuristic: 160k+ = high-conf
    buy at $112 or more. Under 100 = pass."""
    cpc_dollars: Optional[Decimal] = None
    """Cost-per-click in advertiser dollars. Higher CPC = more commercial intent."""
    competition: Optional[float] = None
    """0..1 advertiser competition score. Higher = more demand."""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EstibotClientConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    default_cache_mode: str = CACHE_MODE_AUTO
    # 2026-08-26: was 30.0 — when Estibot drops connections (their per-IP
    # rate limiter does this silently), every call stalled the full 30s and
    # the dashboard's signals popup hung behind it. Estibot is a nice-to-
    # have; 8s is generous for a healthy API and fails fast for a sick one.
    timeout_seconds: float = 8.0
    # Estibot caps batched lookups per call. The docs use the `>>` separator,
    # so the practical cap is whatever fits inside their URL length limit.
    # Default 50 is a conservative safe-bet; tune up once we observe.
    max_batch_size: int = 50


class EstibotNotConfigured(RuntimeError):
    """Raised when an API call is attempted without a key in config."""


class EstibotClient:
    """Stub-ready Estibot client. Makes real HTTP calls when given a valid
    api_key; otherwise raises EstibotNotConfigured."""

    def __init__(self, config: EstibotClientConfig):
        if not config.api_key:
            raise EstibotNotConfigured(
                "Estibot API key not configured. Set ESTIBOT_API_KEY in .env "
                "after subscribing at https://www.estibot.com/register."
            )
        self.config = config

    async def appraise(
        self,
        domains: list[str],
        cache_mode: Optional[str] = None,
    ) -> dict[str, DomainAppraisal]:
        """Bulk-appraise a list of domains.

        Returns a dict mapping each input domain to its DomainAppraisal.
        Domains Estibot couldn't appraise (invalid input, missing data)
        will be absent from the result map.
        """
        mode = cache_mode or self.config.default_cache_mode
        out: dict[str, DomainAppraisal] = {}
        for batch in _batched(domains, self.config.max_batch_size):
            # Estibot restriction (docs + live behavior 2026-07-14): `auto`
            # mode only works for SINGLE-domain lookups. Batches must use
            # cache or live — cache is the recommended fast path.
            batch_mode = mode
            if len(batch) > 1 and batch_mode == CACHE_MODE_AUTO:
                batch_mode = CACHE_MODE_CACHE
            payload = await self._call(
                "appraise",
                params={"d": ">>".join(batch), "t": batch_mode},
            )
            for row in _extract_rows(payload):
                d = (row.get("domain") or "").lower()
                if d:
                    out[d] = _parse_appraisal_row(d, row)
        return out

    async def keyword_search_volume(
        self,
        keywords: list[str],
    ) -> dict[str, KeywordMetrics]:
        """Bulk-lookup search volume + CPC for keywords.

        Use this to answer "is anyone Googling this term?" -- the client's #1
        decision input. Pass the SLD or token of a candidate domain.
        """
        out: dict[str, KeywordMetrics] = {}
        for batch in _batched(keywords, self.config.max_batch_size):
            payload = await self._call(
                "bid_tool",
                params={"d": ">>".join(batch)},
            )
            for row in _extract_rows(payload):
                kw = (row.get("keyword") or row.get("d") or "").lower()
                if kw:
                    out[kw] = KeywordMetrics(
                        keyword=kw,
                        search_volume=_to_int(row.get("search_volume") or row.get("volume")),
                        cpc_dollars=_to_decimal(row.get("cpc") or row.get("avg_cpc")),
                        competition=_to_float(row.get("competition")),
                        raw=row,
                    )
        return out

    # -- internal -----------------------------------------------------------

    async def _call(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        """Single HTTP call to the Estibot API.

        Returns the parsed JSON body. Logs and returns {} on transport errors
        so the caller can degrade gracefully -- Estibot is a nice-to-have
        signal source, not a critical-path dependency.

        Estibot URL pattern (confirmed 2026-05-22):
            GET https://www.estibot.com/api?k=KEY&a=ACTION&d=DATA&t=MODE

        The action (appraise, bid_tool, zone_diff) is the `a` query param --
        NOT a URL path component as you'd expect from a REST API. The auth
        key uses `k=` not `api_key=`.
        """
        import httpx  # local import: avoid forcing httpx on callers that never use Estibot

        url = self.config.base_url  # no path append -- action goes in `a=`
        merged_params = {
            "k": self.config.api_key,
            "a": endpoint,
            **params,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.get(url, params=merged_params)
        except httpx.RequestError as e:
            logger.warning("estibot %s transport error: %s", endpoint, e)
            return {}

        if response.status_code >= 400:
            logger.warning(
                "estibot %s returned %d: %s",
                endpoint,
                response.status_code,
                response.text[:300],
            )
            return {}

        try:
            payload = response.json()
        except ValueError:
            logger.error("estibot %s response was not JSON: %s", endpoint, response.text[:300])
            return {}

        # New-endpoint error envelope (2026-07-14 docs): success=false with
        # the reason in `message` ("Invalid API key.", "Rate limit
        # exceeded.", ...). Surface the reason instead of silently parsing
        # an empty results object.
        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning(
                "estibot %s rejected: %s", endpoint, payload.get("message") or "(no message)"
            )
            return {}
        return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batched(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Estibot wraps results inconsistently across endpoints and docs.

    CONFIRMED LIVE SHAPE (2026-07-14, public-api.estibot.com, appraise):
        {"success": true, "results": {"data": [ {...} ], "total": 1, ...},
         "cache": true, "item_count": 1, ...}
    i.e. `results` is an OBJECT with the rows in `results.data` — despite
    the GitHub docs showing `results` as a flat array. Handle every shape
    we've seen: results.data (live), top-level data, flat results list,
    or a bare list."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        # Live shape: results is a dict containing "data".
        results = payload.get("results")
        if isinstance(results, dict) and isinstance(results.get("data"), list):
            return [r for r in results["data"] if isinstance(r, dict)]
        for key in ("data", "results", "domains", "keywords"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # Single dict response - wrap.
        return [payload]
    return []


def _parse_appraisal_row(domain: str, row: dict[str, Any]) -> DomainAppraisal:
    return DomainAppraisal(
        domain=domain,
        appraised_value=_to_decimal(row.get("appraised_value")),
        appraised_wholesale_value=_to_decimal(row.get("appraised_wholesale_value")),
        appraised_no_sales_value=_to_decimal(row.get("appraised_no_sales_value")),
        category=_to_str(row.get("category")),
        category_root=_to_str(row.get("category_root")),
        classification_id=_to_int(row.get("classification_id")),
        extensions_taken=_to_int(row.get("extensions_taken")),
        sld_extensions_taken=_to_int(row.get("sld_extensions_taken")),
        com_taken=_to_bool(row.get("com_taken")),
        net_taken=_to_bool(row.get("net_taken")),
        org_taken=_to_bool(row.get("org_taken")),
        biz_taken=_to_bool(row.get("biz_taken")),
        info_taken=_to_bool(row.get("info_taken")),
        us_taken=_to_bool(row.get("us_taken")),
        sld=_to_str(row.get("sld")),
        tld=_to_str(row.get("tld")),
        num_words=_to_int(row.get("num_words")),
        num_hyphens=_to_int(row.get("num_hyphens")),
        num_numbers=_to_int(row.get("num_numbers")),
        sld_length=_to_int(row.get("sld_length")),
        first_word=_to_str(row.get("first_word")),
        second_word=_to_str(row.get("second_word")),
        language=_to_str(row.get("language")),
        language_probability=_to_float(row.get("language_probability")),
        is_cctld=_to_bool(row.get("is_cctld")),
        is_ntld=_to_bool(row.get("is_ntld")),
        is_adult=_to_bool(row.get("is_adult")),
        is_reversed=_to_bool(row.get("is_reversed")),
        raw=row,
    )


def _to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_bool(v: Any) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "t")
    return None
