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
    from src.core.state_manager import MarketState
    from src.engine.gex_analytics import calculate_gex_from_snapshot
    from src.database.timescale import (
        get_db_pool, init_db, save_options_chain, 
        save_analytics_snapshot, get_latest_oi_snapshot
    )

    start = time.perf_counter()

    # 1. Fetch the full options chain from Deribit REST
    instruments, spot_price = await fetch_all_btc_options("BTC")

    # 2. Build a MarketState and take a snapshot
    state = MarketState()
    state.bulk_load_chain(instruments, spot_price)
    snapshot = state.get_snapshot()

    # 3. Database Operations (Historical OI and Persistence)
    oi_diff_by_instrument = {}
    db_pool = None
    try:
        db_pool = await get_db_pool()
        await init_db(db_pool)
        
        # Get past OI to compute flow (Dealer Positioning Estimation)
        past_oi = await get_latest_oi_snapshot(db_pool)
        
        # Calculate diffs
        for inst in instruments:
            if inst.instrument_name in past_oi:
                oi_diff_by_instrument[inst.instrument_name] = inst.open_interest - past_oi[inst.instrument_name]
                
    except Exception as e:
        # Graceful fallback: If DB is down, we still calculate GEX but without dynamic positioning
        print(f"DB Error (Fallback to static positioning): {e}", file=sys.stderr)

    # 4. Calculate GEX (with dynamic positioning if DB provided oi_diff)
    result = calculate_gex_from_snapshot(snapshot, oi_diff_by_instrument=oi_diff_by_instrument)

    # 5. Save Analytics Snapshot
    if db_pool and result.get("status", "ok") == "ok":
        try:
            ts = datetime.now(timezone.utc).isoformat()
            await save_options_chain(db_pool, instruments, spot_price, ts)
            await save_analytics_snapshot(db_pool, result, ts)
        except Exception as e:
            print(f"Failed to save to DB: {e}", file=sys.stderr)
        finally:
            await db_pool.close()

    elapsed_ms = (time.perf_counter() - start) * 1000
    result["computation_time_ms"] = round(elapsed_ms, 2)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    result["mode"] = "headless_rest"
    result["status"] = "ok"
    
    # Include metadata about dealer positioning
    result["dealer_positioning"] = "dynamic" if oi_diff_by_instrument else "static (fallback)"

    return result


def main() -> int:
    """Entry point with structured error handling."""
    # Garante que o Windows não quebre com caracteres UTF-8
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = asyncio.run(run())

        # Strip verbose profile data for the cron output to avoid flooding CMD buffer
        keys_to_strip = (
            "gamma_profile", "gex_by_strike", "call_gex_by_strike", "put_gex_by_strike",
            "vex_by_strike", "call_vex_by_strike", "put_vex_by_strike"
        )
        output = {k: v for k, v in result.items() if k not in keys_to_strip}

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
