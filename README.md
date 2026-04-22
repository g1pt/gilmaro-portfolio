# High-Frequency-Trading-Bot

This repository is currently oriented around **paper-first FX runtime evaluation** using `paper_trader.py`.

It also contains legacy crypto/backtest assets, but the practical starting point today is: run the main paper runtime, observe decision quality, and tune safely before any broker/live transition.

## Canonical docs (read in this order)

1. `docs/README.md` — documentation index and deep links.
2. `README.md` — this operational entrypoint.
3. `README_strategy_lab.md` — strategy research/discovery workflows.
4. `paper_trading/README.md` — replay-style paper utility details.
5. `docs/EVO_V24_ROLLOUT_NOTES.md` — historical rollout context/archive.

## Main runtime paths

### 1) Primary: `paper_trader.py` (recommended)

```bash
python paper_trader.py
```

Use this for runtime-like paper operation: symbol scanning, execution gating/filtering, position management, and continuous decision cycles.

Treat symbol universe, thresholds, and mode behavior as **configuration-driven** (`.env` / runtime config), not as fixed constants in this README.

### 2) Secondary: `paper_trading/paper_trade_strategy.py`

```bash
python paper_trading/paper_trade_strategy.py
```

Use this for deterministic CSV replay of a validated strategy JSON. It is useful for quick checks and controlled comparisons, but it is intentionally simpler than the main runtime orchestration.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python paper_trader.py
```

## Paper-first operating model

Recommended progression for this repo:

1. **Run paper mode first** and keep live execution disabled.
2. **Review runtime logs and outcomes** (entries, blocks, closes, reason patterns).
3. **Tune based on measured behavior** (blocked reasons, trade quality, drawdown/risk profile), not on assumptions.
4. **Only then** consider a constrained broker/live pilot.

## Logs, outputs, and evaluation mindset

- Main runtime logs are written to `logs/paper_trades.log` (and streamed to stdout).
- Runtime state/metadata files can also be persisted under `logs/` during operation.
- The secondary replay utility writes a trade log CSV to `paper_trading/paper_portfolio_log.csv` and prints a compact summary (`total trades`, `win rate`, `total return`, `max drawdown`, `final equity`).

Use these outputs to evaluate process quality over multiple sessions (consistency, risk behavior, and failure modes), not single-run headline numbers.

## Optional broker / MT5 evolution path

Broker integration (including MT5 adapter paths) exists in the repo, but is best treated as a **later-stage evolution** after stable paper behavior is demonstrated.

Keep early transitions narrow: limited symbols, conservative sizing, and explicit monitoring.
