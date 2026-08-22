"""Orchestrate one monitoring cycle on a real target series: build the eight
signal arrays the four monitors need, run them, decide on retraining, and run
the champion-challenger gate. Kept separate from __main__ so it is testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..forecasting import backtest as bt
from ..forecasting.models import ForecastFn
from . import monitors, retrain


@dataclass
class CycleResult:
    target: str
    break_index: int
    signals: list[monitors.DriftSignal]
    retrain: retrain.RetrainDecision
    promotion: retrain.PromotionResult | None = None
    detail: dict = field(default_factory=dict)


def run_cycle(series: pd.Series, target: str, model_fn: ForecastFn,
              model_name: str, break_index: int,
              challenger_fn: ForecastFn | None = None,
              challenger_name: str | None = None) -> CycleResult:
    """One end-to-end drift-monitoring cycle around a suspected change point.

    Reference = history before `break_index`; current = from it onward. The
    incumbent champion is trained only on reference history — so if a real
    regime change happened at `break_index`, its post-break residuals and
    errors blow up, and the monitors that gate retraining fire.
    """
    y = series.dropna()
    ref_vals = y.iloc[:break_index].to_numpy(float)
    cur_vals = y.iloc[break_index:].to_numpy(float)

    # Reference predictions/residuals/errors: rolling-origin backtest confined
    # to pre-break history (honest in-distribution error).
    ref_bt = bt.backtest(y.iloc[:break_index], model_fn, model_name, target,
                         min_train=max(12, break_index // 2),
                         horizon=3, step=3)
    ref_pred = ref_bt.predictions
    ref_actual = ref_bt.actuals
    ref_resid = ref_actual - ref_pred

    # Current predictions/residuals/errors: champion trained on pre-break only,
    # forecasting the post-break window it never saw.
    champ_train = y.iloc[:break_index]
    horizon = len(cur_vals)
    cur_fc = model_fn(champ_train, horizon)
    cur_pred = cur_fc.mean
    cur_resid = cur_vals - cur_pred

    signals = monitors.run_all(
        reference=ref_vals, current=cur_vals,
        ref_predictions=ref_pred, cur_predictions=cur_pred,
        ref_residuals=ref_resid, cur_residuals=cur_resid,
        ref_errors=ref_resid, cur_errors=cur_resid,
    )
    decision = retrain.should_retrain(signals)

    promotion = None
    if decision.triggered:
        promotion = retrain.champion_challenger(
            series=y,
            champion_fn=model_fn,
            challenger_fn=challenger_fn or model_fn,
            champion_name=model_name,
            challenger_name=challenger_name or f"{model_name}-refit",
            train_end=break_index,
            holdout=min(6, len(cur_vals)),
        )
    return CycleResult(
        target=target, break_index=break_index, signals=signals,
        retrain=decision, promotion=promotion,
        detail={
            "ref_mae": round(float(np.mean(np.abs(ref_resid))), 4),
            "cur_mae": round(float(np.mean(np.abs(cur_resid))), 4),
            "n_reference": len(ref_vals), "n_current": len(cur_vals),
        },
    )
