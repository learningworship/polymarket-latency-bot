"""
execution/live_executor.py — Real trade execution via Polymarket CLOB (--mode live)

Uses py_clob_client (Polymarket's official Python SDK) to place MARKET orders.

Security:
  - API keys are loaded ONLY from environment variables
  - Keys are NEVER hardcoded or logged

Order flow:
  1. Place a MARKET buy order for the UP or DOWN token
  2. Poll order status every poll_interval_seconds for up to fill_timeout_seconds
  3. Once filled, monitor exit conditions (same as test mode)
  4. Place a MARKET sell order to close the position
  5. Log the completed trade to results.csv

IMPORTANT: This module requires a funded Polymarket account on Polygon mainnet
and valid POLYMARKET_PRIVATE_KEY + POLYMARKET_API_KEY environment variables.
"""

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional

from logger import log, log_trade, log_error
from strategy.latency_arb import Signal, Direction


# ── py_clob_client imports (guarded — only imported in live mode) ─────────────
def _import_clob_client():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            MarketOrderArgs,
            OrderType,
            Side,
        )
        return ClobClient, MarketOrderArgs, OrderType, Side
    except ImportError as e:
        raise ImportError(
            "py_clob_client is required for live mode. "
            "Install it with: pip install py_clob_client"
        ) from e


@dataclass
class LivePosition:
    """Represents one open real position."""
    market_id: str
    direction: Direction
    entry_price: float      # estimated fill price from order response
    entry_time: float
    size_usdc: float
    edge_at_entry: float
    order_id: str           # Polymarket order ID
    token_id: str           # token being held (for the exit order)


