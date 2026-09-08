# Demo script (90 seconds)

A no-narration, captioned walkthrough — the sequence that makes Helios
memorable is the self-healing loop end to end. Every command below runs against
the real pipeline.

## Setup (once, off-camera)

```
python -m src.datagen --seed 42
python -m src.reconciliation
python -m src.warehouse
python -m src.forecasting --no-prophet
python -m src.variance
python -m src.monitoring
python -m powerbi.build_dashboard
```

## Storyboard

| Time | On screen | Caption |
|---|---|---|
| 0:00 | `powerbi/dashboard.html` — Executive page | "Helios forecasts headcount & spend from four messy source systems." |
| 0:10 | Reconciliation scorecard (`data/reconciled/scorecard.json`) | "Every excluded record is quarantined with a reason — nothing silently dropped." |
| 0:20 | Dashboard → Workforce page, point at the 2025-01 reorg marker | "A simulated reorg shifts work from Operations to Technology + contractors." |
| 0:30 | `python -m src.monitoring` output | "A forecast trained before the reorg degrades: Technology headcount MAE 2.1 → 35." |
| 0:40 | same output — DRIFT lines | "Concept-drift and performance-decay monitors fire automatically." |
| 0:48 | same output — RETRAIN + promotion | "Retraining triggers; the challenger beats the champion by 95% on a fresh holdout → promoted." |
| 0:58 | Dashboard → Model health page | "Forecast accuracy, drift status, and the data-quality scorecard, all in one view." |
| 1:05 | Dashboard → Variance page (decomposition) | "The same reorg shows as +80% Technology contractor variance — split into volume, mix, rate." |
| 1:15 | Terminal: `python -m src.agent "Why is Technology contractor spend over budget in December 2025?"` … show SQL + answer | "Ask in plain English — the agent returns a grounded answer and the exact SQL it ran." |
| 1:25 | Terminal: `python -m src.agent "'; DROP TABLE fact_spend; --"` → blocked | "Every generated query is validated read-only. Injections never execute." |
| 1:30 | End card | "Synthetic data. 136 tests. Full pipeline in CI. github.com/aldeeian/helios" |

## The one-liner for the top of the README / a recruiter

> "A forecast trained before a Q1 reorganization degraded 17×; Helios detected
> the drift within the same cycle, retrained automatically, and promoted a
> challenger that beat the incumbent by 95% — then explained the matching +80%
> contractor-cost variance in plain language."

## Recording notes

- Use a terminal with a large font; the drift-demo output is the hero shot.
- The dashboard is theme-aware — record in dark mode for contrast.
- No voiceover needed; the captions above carry it. ~90 seconds total.
