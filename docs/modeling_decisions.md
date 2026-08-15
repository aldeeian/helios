# Modeling Decisions — Forecasting (Phase 4)

All numbers below are from the real backtest report
(`data/reports/backtest_report.json`, seed-42 dataset, rolling-origin CV:
min_train=18, horizon=3, step=3 → 6 folds per 36-month series, metrics pooled
across folds). Reproduce with `python -m src.forecasting`.

## Evaluation design

- **Rolling-origin expanding-window CV, never random splits.** Every fold
  trains strictly before its forecast origin. Random splits leak future
  information into the past — the classic time-series mistake this project
  exists to avoid.
- **Baselines gate promotion.** A candidate becomes champion only if its
  pooled MAPE beats both `naive` (last value) and `seasonal_naive` (same month
  last year). Otherwise the baseline stays champion, and that is reported, not
  hidden.
- **Intervals are validated, not assumed** — empirical coverage of the nominal
  80% interval is measured on every backtest.

## Headline results (11 targets: headcount + spend per department and totals)

**Candidates beat baselines on 9 of 11 targets.** The two exceptions are real
findings, kept as-is:

- `spend::Finance` — champion: **seasonal_naive** (2.28% MAPE, 94% coverage).
  Finance spend is dominated by stable seasonal patterns; nothing beat simply
  repeating last year.
- `spend::Human Resources` — champion: **naive** (3.39% MAPE). The smallest,
  noisiest department; models overfit what is mostly a random walk.

Champions elsewhere: SARIMA took 5 targets (e.g. `headcount::TOTAL` 2.09%
MAPE vs 4.10% naive), XGBoost took 4 (e.g. `headcount::Operations` 4.23% vs
5.15% naive; `spend::TOTAL` 6.45% vs 6.55% — a narrow win).

## Honest failures (worth more in an interview than the wins)

- **Prophet lost on every single target** — MAPE 9.8%–93%. Two structural
  reasons: (a) 18–33 monthly points is far below what Prophet's Bayesian
  seasonal decomposition needs; (b) its changepoint priors smooth right
  through the month-24 reorganization, so post-break folds are badly wrong.
  Prophet stays in the codebase as a documented negative result.
- **SARIMA exploded on `spend::TOTAL`** (63% MAPE vs 6.6% naive): summed
  spend across departments mixes two different post-break regimes
  (Operations shrinking, Technology growing + contractor rate uplift), and
  the seasonal-differencing fit diverged on some folds. Component-level
  SARIMA forecasts are fine — a concrete argument for forecasting components
  and aggregating, rather than forecasting aggregates.
- **Interval coverage is mostly below nominal** (typical 44–67% vs the
  nominal 80%): folds that straddle the month-24 structural break produce
  errors far outside intervals estimated from pre-break history. This is
  exactly what Phase 6 drift detection is for — when the regime shifts,
  historical uncertainty estimates stop being valid. The baselines' wider,
  dumber intervals (78–94% coverage) were more honest than the models'.

## Model-specific choices

- **SARIMA order heuristic, not grid search**: (1,1,1)x(0,1,1,12) with ≥30
  points, seasonal AR fallback at 16–29, plain ARIMA below. A 36-point series
  cannot support a credible auto-search; a documented deterministic heuristic
  is defensible and reproducible.
- **XGBoost features**: lags 1–3, lag 12 when history allows, cyclic month
  encoding (sin/cos), linear time index. Recursive multi-step prediction.
  Intervals from in-sample residuals, floored at half the naive residual
  scale because in-sample residuals understate true error.
- **Champion artifact = refit-on-predict pyfunc** registered in MLflow (11
  registered models, one per target). The registered object stores *which
  family won*; it refits on the history supplied at predict time. On
  36-point series, refitting costs milliseconds and avoids serving stale
  frozen coefficients.

## What the ground truth says (evaluation-only files)

Because the data generator's parameters are known, the errors above can be
put in context: the true generating process has ~1.5–2% month-to-month noise
on headcount, so SARIMA's 2.09% MAPE on `headcount::TOTAL` is close to the
irreducible floor pre-break. Post-break errors dominate the pooled metrics —
which is the correct behavior for an honest backtest, not a flaw.
