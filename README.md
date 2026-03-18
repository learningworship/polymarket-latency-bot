# Polymarket Latency Arbitrage Bot

A Python bot that exploits the information delay between Binance real-time BTC prices and Polymarket's 5-minute BTC Up/Down prediction market odds.

## Strategy

When BTC moves sharply on Binance, Polymarket's order book lags by 2–10 seconds (human market makers and slower bots haven't repriced yet). This bot detects that lag and buys the correct side before the odds adjust.

**Signal logic:**
- Every 1 second, compare BTC price now vs 30 seconds ago
- If move > +0.4% → BUY UP token
- If move < -0.4% → BUY DOWN token
- Cross-check: Polymarket UP price must be between 0.35–0.65 (not already repriced)
- Edge filter: only enter if `|fair_probability - polymarket_price| > 0.10`

---

## Project Structure

```
polymarket_bot/
├── main.py                    # Entry point, CLI args, async orchestration
├── config.yaml                # All tunable parameters
├── feeds/
│   ├── binance.py             # Binance WebSocket (BTC/USDT real-time)
│   └── polymarket.py          # Polymarket CLOB WebSocket + REST
├── strategy/
│   └── latency_arb.py         # Signal detection with inline comments
├── execution/
│   ├── test_executor.py       # Simulated fills (test mode)
│   └── live_executor.py       # Real CLOB orders (live mode)
├── logger.py                  # results.csv + error.log writer
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/learningworship/polymarket-latency-bot.git
cd polymarket-latency-bot
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate      # Linux/macOS
# venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure parameters

Edit `config.yaml` to tune:
- `signal.price_change_threshold_pct` — minimum BTC move to trigger a signal (default: 0.4%)
- `position.max_trade_size_usdc` — max USDC per trade (default: $20)
- `position.hold_seconds` — max hold time before forced exit (default: 240s)
- `risk.daily_loss_limit_usdc` — stop trading if daily loss exceeds this (default: $100)

---

## Running in Test Mode (Recommended First Step)

Test mode connects to real live WebSocket feeds but **never places any orders**. Fills are simulated at the current mid-price.

```bash
python main.py --mode test
```

You will see:
- Real-time BTC prices from Binance
- Live Polymarket order book updates
- Signal detection logs
- Simulated trade entries and exits
- A P&L summary every 60 seconds

Every simulated trade is written to `results.csv`:

| timestamp | market_id | direction | entry_price | exit_price | hold_seconds | pnl_usdc | edge_at_entry | mode |
|---|---|---|---|---|---|---|---|---|
| 2026-03-18T10:00:01Z | 0xabc... | UP | 0.5120 | 0.5890 | 147.0 | +3.0078 | 0.142 | test |

**Run for at least 2–4 hours** and verify that `edge_at_entry` is consistently positive before attempting live mode.

---

## Running in Live Mode

> ⚠️ **Live mode places real orders using real USDC on Polygon mainnet. Use at your own risk.**

### Prerequisites

1. A funded Polymarket account on Polygon mainnet
2. Your Polymarket private key and API key

### Set environment variables

```bash
export POLYMARKET_PRIVATE_KEY="0x..."
export POLYMARKET_API_KEY="your_api_key_here"
```

Or create a `.env` file in the project root (never commit this file):

```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=your_api_key_here
```

### Run

```bash
python main.py --mode live
```

All real trades are logged to `results.csv` with `mode=live`.

---

## Output Files

| File | Description |
|---|---|
| `results.csv` | Every trade (test + live) with full P&L schema |
| `error.log` | All caught exceptions with timestamps |

---

## Risk Controls

| Guard | Behaviour |
|---|---|
| Daily loss limit | Halts all trading if cumulative day loss > $100 (configurable) |
| Settlement buffer | Never enters in last 60 seconds of a 5-min market window |
| Spread filter | Skips signal if Polymarket order book spread > 0.05 |
| Max concurrent | Only 1 open position at a time |
| Edge filter | Minimum 0.10 edge required to enter |
| Exception safety | All exceptions caught, logged to error.log, bot continues |

---

## VPS Deployment (Recommended)

For lowest latency, deploy on a VPS in **Dublin or London** (geographically closest to Polymarket's infrastructure).

```bash
# Install dependencies
sudo apt update && sudo apt install -y python3-pip python3-venv git

# Clone, setup, and run as a background process
git clone https://github.com/learningworship/polymarket-latency-bot.git
cd polymarket-latency-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Set credentials
export POLYMARKET_PRIVATE_KEY="0x..."
export POLYMARKET_API_KEY="..."

# Run in background with nohup
nohup python main.py --mode live > bot.log 2>&1 &
echo $! > bot.pid
```

To stop:
```bash
kill $(cat bot.pid)
```

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `websockets` | Async WebSocket connections (Binance + Polymarket) |
| `aiohttp` | Async HTTP for Polymarket REST API on startup |
| `py_clob_client` | Polymarket's official Python SDK for order placement |
| `pyyaml` | Config file parsing |
| `python-dotenv` | Load credentials from `.env` file |
| `pandas` | Optional: for post-hoc analysis of results.csv |

---

## Disclaimer

This software is for educational purposes. Prediction market trading involves financial risk. The latency arbitrage edge described here may have been reduced by Polymarket's dynamic fee updates. Always run in test mode first and never risk more than you can afford to lose.
