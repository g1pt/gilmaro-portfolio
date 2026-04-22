# Strategy Lab (Research / Discovery)

This document covers **research workflows**. It is not the primary runtime operations guide.

For day-to-day paper runtime execution, start from `README.md` and run `paper_trader.py`.

## Positioning

- Primary runtime today: `paper_trader.py` (FX-first paper evaluation).
- Strategy lab role: generate, validate, and inspect candidate strategies/filters.
- Legacy Bybit/testnet references may still exist in code, but they are not the default research entrypoint here.

## Common research tasks

### Regime/system checks
```bash
python test_regime_system.py
```

### Strategy factory / auto-discovery
```bash
python research/auto_discovery/strategy_factory.py --num-strategies 1000
# or
python research/auto_discovery/strategy_factory.py --target-strategies 1000 --workers 2
```

### Runtime validation pass with main paper trader
```bash
python paper_trader.py
```

## Typical outputs

- `results/strategy_factory_results.csv`
- `results/top_100_strategies.csv`
- `results/top_20_robust_strategies.csv`
- `results/strategy_factory_diagnostics.json`
- `results/paper_trading/trades.csv`
- `logs/paper_trader.log`

## Relationship to other docs

- `README.md`: operational truth and run modes.
- `paper_trading/README.md`: secondary CSV replay utility (`paper_trading/paper_trade_strategy.py`).
- `docs/README.md`: canonical documentation routing.
