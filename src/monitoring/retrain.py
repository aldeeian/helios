"""Threshold-triggered retraining with a champion-challenger promotion gate.

Two design decisions the spec calls out as production-critical:

1. **Threshold-triggered, not scheduled.** Retraining on a fixed cadence either
   wastes compute (retraining when nothing changed) or reacts too late (drift
   between scheduled runs). Helios retrains when a monitor fires — and only then.

2. **Champion-challenger gate.** Automatically retraining and deploying is how
   you automate your way into a *worse* model. The freshly retrained challenger
   must beat the incumbent champion on a held-out window before it is promoted;
   if it does not, the champion stays and an alert fires.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..forecasting import metrics
from ..forecasting.models import ForecastFn
from .monitors import DriftSignal

# Monitors that, when triggered, justify retraining. Data drift alone does not:
# inputs can move without the model getting worse. Prediction/concept drift and
# performance decay all bear directly on output quality.
RETRAIN_ON = {"prediction_drift", "concept_drift", "performance_decay"}


@dataclass
class RetrainDecision:
    triggered: bool
    reasons: list[str]
    signals: list[dict] = field(default_factory=list)


def should_retrain(signals: list[DriftSignal]) -> RetrainDecision:
    reasons = [s.monitor for s in signals if s.drift and s.monitor in RETRAIN_ON]
    return RetrainDecision(
        triggered=bool(reasons),
        reasons=reasons,
        signals=[{"monitor": s.monitor, "drift": s.drift, **s.detail} for s in signals],
    )


@dataclass
class PromotionResult:
    promoted: bool
    champion_model: str
    challenger_model: str
    champion_mae: float
    challenger_mae: float
    improvement: float          # fraction; positive = challenger better
    margin_required: float
    holdout_start: str
    note: str


def champion_challenger(
    series: pd.Series,
    champion_fn: ForecastFn,
    challenger_fn: ForecastFn,
    champion_name: str,
    challenger_name: str,
    train_end: int,
    holdout: int = 6,
    margin: float = 0.02,
) -> PromotionResult:
    """Evaluate a retrained challenger against the incumbent on a fresh holdout.

    Both models forecast the same held-out window (the most recent `holdout`
    months). The champion represents the incumbent trained on data up to
    `train_end`; the challenger is trained on everything before the holdout
    (i.e. it has seen the drifted period). The challenger is promoted only if it
    beats the champion's MAE by more than `margin` — a required improvement, not
    a tie-break, so noise cannot promote a non-better model.
    """
    y = series.dropna()
    n = len(y)
    holdout = min(holdout, n - train_end)
    split = n - holdout
    test = y.iloc[split:]

    champ_train = y.iloc[:train_end]                 # incumbent: pre-drift history
    chall_train = y.iloc[:split]                     # challenger: includes drift

    champ_fc = champion_fn(champ_train, len(test))
    chall_fc = challenger_fn(chall_train, len(test))
    champ_mae = metrics.mae(test.to_numpy(float), champ_fc.mean)
    chall_mae = metrics.mae(test.to_numpy(float), chall_fc.mean)

    improvement = (champ_mae - chall_mae) / champ_mae if champ_mae > 0 else 0.0
    promoted = improvement > margin
    note = (
        f"challenger {'promoted' if promoted else 'rejected'}: "
        f"{improvement:+.1%} vs champion (needed > {margin:.0%})"
    )
    return PromotionResult(
        promoted=promoted,
        champion_model=champion_name,
        challenger_model=challenger_name,
        champion_mae=round(champ_mae, 4),
        challenger_mae=round(chall_mae, 4),
        improvement=round(improvement, 4),
        margin_required=margin,
        holdout_start=str(test.index[0].date()),
        note=note,
    )
