# APGE-06 — Strategy Robustness Research

## Purpose

APGE-06 asks a different question from APGE-03/04/05: not whether the engine is deterministic and fail-closed, but whether the current adaptive grid policy shows a sufficiently stable edge under public historical data and conservative execution assumptions.

A positive aggregate backtest is **not** treated as deployment approval. Consistency across non-overlapping folds and untouched holdout performance are required before a strategy can advance toward live-capital validation.

## Reproducible code gates

Latest research head used for train/holdout evidence:
`9ae45ab611c4ca1355d159da7fe83e09e82d13c5`

At that head:
- pytest checks: success
- train/holdout research workflow: success
- evidence artifact uploaded by GitHub Actions

The immediately preceding fill-model hardening checkout `541ef0ba057dcb65ab0a71e23064ed4c7198b71f` collected and passed **160/160** tests.

## 28-day baseline walk-forward

BTCUSDT USD-M futures, 5-minute Binance Vision archives, four non-overlapping 7-day folds:

- positive folds: **1 / 4**
- aggregate net PnL: **+3.60887426**
- total fills: **561**
- maximum absolute inventory: **0.003** against a **0.005** hard limit
- worst fold drawdown: **6.50770568**

The positive aggregate result is dominated by the final week. It is not statistically consistent enough to support a profitability claim.

## Execution-friction stress

Same four folds, with conservative fill confirmation requiring price to trade through a resting limit:

| Scenario | Positive folds | Aggregate PnL | Fills | Worst fold DD |
| --- | ---: | ---: | ---: | ---: |
| baseline | 1/4 | +3.60887426 | 561 | 6.50770568 |
| 1 bp fill confirmation | 1/4 | -3.48497202 | 489 | 9.26401788 |
| 2 bp fill confirmation | 1/4 | -9.40008244 | 400 | 10.10781634 |
| 1 bp + doubled fee | 1/4 | -11.07364404 | 489 | 9.60323576 |

This is a material finding: the apparent aggregate edge is fragile to realistic fill uncertainty and fee stress.

## Train / untouched holdout research

A pre-declared 12-candidate spacing search was run on six weekly training folds from **2026-07-23 through 2026-09-02**. Candidate selection used training data only and prioritized:

1. number of positive weekly folds,
2. median weekly PnL,
3. aggregate PnL,
4. smaller worst drawdown.

Selected training configuration:

- base spacing: **16 bps**
- volatility spacing multiplier: **2.0**
- positive training folds: **5 / 6**
- median weekly training PnL: **+0.98721201**
- aggregate training PnL: **+4.26467224**
- worst training drawdown: **3.34502756**

The two weekly holdout folds, **2026-09-03 through 2026-09-16**, were not used in selection.

Holdout result:

- positive holdout folds: **1 / 2**
- aggregate holdout PnL: **-3.21227672**
- first holdout week: **-4.55273234**
- second holdout week: **+1.34045562**

One-basis-point fill-confirmation stress on the same frozen holdout:

- positive folds: **1 / 2**
- aggregate PnL: **-1.73725250**

## Current conclusion

The execution/risk architecture remains structurally sound in the tested scope, but the current adaptive policy has **not demonstrated a robust positive expectancy**.

Therefore:

**APGE-06 STRATEGY ROBUSTNESS GATE: NOT PASSED**

This is not a software-test failure. It is a strategy-research result and must block any claim that APGE is ready for real-money deployment.

## Required next research step

Do not tune against the already-observed September holdout. The next strategy revision must be selected on a separate training period and evaluated on a genuinely unseen validation window. Candidate improvements should focus on reducing adverse inventory accumulation and avoiding low-quality fills rather than simply maximizing aggregate backtest PnL.

No real-money endpoint is authorized.
