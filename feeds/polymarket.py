"""
feeds/polymarket.py — Polymarket CLOB WebSocket + REST handler

Responsibilities:
  1. On startup: fetch the active 5-minute BTC Up/Down market via REST
  2. Maintain a live WebSocket subscription to that market's order book
  3. Expose current UP/DOWN token mid-prices and best bid/ask spread
  4. Expose market expiry time (for settlement guard)

Public interface:
    PolymarketFeed.market_id         — active 5-min market condition ID
    PolymarketFeed.up_price          — current UP token mid (0-1)
    PolymarketFeed.down_price        — current DOWN token mid (0-1)
    PolymarketFeed.spread            — best ask - best bid for UP token
    PolymarketFeed.market_end_ts     — unix timestamp of market close
    PolymarketFeed.up_token_id       — token ID for UP (needed for orders)
    PolymarketFeed.down_token_id     — token ID for DOWN
    PolymarketFeed.run()             — coroutine; keeps WS alive forever
    PolymarketFeed.fetch_active_market() — one-shot REST call at startup
"""

import asyncio
import json
import time
from typing import Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from logger import log, log_error

# ── Polymarket endpoint constants ────────────────────────────────────────────
CLOB_REST = "https://clob.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# Polymarket tags / slugs used to identify the active 5-min BTC market
BTC_MARKET_KEYWORDS = ["bitcoin", "btc", "5-minute", "5 minute", "5min"]