class LiveExecutor:
    """
    Places real MARKET orders on Polymarket CLOB.
    Position management logic mirrors TestExecutor exactly.
    """

    def __init__(self, config: dict, strategy):
        pos_cfg = config.get("position", {})
        exec_cfg = config.get("execution", {})

        self.hold_seconds: int = pos_cfg.get("hold_seconds", 240)
        self.take_profit_pct: float = pos_cfg.get("take_profit_pct", 0.15)
        self.max_trade_size_usdc: float = pos_cfg.get("max_trade_size_usdc", 20.0)
        self.max_concurrent: int = pos_cfg.get("max_concurrent", 1)
        self.poll_interval: int = exec_cfg.get("poll_interval_seconds", 10)
        self.fill_timeout: int = exec_cfg.get("fill_timeout_seconds", 30)
        self.chain_id: int = config.get("polymarket", {}).get("chain_id", 137)

        self.strategy = strategy
        self.open_position: Optional[LivePosition] = None
        self._client: Optional[object] = None

    @property
    def has_open_position(self) -> bool:
        return self.open_position is not None

    def _get_client(self):
        """
        Lazily initialise ClobClient using environment variables.
        Raises if credentials are missing.
        """
        if self._client is not None:
            return self._client

        ClobClient, _, _, _ = _import_clob_client()

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        api_key = os.environ.get("POLYMARKET_API_KEY")

        if not private_key:
            raise EnvironmentError(
                "POLYMARKET_PRIVATE_KEY environment variable is not set. "
                "Export it before running in live mode."
            )
        if not api_key:
            raise EnvironmentError(
                "POLYMARKET_API_KEY environment variable is not set. "
                "Export it before running in live mode."
            )

        self._client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=self.chain_id,
        )
        log.info("[LiveExecutor] ClobClient initialised (Polygon mainnet)")
        return self._client

    async def enter(self, signal: Signal, polymarket_feed) -> bool:
        """
        Place a real MARKET buy order on Polymarket CLOB.
        Returns True if order was accepted, False otherwise.
        """
        if self.has_open_position:
            log.debug("[LiveExecutor] Skipping — position already open")
            return False

        try:
            ClobClient, MarketOrderArgs, OrderType, Side = _import_clob_client()
            client = self._get_client()

            # Select token and determine fill price reference
            if signal.direction == Direction.UP:
                token_id = polymarket_feed.up_token_id
                ref_price = polymarket_feed.up_price
            else:
                token_id = polymarket_feed.down_token_id
                ref_price = polymarket_feed.down_price

            if token_id is None:
                log_error("[LiveExecutor] token_id is None — cannot place order")
                return False

            # Calculate number of shares: shares = size_usdc / price
            # e.g. $20 / 0.52 ≈ 38.46 shares
            if ref_price is None or ref_price <= 0:
                log_error("[LiveExecutor] ref_price invalid, cannot size order")
                return False

            shares = self.max_trade_size_usdc / ref_price
            shares = round(shares, 2)

            log.info(
                f"[LIVE] Placing MARKET BUY {signal.direction.value} | "
                f"token={token_id} size=${self.max_trade_size_usdc:.2f} "
                f"shares={shares} ref_price={ref_price:.4f}"
            )

            # Place the market order in a thread pool (SDK is synchronous)
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=self.max_trade_size_usdc,  # amount in USDC
            )
            # Run blocking SDK call in executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.create_and_post_order(order_args),
            )

            order_id = self._extract_order_id(response)
            if order_id is None:
                log_error(f"[LiveExecutor] Order placement failed: {response}")
                return False

            log.info(f"[LIVE] Order placed: {order_id}")

            # Poll for fill confirmation
            fill_price = await self._wait_for_fill(client, order_id, ref_price)

            pos = LivePosition(
                market_id=polymarket_feed.market_id or "unknown",
                direction=signal.direction,
                entry_price=fill_price,
                entry_time=time.time(),
                size_usdc=self.max_trade_size_usdc,
                edge_at_entry=signal.edge,
                order_id=order_id,
                token_id=token_id,
            )
            self.open_position = pos
            log.info(
                f"[LIVE] Position OPEN | {signal.direction.value} "
                f"fill={fill_price:.4f} market={pos.market_id}"
            )

            # Start position monitor
            asyncio.create_task(self._monitor_position(client, polymarket_feed))
            return True

        except Exception as e:
            log_error("[LiveExecutor] enter() failed", e)
            return False

    async def _wait_for_fill(self, client, order_id: str, ref_price: float) -> float:
        """
        Poll order status every poll_interval seconds until filled or timeout.
        Returns the actual fill price (falls back to ref_price on timeout).
        """
        deadline = time.time() + self.fill_timeout
        loop = asyncio.get_event_loop()

        while time.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                order = await loop.run_in_executor(
                    None,
                    lambda: client.get_order(order_id),
                )
                status = (order.get("status") or "").lower()
                if status in ("filled", "matched"):
                    avg_price = float(order.get("average_price") or ref_price)
                    log.info(f"[LIVE] Order {order_id} filled at {avg_price:.4f}")
                    return avg_price
                elif status in ("cancelled", "canceled", "failed"):
                    log.warning(f"[LIVE] Order {order_id} status={status}")
                    return ref_price
            except Exception as e:
                log_error(f"[LiveExecutor] _wait_for_fill poll error", e)

        log.warning(f"[LIVE] Fill timeout for {order_id} — using ref_price {ref_price:.4f}")
        return ref_price

    async def _monitor_position(self, client, polymarket_feed) -> None:
        """
        Same exit conditions as TestExecutor._monitor_position.
        On exit, places a MARKET sell order.
        """
        pos = self.open_position
        if pos is None:
            return

        try:
            while self.open_position is not None:
                await asyncio.sleep(1)
                pos = self.open_position
                if pos is None:
                    break

                if pos.direction == Direction.UP:
                    current_price = polymarket_feed.up_price
                else:
                    current_price = polymarket_feed.down_price

                if current_price is None:
                    continue

                hold_elapsed = time.time() - pos.entry_time
                pct_profit = (current_price - pos.entry_price) / pos.entry_price

                if pct_profit >= self.take_profit_pct:
                    await self._close_position(client, current_price, hold_elapsed, reason="take-profit")
                    return

                if hold_elapsed >= self.hold_seconds:
                    await self._close_position(client, current_price, hold_elapsed, reason="hold-timeout")
                    return

                secs_left = polymarket_feed.seconds_until_settlement()
                if secs_left is not None and secs_left <= 5:
                    await self._close_position(client, current_price, hold_elapsed, reason="settlement")
                    return

        except Exception as e:
            log_error("[LiveExecutor] _monitor_position error", e)
            self.open_position = None

    async def _close_position(self, client, exit_price: float, hold_seconds: float, reason: str) -> None:
        """Place a MARKET sell order, then log the trade."""
        pos = self.open_position
        if pos is None:
            return

        try:
            _, MarketOrderArgs, _, Side = _import_clob_client()

            shares = self.max_trade_size_usdc / pos.entry_price
            log.info(
                f"[LIVE] Placing MARKET SELL {pos.direction.value} | "
                f"reason={reason} token={pos.token_id} shares={shares:.2f}"
            )

            sell_args = MarketOrderArgs(
                token_id=pos.token_id,
                amount=self.max_trade_size_usdc,
                side=Side.SELL,
            )
            loop = asyncio.get_event_loop()
            try:
                sell_resp = await loop.run_in_executor(
                    None,
                    lambda: client.create_and_post_order(sell_args),
                )
                log.info(f"[LIVE] Sell order response: {sell_resp}")
            except Exception as e:
                log_error("[LiveExecutor] Sell order failed (position may need manual close)", e)

            pnl_usdc = ((exit_price - pos.entry_price) / pos.entry_price) * pos.size_usdc

            log.info(
                f"[LIVE] EXIT {pos.direction.value} | "
                f"reason={reason} entry={pos.entry_price:.4f} exit={exit_price:.4f} "
                f"hold={hold_seconds:.0f}s pnl={pnl_usdc:+.4f} USDC"
            )

            log_trade(
                market_id=pos.market_id,
                direction=pos.direction.value,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                hold_seconds=hold_seconds,
                pnl_usdc=pnl_usdc,
                edge_at_entry=pos.edge_at_entry,
                mode="live",
            )

            self.strategy.record_pnl(pnl_usdc)

        except Exception as e:
            log_error("[LiveExecutor] _close_position error", e)
        finally:
            self.open_position = None

    @staticmethod
    def _extract_order_id(response) -> Optional[str]:
        """Extract order ID from various py_clob_client response formats."""
        if response is None:
            return None
        if isinstance(response, dict):
            return response.get("orderID") or response.get("order_id") or response.get("id")
        if hasattr(response, "orderID"):
            return response.orderID
        if hasattr(response, "order_id"):
            return response.order_id
        return str(response) if response else None
