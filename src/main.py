"""
BTC Flow Monitor — Async Orchestrator

Architecture:
- websocket_worker: I/O-bound task that listens to Deribit and populates MarketState.
- analytics_worker: CPU-bound task that periodically snapshots state, offloads
  vectorized Black-Scholes + GEX calculation to a ProcessPoolExecutor,
  and emits structured JSON results.

The two workers are fully decoupled via MarketState. The GIL is bypassed
for heavy math by using a separate process.
"""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor

from loguru import logger

from .core.state_manager import MarketState
from .ingestion.deribit_ws import DeribitWSConsumer
from .engine.gex_analytics import run_heavy_math_analysis

# ── Configuration ────────────────────────────────────────────────────────
ANALYTICS_INTERVAL_SECONDS = 60    # Recalculate GEX every 60 seconds
MAX_PROCESS_WORKERS = 2            # Number of CPU cores for math


async def websocket_worker(state: MarketState) -> None:
    """
    Dedicated task for WebSocket I/O.
    Runs forever, populating the shared MarketState.
    """
    ws = DeribitWSConsumer(state)
    logger.info("🔌 Starting WebSocket Worker (I/O-bound)...")
    await ws.connect_and_listen()


async def analytics_worker(state: MarketState, pool: ProcessPoolExecutor) -> None:
    """
    Dedicated task for math orchestration.
    Periodically takes a snapshot and dispatches heavy computation
    to a separate process via the executor pool.
    """
    logger.info(
        f"📊 Starting Analytics Worker (CPU-bound via ProcessPool). "
        f"Interval: {ANALYTICS_INTERVAL_SECONDS}s"
    )

    while True:
        await asyncio.sleep(ANALYTICS_INTERVAL_SECONDS)

        if not state.is_ready:
            logger.warning(
                f"⏳ Awaiting sufficient data... "
                f"(instruments: {state.instrument_count}, spot: {state.last_spot_price})"
            )
            continue

        # Take an immutable snapshot for the engine
        snapshot = state.get_snapshot()

        # Convert InstrumentData objects to a serializable format for the process pool
        serializable_snapshot = _serialize_snapshot(snapshot)

        start_time = time.perf_counter()

        # Dispatch to a separate process (GIL-free)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            pool,
            run_heavy_math_analysis,
            serializable_snapshot,
        )

        wall_time = (time.perf_counter() - start_time) * 1000

        if result.get("status") == "ok":
            logger.info(
                f"✅ GEX Recalculated | "
                f"Spot: ${result['spot_price']:,.2f} | "
                f"Regime: {result['gex_regime']} | "
                f"Total GEX: ${result['total_gex_usd']:,.0f} | "
                f"Call Wall: {result['call_wall_strike']:,.0f} | "
                f"Put Wall: {result['put_wall_strike']:,.0f} | "
                f"Flip: {result.get('gamma_flip', 'N/A')} | "
                f"Engine: {result['computation_time_ms']:.1f}ms | "
                f"Wall: {wall_time:.1f}ms | "
                f"Instruments: {result['instruments_analyzed']}"
            )

            # Emit JSON to stdout (pipe-ready for downstream consumers)
            # Strip the large gamma_profile for console output
            console_result = {k: v for k, v in result.items()
                             if k not in ("gamma_profile", "gex_by_strike",
                                          "call_gex_by_strike", "put_gex_by_strike")}
            print(json.dumps(console_result, indent=2))
        else:
            logger.error(f"❌ GEX calculation failed: {result.get('error')}")


def _serialize_snapshot(snapshot: dict) -> dict:
    """
    Convert InstrumentData dataclass instances to plain dicts
    so they can be pickled and sent to the ProcessPoolExecutor.
    """
    from dataclasses import asdict
    chain = snapshot["chain"]
    serialized_chain = {}
    for name, inst in chain.items():
        serialized_chain[name] = inst  # InstrumentData is a dataclass, pickle-safe
    return {
        "spot": snapshot["spot"],
        "chain": serialized_chain,
        "update_count": snapshot["update_count"],
        "last_update": snapshot["last_update"],
    }


async def main() -> None:
    """Entry point: launch both workers concurrently."""
    state = MarketState()
    executor = ProcessPoolExecutor(max_workers=MAX_PROCESS_WORKERS)

    logger.info("🚀 BTC Flow Monitor starting...")
    logger.info(f"   Analytics interval: {ANALYTICS_INTERVAL_SECONDS}s")
    logger.info(f"   Process pool workers: {MAX_PROCESS_WORKERS}")

    try:
        await asyncio.gather(
            websocket_worker(state),
            analytics_worker(state, executor),
        )
    except KeyboardInterrupt:
        logger.info("🛑 Shutdown requested by user.")
    finally:
        executor.shutdown(wait=False)
        logger.info("🏁 BTC Flow Monitor stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 System terminated successfully.")
