"""
Deribit REST API client — fetches the full BTC options chain in a single HTTP call.

Used for:
1. Initial bootstrap of MarketState on startup.
2. Periodic full refresh (every 5-10 min) to catch instruments the WS may have missed.
3. The headless btc_gex_worker.py cron script.

Endpoint: public/get_book_summary_by_currency (no auth required).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from ..core.state_manager import InstrumentData

# Deribit public REST base URL
DERIBIT_REST_URL = "https://www.deribit.com/api/v2"

# Month abbreviation -> month number lookup
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Regex for Deribit instrument names: BTC-29MAY26-96000-C
_INSTRUMENT_RE = re.compile(
    r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2,4})-(\d+)-([CP])$"
)


def _parse_instrument_name(name: str) -> tuple[str, str, float, str] | None:
    """
    Parse a Deribit instrument name into (currency, expiration_iso, strike, option_type).

    Example: 'BTC-29MAY26-96000-C' -> ('BTC', '2026-05-29', 96000.0, 'C')
    """
    m = _INSTRUMENT_RE.match(name)
    if not m:
        return None

    currency = m.group(1)
    day = int(m.group(2))
    month_str = m.group(3)
    year_short = m.group(4)
    strike = float(m.group(5))
    option_type = m.group(6)

    month = _MONTH_MAP.get(month_str)
    if month is None:
        return None

    # Handle 2-digit vs 4-digit year
    year = int(year_short)
    if year < 100:
        year += 2000

    exp_iso = f"{year:04d}-{month:02d}-{day:02d}"
    return currency, exp_iso, strike, option_type


async def fetch_all_btc_options(currency: str = "BTC") -> tuple[list[InstrumentData], float]:
    """
    Fetch the complete options chain for a currency from Deribit REST API.

    Returns
    -------
    tuple[list[InstrumentData], float]
        (list of parsed instruments, spot price)
    """
    url = f"{DERIBIT_REST_URL}/public/get_book_summary_by_currency"
    params = {"currency": currency, "kind": "option"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise ConnectionError(
            f"Deribit REST API returned {response.status_code}: {response.text}"
        )

    data = response.json()
    results = data.get("result", [])

    if not results:
        raise ValueError(f"No option instruments returned for {currency}")

    # Extract spot price from first instrument that has it
    spot_price = 0.0
    for item in results:
        edp = item.get("estimated_delivery_price") or item.get("underlying_price")
        if edp and edp > 0:
            spot_price = float(edp)
            break

    if spot_price <= 0:
        raise ValueError("Could not determine spot price from Deribit response")

    instruments: list[InstrumentData] = []

    for item in results:
        name = item.get("instrument_name", "")
        parsed = _parse_instrument_name(name)
        if parsed is None:
            continue

        _, exp_iso, strike, option_type = parsed

        oi = float(item.get("open_interest", 0))
        mark_iv = float(item.get("mark_iv", 0))

        # Convert IV from percentage (70.0) to decimal (0.70)
        if mark_iv > 1.0:
            mark_iv /= 100.0

        underlying = float(
            item.get("underlying_price", 0)
            or item.get("estimated_delivery_price", 0)
            or spot_price
        )

        instruments.append(InstrumentData(
            instrument_name=name,
            strike=strike,
            expiration=exp_iso,
            option_type=option_type,
            open_interest=oi,
            mark_iv=mark_iv,
            mark_price=float(item.get("mark_price", 0)),
            underlying_price=underlying,
            volume=float(item.get("volume", 0)),
            bid_price=float(item["bid_price"]) if item.get("bid_price") is not None else None,
            ask_price=float(item["ask_price"]) if item.get("ask_price") is not None else None,
        ))

    logger.info(
        f"Deribit REST: fetched {len(instruments)} instruments for {currency}, "
        f"spot=${spot_price:,.2f}"
    )

    return instruments, spot_price
