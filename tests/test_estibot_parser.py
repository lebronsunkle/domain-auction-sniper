"""Estibot response parsing — pinned against the REAL live envelope
captured 2026-07-14 from public-api.estibot.com (picnic.com appraisal).

History: the integration was blocked for a month on an IP whitelist that
only applied to the legacy endpoint. The first successful live call
revealed the response nests rows under results.data (an object), while
Estibot's own GitHub docs show results as a flat array. These tests pin
BOTH shapes so a future docs-vs-live drift doesn't silently zero out the
appraisals again."""

from __future__ import annotations

from decimal import Decimal

from app.valuation.estibot import _extract_rows, _parse_appraisal_row

# Trimmed but structurally exact copy of the live 2026-07-14 response.
LIVE_ENVELOPE = {
    "success": True,
    "message": "",
    "results": {
        "data": [
            {
                "id": "1906654",
                "domain": "picnic.com",
                "sld": "picnic",
                "tld": "com",
                "appraised_value": 1644000,          # int in live response!
                "appraised_wholesale_value": "91712",  # str in live response
                "category": "Food",
                "category_root": "Food",
                "extensions_taken": "6",
                "com_taken": "1",
                "num_words": "1",
                "sld_length": "6",
                "keyword_exact_global_search_volume": "823000",
                "cached": 1,
            }
        ],
        "total": 1,
        "count": 1,
    },
    "cache": True,
    "bulk": False,
    "item_count": 1,
    "not_found": [],
    "result_count": 1,
}

# The shape the GitHub docs show (results as flat array) — also supported.
DOCS_ENVELOPE = {
    "success": True,
    "message": "",
    "results": [{"domain": "test.com", "appraised_value": "574368"}],
    "cache": True,
}


def test_live_envelope_rows_extracted():
    rows = _extract_rows(LIVE_ENVELOPE)
    assert len(rows) == 1
    assert rows[0]["domain"] == "picnic.com"


def test_docs_envelope_rows_extracted():
    rows = _extract_rows(DOCS_ENVELOPE)
    assert len(rows) == 1
    assert rows[0]["domain"] == "test.com"


def test_appraisal_row_parses_mixed_int_and_str_values():
    row = _extract_rows(LIVE_ENVELOPE)[0]
    a = _parse_appraisal_row("picnic.com", row)
    assert a.appraised_value == Decimal("1644000")
    assert a.appraised_wholesale_value == Decimal("91712")
    assert a.category == "Food"
    assert a.extensions_taken == 6


def test_error_envelope_yields_no_rows():
    err = {
        "success": False,
        "message": "Invalid API key.",
        "redirect_url": "",
        "results": {"total": 0, "count": 0, "start": 0, "end": 0, "data": []},
    }
    assert _extract_rows(err) == []
