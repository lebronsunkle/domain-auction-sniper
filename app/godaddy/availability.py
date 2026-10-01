"""
GoDaddy domain availability client — for TLD spread checking.

Per the 2026-05-22 call with the client: TLD spread is one of his core valuation
signals. His `picnic` example — *"with picnic, there's like 299 extensions
registered, dot DE for Germany, dot BE for Belgium. If those are taken and
the .com is the only one missing, that's huge information"* — illustrates
the rule. The same SLD being registered across many TLDs means real-world
demand exists, even if the .com isn't taken yet.

We hit GoDaddy's standard `/v1/domains/available` endpoint. This is NOT the
Aftermarket API — it's the regular Domain API every GoDaddy account can use.
Auth is the same sso-key header so we can reuse GoDaddyClient.

Endpoint:
    POST https://api.godaddy.com/v1/domains/available?checkType=FAST
    Body: JSON array of domain strings (up to 500 per call)
    Response: { "domains": [ {"domain": "...", "available": bool, ...} ] }

We use checkType=FAST (the cached, sub-second variant). The result is
"is this SLD.tld currently registerable" — FALSE means taken, which is
the signal we actually want.

Eventually Estibot will give us TLD spread directly as part of its $500/mo
valuation feed, but until that's wired this is the free version that uses
infrastructure we already have.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .client import GoDaddyClient

logger = logging.getLogger(__name__)


# Default TLD set to check for each SLD. Mix of gTLDs and ccTLDs the client cares
# about. ~25 entries keeps each spread check to one bulk API call. Tuned with
# The client's `picnic` example in mind: DE, BE, CH, NL all matter for him.
DEFAULT_TLDS_TO_CHECK: list[str] = [
    # Top gTLDs
    "com", "net", "org", "co", "io", "me", "biz", "info", "app", "dev", "ai",
    # European ccTLDs the client specifically cited
    "de", "be", "ch", "nl", "uk", "fr", "es", "se", "no", "dk", "it",
    # Asia-Pacific
    "jp", "com.au", "com.br", "com.mx",
]


@dataclass
class DomainAvailability:
    """Per-domain result from the bulk availability endpoint."""

    domain: str
    available: bool
    definitive: bool = False
    price_micros: Optional[int] = None
    currency: Optional[str] = None


@dataclass
class TLDSpreadResult:
    """Result of running a TLD spread check on a single SLD.

    `taken_count` is the headline number: how many of the checked TLDs are
    already registered. Higher = stronger demand signal. Per the client's rule
    of thumb, an SLD with 200+ TLDs taken (out of his ~250 manual checks)
    is a high-confidence buy regardless of price.

    `available_count + taken_count` may not equal `total_checked` if any
    rows came back non-definitive — those go in `indeterminate_count`.
    """

    sld: str
    total_checked: int
    taken_count: int
    available_count: int
    indeterminate_count: int
    per_tld: dict[str, bool] = field(default_factory=dict)
    """Map of TLD -> is_taken. Keys are the TLDs from DEFAULT_TLDS_TO_CHECK
    (or whatever the caller passed). True = taken, False = available."""

    @property
    def spread_ratio(self) -> float:
        """Fraction of checked TLDs that are taken. 0.0 to 1.0."""
        if self.total_checked == 0:
            return 0.0
        return self.taken_count / self.total_checked


class AvailabilityClient:
    """Thin wrapper around the /v1/domains/available endpoint."""

    BULK_ENDPOINT_PATH = "/v1/domains/available"
    BULK_MAX_DOMAINS = 500  # GoDaddy's hard cap per call

    def __init__(self, client: GoDaddyClient):
        self.client = client

    async def check_availability(
        self,
        domains: list[str],
        check_type: str = "FAST",
    ) -> dict[str, DomainAvailability]:
        """Run a bulk availability check.

        Returns a dict mapping each input domain to its DomainAvailability
        record. Domains not present in the response (which can happen with
        malformed inputs) are omitted from the result map — the caller is
        responsible for handling missing keys.

        Splits the input into batches of up to BULK_MAX_DOMAINS if needed.
        """
        if not domains:
            return {}

        results: dict[str, DomainAvailability] = {}
        # Batch.
        for i in range(0, len(domains), self.BULK_MAX_DOMAINS):
            batch = domains[i : i + self.BULK_MAX_DOMAINS]
            batch_results = await self._check_batch(batch, check_type=check_type)
            results.update(batch_results)
        return results

    async def _check_batch(
        self,
        domains: list[str],
        check_type: str,
    ) -> dict[str, DomainAvailability]:
        url = (
            f"{self.client.config.rest_base_url}{self.BULK_ENDPOINT_PATH}"
            f"?checkType={check_type}"
        )
        response = await self.client._request(
            "POST",
            url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=domains,
        )

        if response.status_code >= 400:
            logger.warning(
                "availability check returned %d: %s",
                response.status_code,
                response.text[:500],
            )
            return {}

        try:
            payload = response.json()
        except ValueError:
            logger.error("availability response was not JSON: %s", response.text[:500])
            return {}

        # Response shape per docs: {"domains": [ {"domain", "available", "definitive", "price", "currency"} ]}
        rows = payload.get("domains") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            logger.error("availability response missing 'domains' array: %s", payload)
            return {}

        results: dict[str, DomainAvailability] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = row.get("domain")
            if not isinstance(d, str):
                continue
            results[d.lower()] = DomainAvailability(
                domain=d.lower(),
                available=bool(row.get("available", False)),
                definitive=bool(row.get("definitive", False)),
                price_micros=row.get("price") if isinstance(row.get("price"), int) else None,
                currency=row.get("currency") if isinstance(row.get("currency"), str) else None,
            )
        return results

    async def check_tld_spread(
        self,
        sld: str,
        tlds: Optional[list[str]] = None,
    ) -> TLDSpreadResult:
        """Check how many TLDs for `sld` are already registered.

        sld:  the second-level domain WITHOUT the dot, e.g. "picnic".
        tlds: list of TLDs to test against; defaults to DEFAULT_TLDS_TO_CHECK.

        Returns a TLDSpreadResult with the per-TLD breakdown.
        """
        sld = sld.lower().strip()
        if not sld or "." in sld:
            raise ValueError(f"sld must be the bare SLD without a dot; got {sld!r}")

        check_tlds = list(tlds) if tlds is not None else list(DEFAULT_TLDS_TO_CHECK)
        # De-dup while preserving order.
        seen = set()
        check_tlds = [t for t in check_tlds if not (t in seen or seen.add(t))]

        domains = [f"{sld}.{tld}" for tld in check_tlds]
        results = await self.check_availability(domains)

        taken = 0
        available = 0
        indeterminate = 0
        per_tld: dict[str, bool] = {}
        for tld in check_tlds:
            domain = f"{sld}.{tld}"
            row = results.get(domain)
            if row is None:
                indeterminate += 1
                continue
            if not row.definitive:
                # Non-definitive answers from FAST checks are unreliable — treat
                # as indeterminate rather than counting them toward taken/available.
                indeterminate += 1
                continue
            # The signal we actually want is "is this registered?" which is the
            # inverse of "available".
            is_taken = not row.available
            per_tld[tld] = is_taken
            if is_taken:
                taken += 1
            else:
                available += 1

        return TLDSpreadResult(
            sld=sld,
            total_checked=len(check_tlds),
            taken_count=taken,
            available_count=available,
            indeterminate_count=indeterminate,
            per_tld=per_tld,
        )

    async def check_tld_spread_many(
        self,
        slds: list[str],
        tlds: Optional[list[str]] = None,
        concurrency: int = 4,
        delay_between_calls_sec: float = 0.2,
    ) -> dict[str, TLDSpreadResult]:
        """Check TLD spread for a list of SLDs.

        Bounded concurrency + a small inter-call delay to be a polite API
        citizen — GoDaddy's domain-availability throttle is documented at
        ~60/min on the production tier we're on. With concurrency=4 and a
        200ms delay we stay well under that.
        """
        sem = asyncio.Semaphore(concurrency)
        out: dict[str, TLDSpreadResult] = {}

        async def _one(sld: str) -> None:
            async with sem:
                try:
                    result = await self.check_tld_spread(sld, tlds=tlds)
                    out[sld] = result
                except Exception as e:
                    logger.warning("tld spread failed for %s: %s", sld, e)
                if delay_between_calls_sec > 0:
                    await asyncio.sleep(delay_between_calls_sec)

        await asyncio.gather(*(_one(s) for s in slds))
        return out