class PolymarketFeed:
    """
    Connects to Polymarket's CLOB WebSocket and maintains live order-book
    state for the active 5-minute BTC Up/Down prediction market.
    """

    def __init__(self, clob_rest_url: str = CLOB_REST, clob_ws_url: str = CLOB_WS):
        self.clob_rest_url = clob_rest_url
        self.clob_ws_url = clob_ws_url

        # Market identifiers (populated by fetch_active_market)
        self.market_id: Optional[str] = None         # condition_id
        self.up_token_id: Optional[str] = None
        self.down_token_id: Optional[str] = None
        self.market_end_ts: Optional[float] = None   # unix timestamp

        # Live prices (0.0 – 1.0)
        self.up_price: Optional[float] = None
        self.down_price: Optional[float] = None

        # Order book spread for the UP token (ask - bid)
        self.spread: Optional[float] = None

        # Best bid / ask (UP token) for spread calculation
        self._best_bid: Optional[float] = None
        self._best_ask: Optional[float] = None

        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── REST: find active 5-minute BTC market ────────────────────────────────

    async def fetch_active_market(self) -> bool:
        """
        Query Polymarket CLOB REST API to find the currently active
        5-minute BTC Up/Down market. Populates market_id, token IDs,
        and market_end_ts.

        Returns True on success, False if no market found.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Get all active markets (paginated; limit 100 for BTC slug)
                url = f"{self.clob_rest_url}/markets"
                params = {"active": "true", "closed": "false", "limit": 100}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log_error(f"[Polymarket] REST /markets returned {resp.status}")
                        return False
                    data = await resp.json()

            markets = data.get("data", []) if isinstance(data, dict) else data
            log.info(f"[Polymarket] Fetched {len(markets)} active markets from REST")

            best_market = self._pick_btc_5min_market(markets)
            if best_market is None:
                log_error("[Polymarket] No active 5-minute BTC market found")
                return False

            self._populate_from_market(best_market)
            log.info(
                f"[Polymarket] Active market: {self.market_id} | "
                f"UP={self.up_token_id} DOWN={self.down_token_id} | "
                f"Ends: {self.market_end_ts}"
            )
            return True

        except Exception as e:
            log_error("[Polymarket] fetch_active_market failed", e)
            return False

    def _pick_btc_5min_market(self, markets: list) -> Optional[dict]:
        """
        Score candidate markets; pick the one that looks most like the
        active 5-minute BTC Up/Down market.
        """
        candidates = []
        for m in markets:
            title = (m.get("question") or m.get("title") or "").lower()
            description = (m.get("description") or "").lower()
            combined = title + " " + description
            # Must contain BTC/bitcoin AND 5-minute indicator
            has_btc = any(kw in combined for kw in ["bitcoin", "btc"])
            has_5min = any(kw in combined for kw in ["5-minute", "5 minute", "5min", "five minute"])
            is_updown = any(kw in combined for kw in ["up", "down", "above", "below", "higher", "lower"])
            if has_btc and has_5min and is_updown:
                # Prefer the market expiring soonest (closest active window)
                end_date = m.get("end_date_iso") or m.get("end_date") or ""
                candidates.append((end_date, m))

        if not candidates:
            return None
        # Sort by expiry ascending; pick the next-to-expire (currently active)
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _populate_from_market(self, market: dict) -> None:
        """Extract condition_id, token IDs, and expiry from a market dict."""
        self.market_id = market.get("condition_id") or market.get("id")
        self.market_end_ts = self._parse_end_ts(
            market.get("end_date_iso") or market.get("end_date")
        )
        tokens = market.get("tokens", [])
        for token in tokens:
            outcome = (token.get("outcome") or "").upper()
            if outcome == "YES" or outcome == "UP":
                self.up_token_id = token.get("token_id")
            elif outcome == "NO" or outcome == "DOWN":
                self.down_token_id = token.get("token_id")
        # Fallback: assign by index if outcomes aren't labeled
        if tokens and self.up_token_id is None and len(tokens) >= 2:
            self.up_token_id = tokens[0].get("token_id")
            self.down_token_id = tokens[1].get("token_id")

    @staticmethod
    def _parse_end_ts(end_date_str: Optional[str]) -> Optional[float]:
        if not end_date_str:
            return None
        try:
            from datetime import datetime, timezone
            # Handle ISO format with or without timezone
            end_date_str = end_date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(end_date_str)
            return dt.timestamp()
        except Exception:
            return None

    # ── WebSocket: live order book ───────────────────────────────────────────

    async def run(self) -> None:
        """
        Persistent WebSocket loop. Subscribes to the active market's
        order book and maintains live price state.
        Reconnects automatically; re-discovers active market on each reconnect
        (market windows roll over every 5 minutes).
        """
        backoff = 1
        while True:
            # Always refresh market identity before connecting
            if self.market_id is None:
                ok = await self.fetch_active_market()
                if not ok:
                    log_error("[Polymarket] Cannot start WS: no active market. Retrying in 15s")
                    await asyncio.sleep(15)
                    continue

            try:
                log.info(f"[Polymarket] Connecting WS for market {self.market_id}")
                async with websockets.connect(
                    self.clob_ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    backoff = 1

                    # Subscribe to order book updates for both tokens
                    sub_msg = {
                        "type": "subscribe",
                        "channel": "order_book",
                        "assets": [self.up_token_id, self.down_token_id],
                    }
                    await ws.send(json.dumps(sub_msg))
                    log.info(f"[Polymarket] Subscribed to order book for {self.market_id}")

                    async for raw in ws:
                        await self._handle_message(raw)

            except ConnectionClosed as e:
                self._connected = False
                log_error(f"[Polymarket] WS closed: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                self._connected = False
                log_error(f"[Polymarket] WS error: {e}. Reconnecting in {backoff}s...", e)
            finally:
                self._connected = False

            # After a 5-min window closes, force re-discovery of the next market
            self.market_id = None
            self.up_token_id = None
            self.down_token_id = None
            self.market_end_ts = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _handle_message(self, raw: str) -> None:
        """Parse Polymarket CLOB order book message and update prices/spread."""
        try:
            msg = json.loads(raw)
            event_type = msg.get("event_type") or msg.get("type") or ""

            # Handle both array and single-object message formats
            events = msg if isinstance(msg, list) else [msg]

            for event in events:
                asset_id = event.get("asset_id") or event.get("token_id")
                if asset_id == self.up_token_id:
                    self._update_token_price(event, side="UP")
                elif asset_id == self.down_token_id:
                    self._update_token_price(event, side="DOWN")

        except (json.JSONDecodeError, TypeError) as e:
            log_error(f"[Polymarket] Failed to parse WS message: {raw[:200]}", e)

    def _update_token_price(self, event: dict, side: str) -> None:
        """
        Extract best bid, best ask, and compute mid-price from an order
        book snapshot or delta event.
        """
        try:
            bids = event.get("bids", [])
            asks = event.get("asks", [])

            # Bids/asks are lists of {price, size} or [price, size] arrays
            def best_price(orders, highest: bool) -> Optional[float]:
                prices = []
                for o in orders:
                    if isinstance(o, dict):
                        p = float(o.get("price") or o.get("p") or 0)
                    elif isinstance(o, (list, tuple)) and len(o) >= 1:
                        p = float(o[0])
                    else:
                        continue
                    if p > 0:
                        prices.append(p)
                if not prices:
                    return None
                return max(prices) if highest else min(prices)

            best_bid = best_price(bids, highest=True)
            best_ask = best_price(asks, highest=False)

            mid = None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
            elif best_bid is not None:
                mid = best_bid
            elif best_ask is not None:
                mid = best_ask

            if side == "UP":
                if mid is not None:
                    self.up_price = mid
                if best_bid is not None:
                    self._best_bid = best_bid
                if best_ask is not None:
                    self._best_ask = best_ask
                # Update spread
                if self._best_bid is not None and self._best_ask is not None:
                    self.spread = round(self._best_ask - self._best_bid, 6)
            elif side == "DOWN":
                if mid is not None:
                    self.down_price = mid

        except (ValueError, TypeError) as e:
            log_error(f"[Polymarket] _update_token_price error for {side}", e)

    # ── Utility ──────────────────────────────────────────────────────────────

    def seconds_until_settlement(self) -> Optional[float]:
        """How many seconds until the current market window closes."""
        if self.market_end_ts is None:
            return None
        return self.market_end_ts - time.time()

    def refresh_if_expired(self) -> bool:
        """Returns True if the current market has expired (needs re-fetch)."""
        if self.market_end_ts is None:
            return True
        return time.time() >= self.market_end_ts
