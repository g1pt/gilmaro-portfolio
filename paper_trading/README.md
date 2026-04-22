# Paper Trading CSV Replay Utility (Secondary)

This folder contains a **simple replay runner** for validated strategy JSON + OHLCV CSV data.

> This is **not** the main paper-trader runtime. The primary operations path is `python paper_trader.py` from the repo root.

## What this utility does

- Loads strategy parameters from a validated strategy JSON.
- Replays bars from a CSV file.
- Reuses signal/risk logic from `research/validate_strategy.py`.
- Runs a one-position-at-a-time paper loop (no broker/exchange execution path).
- Writes trade log + summary metrics.

## Default inputs

- Strategy: `research/validated_strategies/momentum_breakout_v1.json`
- Data: `data/BTCUSDT_5s_bars.csv`

These defaults are convenience examples; for FX-focused work, pass your own strategy/data files via CLI args.

## Run

```bash
python paper_trading/paper_trade_strategy.py
```

Optional args:

```bash
python paper_trading/paper_trade_strategy.py \
  --strategy-path research/validated_strategies/momentum_breakout_v1.json \
  --data data/BTCUSDT_5s_bars.csv \
  --fee-rate 0.0008 \
  --initial-equity 10000
```

## Output

- Terminal summary:
  - total trades
  - win rate
  - total return
  - max drawdown
  - final equity
- Trade log CSV:
  - `paper_trading/paper_portfolio_log.csv`
