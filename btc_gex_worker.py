#!/usr/bin/env python3
"""
BTC GEX Worker — Headless REST-only script for cron jobs.

This script does NOT use WebSockets. It performs a single-shot:
1. Fetch all BTC options via Deribit REST API.
2. Calculate GEX using vectorized Black-Scholes.
3. Print structured JSON to stdout.
4. Exit cleanly (0 = success, 1 = failure).

Usage:
    python btc_gex_worker.py
    python btc_gex_worker.py >> /var/log/btc_gex.jsonl

Recommended cron schedule (every 5 minutes):
    */5 * * * * cd /path/to/btc-flow-monitor && python btc_gex_worker.py >> /var/log/btc_gex.jsonl 2>&1
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone


async def run() -> dict:
    """Execute a single GEX calculation cycle."""
    # Import here to allow the script to report import errors gracefully
    from src.ingestion.deribit_rest import fetch_all_btc_options
    from src.core.state_manager import MarketState, InstrumentData
    from src.engine.gex_analytics import calculate_gex_from_snapshot

    start = time.perf_counter()

    # 1. Fetch the full options chain from Deribit REST
    instruments, spot_price = await fetch_all_btc_options("BTC")

    # 2. Build a MarketState and take a snapshot
    state = MarketState()
    state.bulk_load_chain(instruments, spot_price)
    snapshot = state.get_snapshot()

    # 3. Calculate GEX
    result = calculate_gex_from_snapshot(snapshot)

    elapsed_ms = (time.perf_counter() - start) * 1000
    result["computation_time_ms"] = round(elapsed_ms, 2)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    result["mode"] = "headless_rest"
    result["status"] = "ok"

    return result


def main() -> int:
    """Entry point with structured error handling."""
    try:
        result = asyncio.run(run())

        # Strip verbose profile data for the cron output
        output = {k: v for k, v in result.items()
                  if k not in ("gamma_profile", "gex_by_strike",
                               "call_gex_by_strike", "put_gex_by_strike")}

        print(json.dumps(output, indent=4))
        return 0

    except Exception as e:
        error_payload = {
            "asset": "BTC/USD",
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "headless_rest",
        }
        print(json.dumps(error_payload, indent=4))
        return 1


if __name__ == "__main__":
    sys.exit(main())
