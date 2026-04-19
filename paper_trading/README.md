# Paper Trading Runner

Deze map bevat een eenvoudige **CSV replay paper-trading runner** voor gevalideerde strategieën.

## Doel
- Strategy JSON laden
- Bars uit CSV replays gebruiken
- Signal-logica hergebruiken uit `research/validate_strategy.py`
- Eén positie tegelijk paper traden (geen exchange calls)
- Trades, PnL, equity en drawdown loggen

## Standaard input
- Strategie: `research/validated_strategies/momentum_breakout_v1.json`
- Data: `data/BTCUSDT_5s_bars.csv`

## Run
```bash
python paper_trading/paper_trade_strategy.py
```

Optionele args:
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

Trade log velden:
- `timestamp_entry`
- `timestamp_exit`
- `side`
- `entry_price`
- `exit_price`
- `gross_return`
- `net_return`
- `fee_paid`
- `reason_exit`
- `equity_after_trade`
