# AI Strategy Lab (Bybit Testnet)

## Regime test draaien
```bash
python test_regime_system.py
```

## Strategy factory draaien
```bash
python research/auto_discovery/strategy_factory.py --num-strategies 1000
python research/auto_discovery/strategy_factory.py --target-strategies 1000 --workers 2
```

## Paper trader draaien
```bash
python paper_trader.py
```

## Output bestanden
- `results/strategy_factory_results.csv`
- `results/top_100_strategies.csv`
- `results/top_20_robust_strategies.csv`
- `results/strategy_factory_diagnostics.json`
- `results/paper_trading/trades.csv`
- `logs/paper_trader.log`

## Bybit testnet env setup
Maak een `.env` in de project root met:
```env
BYBIT_API_KEY=your_testnet_key
BYBIT_API_SECRET=your_testnet_secret
```
De paper trader gebruikt `testnet=True` en plaatst geen echte orders.
