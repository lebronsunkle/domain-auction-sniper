"""
SOAP client for GoDaddy closeout purchases.

The auctions REST API does not expose closeout buy endpoints. The SOAP API
at https://auctions.godaddy.com/gdAuctionsWSAPI/gdAuctionsBiddingWS_v2.asmx
handles them via a two-step flow:

  1. EstimateCloseoutDomainPrice -> returns (price breakdown, time-limited price key)
  2. (Step 2 to be wired in once we have access to the action name from GoDaddy.)

Both calls use the same sso-key auth header as REST. The request body is a
SOAP XML envelope; responses are XML that we parse with lxml.

NOTE: The training transcript described the two-step flow but the second
action name ("ExecuteCloseoutPurchase" or similar) wasn't quoted verbatim.
The estimate call is fully wired here; the execute call is a stub that we'll
fill in during Phase 0 once we either confirm the action name with GoDaddy
support or observe a successful call in OTE.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .client import GoDaddyClient
from .micro_units import micros_to_dollars

logger = logging.getLogger(__name__)


SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body>{body}</soap:Body>"
    "</soap:Envelope>"
)

NS = "GdAuctionsBiddingWSAPI_v2"


@dataclass
class CloseoutEstimate:
    """Result of EstimateCloseoutDomainPrice."""

    domain_name: str
    success: bool
    listing_price_micros: Optional[int] = None
    renewal_micros: Optional[int] = None
    icann_fee_micros: Optional[int] = None
    tax_micros: Optional[int] = None
    total_micros: Optional[int] = None
    price_key: Optional[str] = None  # closeoutDomainPriceKey
    failure_message: Optional[str] = None

    @property
    def total_dollars(self) -> Optional[Decimal]:
        return micros_to_dollars(self.total_micros) if self.total_micros is not None else None

    @property
    def listing_price_dollars(self) -> Optional[Decimal]:
        return (
            micros_to_dollars(self.listing_price_micros)
            if self.listing_price_micros is not None
            else None
        )


class SoapClient:
    """Thin wrapper around the SOAP closeout endpoints."""

    def __init__(self, client: GoDaddyClient):
        self.client = client

    async def estimate_closeout_price(
        self, domain_name: str, add_privacy: bool = False
    ) -> CloseoutEstimate:
        """Step 1 of the two-step closeout flow.

        Returns a CloseoutEstimate. On success, the price_key is set and the
        full breakdown is populated. On failure (domain not currently in
        closeout status), failure_message is populated and success is False.
        """
        body = (
            f'<EstimateCloseoutDomainPrice xmlns="{NS}">'
            f"<domainName>{_xml_escape(domain_name)}</domainName>"
            f"<addPrivacy>{'true' if add_privacy else 'false'}</addPrivacy>"
            f"</EstimateCloseoutDomainPrice>"
        )
        envelope = SOAP_ENVELOPE.format(body=body)

        response = await self.client._request(
            "POST",
            self.client.config.soap_endpoint_url,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{NS}/EstimateCloseoutDomainPrice",
            },
            content=envelope,
        )

        # GoDaddy serializes the typed result as an entity-escaped XML string
        # inside <EstimateCloseoutDomainPriceResult>...</...>. Unwrap and
        # html-unescape so we're parsing the actual inner element.
        inner = _extract_inner_result(response.text)

        # GoDaddy's failure format:
        #   <EstimateCloseoutDomainPrice Result="Failure" Message="..."/>
        fail_match = re.search(
            r'<EstimateCloseoutDomainPrice[^>]*Result="Failure"[^>]*Message="([^"]*)"',
            inner,
        )
        if fail_match:
            return CloseoutEstimate(
                domain_name=domain_name,
                success=False,
                failure_message=fail_match.group(1),
            )

        # Success path — extract fields from XML ATTRIBUTES on the self-closing
        # <EstimateCloseoutDomainPrice .../> tag. Confirmed against the
        # 2026-05-29 capture of optibold.com:
        #   <EstimateCloseoutDomainPrice
        #       Result="Success" Domain="optibold.com" Price="$5.00"
        #       TransferPrice="$10.51" ICANNFee="$0.18" Taxes="$0.39"
        #       PrivateRegistration="N/A" Total="$16.08"
        #       closeoutDomainPriceKey="NwAwA..." />
        # Notes:
        #   - Prices arrive as dollar strings ("$5.00"), not micro-units
        #   - The PriceKey may have whitespace before the `=` sign
        #   - PrivateRegistration="N/A" contains a slash; our match needs to
        #     allow slashes inside attribute values (only stop at the closing
        #     `/>` or `>`)
        # We grab the whole tag contents non-greedily (anything not `<`),
        # then check for `Result="Success"` and extract attrs from that blob.
        tag_match = re.search(
            r'<EstimateCloseoutDomainPrice\b([^<]*?)/?>',
            inner,
            re.DOTALL,
        )
        if not tag_match or 'Result="Success"' not in tag_match.group(1):
            return CloseoutEstimate(
                domain_name=domain_name,
                success=False,
                failure_message=(
                    "Response had no recognizable Result attribute; schema "
                    "drift? Inner XML: " + inner[:500]
                ),
            )
        attrs_blob = tag_match.group(1)

        def _attr_str(name: str) -> Optional[str]:
            # Allow optional whitespace around `=` -- we've seen it in the wild.
            m = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', attrs_blob)
            return m.group(1) if m else None

        def _attr_dollars(name: str) -> Optional[Decimal]:
            raw = _attr_str(name)
            if raw is None or raw == "" or raw == "N/A":
                return None
            cleaned = raw.replace("$", "").replace(",", "").strip()
            try:
                return Decimal(cleaned)
            except Exception:
                return None

        domain_attr = _attr_str("Domain") or domain_name

        estimate = CloseoutEstimate(
            domain_name=domain_attr,
            success=True,
            listing_price_micros=_dollars_to_micros(_attr_dollars("Price")),
            renewal_micros=_dollars_to_micros(_attr_dollars("TransferPrice")),
            icann_fee_micros=_dollars_to_micros(_attr_dollars("ICANNFee")),
            tax_micros=_dollars_to_micros(_attr_dollars("Taxes")),
            total_micros=_dollars_to_micros(_attr_dollars("Total")),
            price_key=_attr_str("closeoutDomainPriceKey") or _attr_str("priceKey"),
        )

        # Defensive: if nothing extracted, the response shape changed.
        if estimate.price_key is None and estimate.total_micros is None:
            return CloseoutEstimate(
                domain_name=domain_attr,
                success=False,
                failure_message=(
                    "Response parsed but no known fields populated — likely a "
                    "schema we haven't mapped yet. Inner XML: " + inner[:500]
                ),
            )
        return estimate

    async def execute_closeout_purchase(
        self,
        domain_name: str,
        price_key: str,
        *,
        accept_utos: bool = True,
        accept_ama: bool = True,
        accept_dnra: bool = True,
    ) -> "CloseoutPurchaseResult":
        """Step 2 of the closeout flow: ACTUALLY BUY the domain.

        Calls the SOAP action `InstantPurchaseCloseoutDomain` — confirmed
        as the correct action name on 2026-06-03 by reading the public
        ASMX operation listing at
        https://auctions.godaddy.com/gdAuctionsWSAPI/gdAuctionsBiddingWS_v2.asmx

        Request body (per the WSDL sample):
            <InstantPurchaseCloseoutDomain xmlns="GdAuctionsBiddingWSAPI_v2">
              <domainName>{domain}</domainName>
              <closeoutDomainPriceKey>{key from estimate}</closeoutDomainPriceKey>
              <acceptUTOS>true</acceptUTOS>     # Universal Terms of Service
              <acceptAMA>true</acceptAMA>       # Auction Master Agreement
              <acceptDNRA>true</acceptDNRA>     # Domain Name Registration Agreement
            </InstantPurchaseCloseoutDomain>

        All three accept flags default to True — the buyer must consent to
        all three for the purchase to go through. Defaulting to True is
        OK here because: (a) the user/operator explicitly initiated the
        buy via our UI, (b) these are standard GoDaddy ToS that they've
        already accepted at account-creation time, (c) the safety
        governors already gate whether the buy happens at all.
        """
        if not price_key:
            return CloseoutPurchaseResult(
                success=False,
                failure_message="No price_key provided. Call estimate_closeout_price first.",
            )

        body = (
            f'<InstantPurchaseCloseoutDomain xmlns="{NS}">'
            f"<domainName>{_xml_escape(domain_name)}</domainName>"
            f"<closeoutDomainPriceKey>{_xml_escape(price_key)}</closeoutDomainPriceKey>"
            f"<acceptUTOS>{'true' if accept_utos else 'false'}</acceptUTOS>"
            f"<acceptAMA>{'true' if accept_ama else 'false'}</acceptAMA>"
            f"<acceptDNRA>{'true' if accept_dnra else 'false'}</acceptDNRA>"
            f"</InstantPurchaseCloseoutDomain>"
        )
        envelope = SOAP_ENVELOPE.format(body=body)

        response = await self.client._request(
            "POST",
            self.client.config.soap_endpoint_url,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{NS}/InstantPurchaseCloseoutDomain",
            },
            content=envelope,
            # THIS CALL SPENDS MONEY. Never auto-retry after a timeout/5xx —
            # GoDaddy may have already processed the purchase (audit R2).
            idempotent=False,
        )

        if response.status_code != 200:
            return CloseoutPurchaseResult(
                success=False,
                failure_message=(
                    f"HTTP {response.status_code}: {response.text[:500]}"
                ),
            )

        # The response wraps an XML-encoded result inside
        # <InstantPurchaseCloseoutDomainResult>...</InstantPurchaseCloseoutDomainResult>.
        # The inner content is entity-escaped, same pattern as estimate.
        m = re.search(
            r"<InstantPurchaseCloseoutDomainResult>(.*?)</InstantPurchaseCloseoutDomainResult>",
            response.text,
            re.DOTALL,
        )
        if not m:
            return CloseoutPurchaseResult(
                success=False,
                failure_message=(
                    "Response had no <InstantPurchaseCloseoutDomainResult> tag. "
                    f"Body: {response.text[:500]}"
                ),
            )
        inner = html.unescape(m.group(1))

        # The response shape follows the same attribute-style pattern as
        # the estimate response. Likely shapes:
        #   <InstantPurchaseCloseoutDomain Result="Success" OrderID="123" ... />
        #   <InstantPurchaseCloseoutDomain Result="Failure" Message="..." />
        # We parse defensively — log the full inner XML if it doesn't match
        # so we can refine on first real call.
        tag_match = re.search(
            r"<InstantPurchaseCloseoutDomain\b([^<]*?)/?>",
            inner,
            re.DOTALL,
        )
        if not tag_match:
            return CloseoutPurchaseResult(
                success=False,
                failure_message=(
                    "Response did not match expected attribute-style shape. "
                    f"Inner XML: {inner[:500]}"
                ),
            )
        attrs_blob = tag_match.group(1)

        def _attr(name: str) -> Optional[str]:
            m = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', attrs_blob)
            return m.group(1) if m else None

        result_str = _attr("Result")
        success = result_str == "Success"
        order_id = _attr("OrderID") or _attr("orderId") or _attr("OrderId")
        failure_message = _attr("Message") if not success else None

        return CloseoutPurchaseResult(
            success=success,
            order_id=order_id,
            failure_message=failure_message or (None if success else f"Result={result_str}, inner={inner[:300]}"),
        )

    async def get_auction_details_by_domain_name(
        self, domain_name: str
    ) -> Optional[str]:
        """Look up auction metadata for any domain that's currently in a
        GoDaddy auction. Returns the unescaped inner XML on success, or
        None if the domain isn't currently in an auction.

        Confirmed as a valid SOAP operation on the auctions endpoint
        (2026-06-03 WSDL inspection). This is what unlocks the "search
        any domain" feature in our dashboard — no listing_id required.
        """
        body = (
            f'<GetAuctionDetailsByDomainName xmlns="{NS}">'
            f"<domainName>{_xml_escape(domain_name)}</domainName>"
            f"</GetAuctionDetailsByDomainName>"
        )
        envelope = SOAP_ENVELOPE.format(body=body)
        response = await self.client._request(
            "POST",
            self.client.config.soap_endpoint_url,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{NS}/GetAuctionDetailsByDomainName",
            },
            content=envelope,
        )
        if response.status_code != 200:
            return None
        m = re.search(
            r"<GetAuctionDetailsByDomainNameResult>(.*?)</GetAuctionDetailsByDomainNameResult>",
            response.text,
            re.DOTALL,
        )
        if not m:
            return None
        return html.unescape(m.group(1))


@dataclass
class CloseoutPurchaseResult:
    """Result of the (still-stubbed) execute step."""

    success: bool
    order_id: Optional[str] = None
    failure_message: Optional[str] = None


def _extract_inner_result(envelope_text: str) -> str:
    """Pull the typed result out of <...Result>...</...Result> and html-unescape.

    GoDaddy's ASMX serializes the EstimateCloseoutDomainPrice return value as
    an entity-encoded XML string wrapped in <EstimateCloseoutDomainPriceResult>.
    Returns the inner XML, unescaped. If no wrapper is present, falls back to
    the original text (still html-unescaped) so the regex matchers also work
    against the unwrapped shape documented in the training handout.
    """
    m = re.search(
        r"<EstimateCloseoutDomainPriceResult>(.*?)</EstimateCloseoutDomainPriceResult>",
        envelope_text,
        re.DOTALL,
    )
    payload = m.group(1) if m else envelope_text
    return html.unescape(payload)


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _dollars_to_micros(d: Optional[Decimal]) -> Optional[int]:
    """Convert a Decimal dollar value to GoDaddy's micro-units (1 USD = 1,000,000).
    Returns None for None input. Used for the estimate response which arrives
    as dollar strings rather than micros."""
    if d is None:
        return None
    # micros = dollars * 1_000_000, rounded to int
    return int((d * Decimal("1000000")).quantize(Decimal("1")))
