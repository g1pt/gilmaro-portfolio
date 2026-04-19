# EVO v24 rollout notes

## Wat is gefixt
- Composite weighting semantiek gecorrigeerd: `adjusted_signal_score` heeft nu eigen `ADJUSTED_SIGNAL_WEIGHT`, en `trend_bias` wordt apart gewogen via `TREND_BIAS_WEIGHT`.
- Execution quality fallback opgeschoond: eigen key `MISSING_EXECUTION_QUALITY_FALLBACK` toegevoegd en gebruikt.
- V24 normalisatiegrens expliciet gemaakt in runtime: legacy scores worden bij v24-gating naar `0..1` genormaliseerd.
- Hard-block routing verduidelijkt in decision flow: regime/volatility/execution hard-blocks mappen nu consistenter naar stage/outcome/reason_code.

## Waarom
- Om score-interpretatie voorspelbaar te maken tijdens tuning.
- Om paper-validatie en log-analyse betrouwbaarder te maken.
- Om regressierisico te verkleinen richting gecontroleerde live activatie.

## Nieuwe env keys
- `ADJUSTED_SIGNAL_WEIGHT`
- `MISSING_EXECUTION_QUALITY_FALLBACK`

## Bewust niet veranderd
- Legacy interne scorelogica buiten de v24 assessment-grens is niet breed herbouwd.
- Bestaande risk/spacing flow is behouden, met alleen gerichte v24-integratieverbeteringen.

## Paper-validatie voor live
1. Draai meerdere sessies in PAPER met defaults.
2. Analyseer blocked outcomes op `reason_code`/`outcome` in `DECISION LAYERS`.
3. Tune thresholds in kleine stappen en vergelijk runs.
4. Activeer live pas met kleine size + beperkte symbol set.
