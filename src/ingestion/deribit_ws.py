"""
Deribit WebSocket Consumer — Async I/O-only module.

Responsibilities:
1. Connect to Deribit WebSocket API (public, no auth needed).
2. Subscribe to `ticker.BTC-PERPETUAL.100ms` for real-time spot price.
3. Periodically trigger a REST refresh of the full options chain.
4. Feed all data into the shared MarketState.

This module does ZERO math — it only handles network I/O.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import websockets
from loguru import logger

from .deribit_rest import fetch_all_btc_options

if TYPE_CHECKING:
    from ..core.state_manager import MarketState

# Deribit public WebSocket endpoint
DERIBIT_WS_URL = "wss://www.deribit.com/ws/api/v2"

# How often to do a full REST refresh of the options chain (seconds)
REST_REFRESH_INTERVAL = 300  # 5 minutes


class DeribitWSConsumer:
    """
    Async WebSocket consumer for Deribit.

    Usage:
        state = MarketState()
        ws = DeribitWSConsumer(state)
        await ws.connect_and_listen()  # blocks forever
    """

    def __init__(self, state: MarketState, currency: str = "BTC"):
        self.state = state
        self.currency = currency
        self._msg_id = 0
        self._running = True

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def connect_and_listen(self) -> None:
        """
        Main loop with automatic reconnection on failure.
        """
        while self._running:
            try:
                await self._session()
            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)

    async def _session(self) -> None:
        """Single WebSocket session lifecycle."""
        logger.info(f"Connecting to Deribit WebSocket: {DERIBIT_WS_URL}")

        async with websockets.connect(
            DERIBIT_WS_URL,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("WebSocket connected. Bootstrapping chain via REST...")

            # Bootstrap: load full chain via REST before subscribing
            await self._rest_refresh()

            # Subscribe to perpetual ticker for spot price
            await self._subscribe_spot(ws)

            # Launch REST refresh task in background
            refresh_task = asyncio.create_task(self._periodic_rest_refresh())

            try:
                async for raw_msg in ws:
                    self._handle_message(raw_msg)
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

    async def _subscribe_spot(self, ws) -> None:
        """Subscribe to BTC-PERPETUAL ticker for real-time spot updates."""
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "public/subscribe",
            "params": {
                "channels": [f"ticker.{self.currency}-PERPETUAL.100ms"]
            }
        }
        await ws.send(json.dumps(subscribe_msg))
        logger.info(f"Subscribed to ticker.{self.currency}-PERPETUAL.100ms")

    def _handle_message(self, raw: str) -> None:
        """Parse incoming WS messages and update state."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Received non-JSON message: {raw[:100]}")
            return

        # Subscription notification (ticker data)
        if msg.get("method") == "subscription":
            params = msg.get("params", {})
            channel = params.get("channel", "")
            data = params.get("data", {})

            if "ticker" in channel and "PERPETUAL" in channel:
                # Extract spot price from perpetual mark price
                mark_price = data.get("mark_price")
                index_price = data.get("index_price")
                # Prefer index_price (actual spot), fallback to mark_price
                price = index_price or mark_price
                if price and price > 0:
                    self.state.update_spot(float(price))

        # Subscription confirmation (ignore)
        elif "result" in msg:
            pass
        # Error
        elif "error" in msg:
            logger.error(f"Deribit WS error: {msg['error']}")

    async def _rest_refresh(self) -> None:
        """Fetch full options chain via REST and bulk-load into state."""
        try:
            instruments, spot = await fetch_all_btc_options(self.currency)
            self.state.bulk_load_chain(instruments, spot)
            logger.info(
                f"REST refresh complete: {len(instruments)} instruments loaded, "
                f"spot=${spot:,.2f}"
            )
        except Exception as e:
            logger.error(f"REST refresh failed: {e}")

    async def _periodic_rest_refresh(self) -> None:
        """Background task: refresh full chain via REST every N seconds."""
        while True:
            await asyncio.sleep(REST_REFRESH_INTERVAL)
            await self._rest_refresh()

    def stop(self) -> None:
        """Signal the consumer to stop reconnecting."""
        self._running = False
