# High-Frequency-Trading-Bot

Live **paper-signal VWAP bot** op Binance trades (`btcusdt@aggTrade`).

> Dit project doet **geen echte orders**. Het print alleen BUY/SELL signalen (paper mode).

## Wat is nu geïmplementeerd (zoals in je roadmap screenshots)
- `market_data.py`: live market-data stream via Binance WebSocket.
- `vwap.py`: rolling VWAP engine.
- `main.py`: strategy engine (mean reversion rond VWAP) met signal logging.

## Installatie
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run
```bash
python main.py --config config.yaml
```

Je ziet dan live signalen in de terminal zoals:
- `SIGNAL=BUY` als prijs onder VWAP zakt.
- `SIGNAL=SELL` als prijs boven VWAP komt.

## Architectuur richting “elite”
Deze repo bevat nu de basisblokken uit je screenshots:
1. market data engine ✅
2. VWAP engine ✅
3. strategy engine ✅

Nog toe te voegen voor volgende fases:
- execution simulator met slippage/fees
- risk limits (max position, daily loss)
- backtest + metrics (Sharpe, drawdown)
- multi-exchange/arbitrage module

## Belangrijke realiteitscheck
Claims zoals `$100-$1000/maand` of `elite in 3 maanden` zijn **niet gegarandeerd**.
Resultaten hangen af van edge, kosten, latency, regime en risicobeheer.

## EVO v24 decision flow (paper/live)

De runtime in `paper_trader.py` gebruikt nu een centrale EVO v24-gatinglaag:

1. **Composite edge score** met breakdown:
   - raw signal
   - adjusted signal
   - signal strength
   - regime alignment
   - volatility suitability
   - execution quality
   - fill quality
   - symbol quality
2. **Execution-aware hard blocks** (o.a. execution rate, spread proxy, quality hard-blocks).
3. **Profit / expectancy gate** met aparte pass/fail-output en reason codes.
4. **Adaptive cooldown + trade spacing** met bounds vanuit env.
5. **No-trade outcome codes** (bijv. `PROFIT_GATE_BLOCKED`, `EXECUTION_BLOCKED`, `REGIME_BLOCKED`, `VOLATILITY_BLOCKED`, `RISK_BLOCKED`).

Belangrijk: alle EVO v24 thresholds/weights worden uit env geladen via `execution/evo_v24.py` (inclusief legacy aliases zoals `PE_MIN_SIGNAL` en `MIN_TIME_BETWEEN_TRADES`), zodat tuning reproducible blijft tussen paper runs.

### PAPER draaien met EVO v24 defaults

```bash
cp .env.example .env
python paper_trader.py
```

### Decision logs lezen

Zoek op deze structured log regels:

- `DECISION LAYERS | ...` → volledige stage-output met breakdown.
- `DECISION LAYERS` bevat nu vaste velden: `outcome`, `reason_code`, `quality_v24`, `execution_v24`, `profit_gate`.
- `EXECUTION FUNNEL | ...` → funnel counters/uitkomst.
- `SYMBOL RANK V24 | ...` → symbol ranking met signal/execution/regime/volatility/symbol-quality breakdown.

### Snelle tuning-groepen

- **Composite edge**: `*_WEIGHT`, `SIGNAL_SCORE_MIN`, `MIN_COMPOSITE_QUALITY_SCORE`
- **Execution-aware filters**: `MIN_EXECUTION_RATE`, `MIN_FILL_QUALITY_SCORE`, `MAX_SPREAD_PROXY_SCORE`, `MAX_SLIPPAGE_BPS`
- **Profit gate**: `MIN_EXPECTANCY_PROXY_SCORE`, `PROFIT_GATE_STRICT_MODE`, `PROFIT_OVERRIDE_*`
- **Adaptive gedrag**: `BASE_COOLDOWN_SECONDS`, `MIN_TIME_BETWEEN_TRADES_SECONDS`, `LOSS_STREAK_*`, `NO_TRADE_LOOPS_*`
- **Symbol selectiviteit**: `SYMBOL_QUALITY_FILTER_ENABLED`, `MIN_SYMBOL_QUALITY_SCORE`, `SYMBOL_PRIORITY_*`

### Aanbevolen rollout (paper -> live)

1. **Paper met defaults**: start met `.env.example` zonder live toggles.
2. **Logs reviewen**: monitor `DECISION LAYERS`, `EXECUTION FUNNEL`, `SYMBOL RANK V24`, plus `reason_code` en `outcome`.
3. **Thresholds tunen**: pas EVO v24 thresholds/weights aan op basis van blocked reasons.
4. **Dan pas live activeren**: eerst minimale risk en conservatieve caps.
5. **Gefaseerd uitbreiden**: begin met beperkt symbol universe en lage size.

### Legacy score vs EVO v24 normalisatie

- Legacy runtime gebruikt intern signal scores die > 1.0 kunnen zijn.
- EVO v24 gating normaliseert deze scores naar `0..1` aan de grens van de v24 assessments.
- Zo blijft bestaand runtime-gedrag behouden, terwijl composite/profit gating consistent en reproduceerbaar tuneable is.
