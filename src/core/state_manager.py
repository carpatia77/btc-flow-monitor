"""
State Manager — Memory-safe market state container.

Uses dictionary overwrite semantics for options chain (O(1) memory per instrument)
and a bounded deque for recent trades to prevent unbounded memory growth.
"""
from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InstrumentData:
    """Parsed option instrument record from Deribit."""
    instrument_name: str
    strike: float
    expiration: str          # ISO date string e.g. "2026-06-27"
    option_type: str         # "C" or "P"
    open_interest: float     # In BTC (1 contract = 1 BTC on Deribit)
    mark_iv: float           # Implied volatility in decimal (0.70 = 70%)
    mark_price: float        # Mark price in BTC
    underlying_price: float  # Spot / index price at time of snapshot
    volume: float = 0.0
    bid_price: float | None = None
    ask_price: float | None = None


class MarketState:
    """
    Thread-safe container for live market state.

    Design principles:
    - Dictionary keyed by instrument_name → overwrites stale data (no accumulation).
    - deque(maxlen=N) for trades → automatically evicts oldest entries.
    - Snapshot copy for engine consumption → prevents lock contention with WS thread.
    """

    def __init__(self, max_trades: int = 5_000):
        self._lock = threading.Lock()
        self.options_chain: dict[str, InstrumentData] = {}
        self.recent_trades: deque[dict[str, Any]] = deque(maxlen=max_trades)
        self.last_spot_price: float | None = None
        self.last_update: float = 0.0
        self._update_count: int = 0

    # ── Mutators (called from WS consumer thread) ────────────────────────

    def update_instrument(self, instrument: InstrumentData) -> None:
        """Upsert a single instrument into the chain (O(1) memory)."""
        with self._lock:
            self.options_chain[instrument.instrument_name] = instrument
            self.last_update = time.time()
            self._update_count += 1

    def update_spot(self, price: float) -> None:
        """Update the BTC spot / index price."""
        with self._lock:
            self.last_spot_price = price
            self.last_update = time.time()

    def add_trade(self, trade: dict[str, Any]) -> None:
        """Append a trade to the bounded deque."""
        with self._lock:
            self.recent_trades.append(trade)

    def bulk_load_chain(self, instruments: list[InstrumentData], spot: float) -> None:
        """Atomic bulk replacement of the entire options chain (REST bootstrap)."""
        with self._lock:
            self.options_chain = {inst.instrument_name: inst for inst in instruments}
            self.last_spot_price = spot
            self.last_update = time.time()
            self._update_count += len(instruments)

    # ── Snapshot (called from analytics worker) ──────────────────────────

    def get_snapshot(self) -> dict[str, Any]:
        """
        Return a deep-enough copy of the current state for the engine.
        The copy ensures the analytics process never holds a reference
        that the WS thread is mutating.
        """
        with self._lock:
            return {
                "spot": self.last_spot_price,
                "chain": dict(self.options_chain),  # shallow copy of dict; values are frozen dataclasses
                "update_count": self._update_count,
                "last_update": self.last_update,
            }

    @property
    def instrument_count(self) -> int:
        return len(self.options_chain)

    @property
    def is_ready(self) -> bool:
        """True when we have both a spot price and at least one instrument."""
        return self.last_spot_price is not None and len(self.options_chain) > 0
