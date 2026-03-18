"""
feeds/binance.py — Binance WebSocket handler

Connects to wss://stream.binance.com:9443/ws/btcusdt@ticker and
maintains a real-time rolling price window used by the strategy.

Public interface:
    BinanceFeed.price          — latest BTC/USDT mid price (float | None)
    BinanceFeed.price_window   — deque of (timestamp, price) tuples
    BinanceFeed.run()          — coroutine; keeps connection alive forever
"""

import asyncio
import json
import time
from collections import deque
from typing import Deque, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

from logger import log, log_error


class BinanceFeed:
    """
    Maintains a live connection to the Binance BTC/USDT ticker stream.
    Stores a rolling window of (unix_timestamp, price) for the strategy
    to compute N-second price changes without any REST polling.
    """

    WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    # Keep enough history for the longest window we'd ever need (120 s)
    WINDOW_SIZE = 300

    def __init__(self, price_window_seconds: int = 30):
        self.price_window_seconds = price_window_seconds
        # Most recent price
        self.price: Optional[float] = None
        # Rolling deque of (unix_ts_float, price_float)
        self.price_window: Deque[Tuple[float, float]] = deque(
            maxlen=self.WINDOW_SIZE
        )
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def price_n_seconds_ago(self, n: int) -> Optional[float]:
        """
        Return the closest price we have from ~n seconds ago.
        Returns None if the window doesn't yet have old enough data.
        """
        cutoff = time.time() - n
        # Walk from oldest to newest; return last price before cutoff
        best: Optional[float] = None
        for ts, px in self.price_window:
            if ts <= cutoff:
                best = px
            else:
                break  # deque is chronological; once past cutoff we're done
        return best

    async def run(self) -> None:
        """
        Persistent connection loop. Reconnects automatically on any error.
        Should be started with asyncio.create_task().
        """
        backoff = 1
        while True:
            try:
                log.info(f"[Binance] Connecting to {self.WS_URL}")
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    backoff = 1  # reset on successful connect
                    log.info("[Binance] Connected. Streaming BTC/USDT ticker...")
                    async for raw in ws:
                        await self._handle_message(raw)
            except ConnectionClosed as e:
                self._connected = False
                log_error(f"[Binance] WebSocket closed: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                self._connected = False
                log_error(f"[Binance] Unexpected error: {e}. Reconnecting in {backoff}s...", e)
            finally:
                self._connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # exponential back-off, cap at 60s

    async def _handle_message(self, raw: str) -> None:
        """Parse a Binance ticker message and update internal state."""
        try:
            data = json.loads(raw)
            # Binance ticker fields: "c" = last price, "b" = best bid, "a" = best ask
            last_price = float(data.get("c") or data.get("b") or 0)
            if last_price <= 0:
                return
            now = time.time()
            self.price = last_price
            self.price_window.append((now, last_price))
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            log_error(f"[Binance] Failed to parse message: {raw[:200]}", e)
