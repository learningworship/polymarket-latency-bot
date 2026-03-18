"""
execution/test_executor.py — Simulated trade execution (--mode test)

In test mode:
  - No real orders are placed
  - Fills are simulated at the current Polymarket mid-price
  - Hold time is tracked; position closes after hold_seconds OR take_profit_pct
  - Every closed position is written to results.csv via logger.log_trade()

The simulation runs inside the main asyncio event loop using asyncio.sleep()
so it never blocks the WebSocket feeds.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from logger import log, log_trade, log_error
from strategy.latency_arb import Signal, Direction


@dataclass
class SimPosition:
    """Represents one open simulated position."""
    market_id: str
    direction: Direction
    entry_price: float         # simulated fill price (mid-price at signal time)
    entry_time: float          # unix timestamp
    size_usdc: float
    edge_at_entry: float
    signal: Signal


class TestExecutor:
    """
    Simulates Polymarket order fills and position management.
    Shares identical entry/exit logic with live mode (only order placement differs).
    """

    def __init__(self, config: dict, strategy):
        pos_cfg = config.get("position", {})
        self.hold_seconds: int = pos_cfg.get("hold_seconds", 240)
        self.take_profit_pct: float = pos_cfg.get("take_profit_pct", 0.15)
        self.max_trade_size_usdc: float = pos_cfg.get("max_trade_size_usdc", 20.0)
        self.max_concurrent: int = pos_cfg.get("max_concurrent", 1)

        self.strategy = strategy  # LatencyArbStrategy instance (for record_pnl)
        self.open_position: Optional[SimPosition] = None

    @property
    def has_open_position(self) -> bool:
        return self.open_position is not None

    async def enter(self, signal: Signal, polymarket_feed) -> bool:
        """
        Simulate entry: fill at current mid-price.
        Returns True if position was opened, False if skipped.
        """
        if self.has_open_position:
            log.debug("[TestExecutor] Skipping signal — position already open")
            return False

        try:
            # Determine fill price based on direction
            if signal.direction == Direction.UP:
                fill_price = polymarket_feed.up_price
            else:
                fill_price = polymarket_feed.down_price

            if fill_price is None:
                log_error("[TestExecutor] Cannot fill — no mid-price available")
                return False

            pos = SimPosition(
                market_id=polymarket_feed.market_id or "unknown",
                direction=signal.direction,
                entry_price=fill_price,
                entry_time=time.time(),
                size_usdc=self.max_trade_size_usdc,
                edge_at_entry=signal.edge,
                signal=signal,
            )
            self.open_position = pos
            log.info(
                f"[TEST] ENTER {signal.direction.value} | "
                f"market={pos.market_id} fill={fill_price:.4f} "
                f"size=${pos.size_usdc:.2f} edge={signal.edge:+.3f}"
            )
            # Launch non-blocking position monitor
            asyncio.create_task(self._monitor_position(polymarket_feed))
            return True

        except Exception as e:
            log_error("[TestExecutor] enter() error", e)
            return False

    async def _monitor_position(self, polymarket_feed) -> None:
        """
        Polls every second to check exit conditions:
          1. Hold time exceeded
          2. Take-profit threshold crossed
          3. Market expired (settlement)
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

                # Get current exit price
                if pos.direction == Direction.UP:
                    current_price = polymarket_feed.up_price
                else:
                    current_price = polymarket_feed.down_price

                if current_price is None:
                    continue

                hold_elapsed = time.time() - pos.entry_time

                # P&L as a fraction of entry (e.g. bought at 0.50, now 0.58 → +16%)
                pct_profit = (current_price - pos.entry_price) / pos.entry_price

                # Exit condition 1: take-profit
                if pct_profit >= self.take_profit_pct:
                    await self._close_position(current_price, hold_elapsed, reason="take-profit")
                    return

                # Exit condition 2: hold time expired
                if hold_elapsed >= self.hold_seconds:
                    await self._close_position(current_price, hold_elapsed, reason="hold-timeout")
                    return

                # Exit condition 3: market about to settle (safety net)
                secs_left = polymarket_feed.seconds_until_settlement()
                if secs_left is not None and secs_left <= 5:
                    await self._close_position(current_price, hold_elapsed, reason="settlement")
                    return

        except Exception as e:
            log_error("[TestExecutor] _monitor_position error", e)
            self.open_position = None

    async def _close_position(self, exit_price: float, hold_seconds: float, reason: str) -> None:
        """Compute P&L, log trade, and clear the open position."""
        pos = self.open_position
        if pos is None:
            return

        try:
            # P&L in USDC: (exit - entry) / entry * size
            # e.g. bought $20 of token at 0.50; token goes to 0.58 → P&L = +$3.20
            pnl_usdc = ((exit_price - pos.entry_price) / pos.entry_price) * pos.size_usdc

            log.info(
                f"[TEST] EXIT {pos.direction.value} | "
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
                mode="test",
            )

            # Update daily P&L tracker in strategy
            self.strategy.record_pnl(pnl_usdc)

        except Exception as e:
            log_error("[TestExecutor] _close_position error", e)
        finally:
            self.open_position = None
