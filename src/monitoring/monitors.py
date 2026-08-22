"""The four failure modes Helios watches, kept as distinct concepts (spec Phase 6).

    data drift        input feature distribution moved vs the training baseline
    prediction drift  the model's output distribution moved (often the earliest
                      warning — it shifts before ground truth arrives)
    concept drift     the input->output relationship changed: residuals that
                      were well-behaved on the baseline are now biased/spread
    performance decay measured forecast error rose vs the baseline error

Conflating these hides *why* a model went wrong. Data drift with stable
performance may be benign; prediction drift with stable inputs is suspicious;
performance decay is the one that always matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import drift

# Distributional tests (PSI/KS) need enough observations to be trustworthy;
# below this, they abstain rather than claim drift they cannot support. This is
# why, on 36-point monthly series, the residual-based monitors (concept drift,
# performance decay) are the primary signal and the distributional ones are a
# secondary cross-check that mostly abstains — the correct behaviour, not a gap.
MIN_DISTRIBUTIONAL_SAMPLES = 24


@dataclass
class DriftSignal:
    monitor: str
    drift: bool
    detail: dict = field(default_factory=dict)


def _abstain(monitor: str, n_ref: int, n_cur: int) -> DriftSignal:
    return DriftSignal(monitor, False, {
        "note": "insufficient_samples", "n_reference": n_ref, "n_current": n_cur,
        "min_required": MIN_DISTRIBUTIONAL_SAMPLES,
    })


def data_drift(reference: np.ndarray, current: np.ndarray,
               min_samples: int = MIN_DISTRIBUTIONAL_SAMPLES) -> DriftSignal:
    """Input distribution drift via PSI (primary) with a KS cross-check.

    Flags drift if PSI signals a significant shift OR KS rejects equality —
    two lenses on the same question, either firing is enough to warrant a look.
    Abstains below `min_samples`: a distributional claim from a handful of
    points would be noise, not signal.
    """
    if len(reference) < min_samples or len(current) < min_samples:
        return _abstain("data_drift", len(reference), len(current))
    psi = drift.population_stability_index(reference, current)
    ks = drift.ks_two_sample(reference, current)
    flagged = psi.verdict == "significant_shift" or ks.drift
    return DriftSignal("data_drift", flagged, {
        "psi": round(psi.psi, 4), "psi_verdict": psi.verdict,
        "ks_stat": round(ks.statistic, 4), "ks_p": round(ks.p_value, 6),
    })


def prediction_drift(ref_predictions: np.ndarray, cur_predictions: np.ndarray,
                     min_samples: int = MIN_DISTRIBUTIONAL_SAMPLES) -> DriftSignal:
    """Output distribution drift — same statistics applied to predictions."""
    if len(ref_predictions) < min_samples or len(cur_predictions) < min_samples:
        return _abstain("prediction_drift", len(ref_predictions), len(cur_predictions))
    psi = drift.population_stability_index(ref_predictions, cur_predictions)
    ks = drift.ks_two_sample(ref_predictions, cur_predictions)
    flagged = psi.verdict == "significant_shift" or ks.drift
    return DriftSignal("prediction_drift", flagged, {
        "psi": round(psi.psi, 4), "psi_verdict": psi.verdict,
        "ks_stat": round(ks.statistic, 4), "ks_p": round(ks.p_value, 6),
    })


def concept_drift(ref_residuals: np.ndarray, cur_residuals: np.ndarray) -> DriftSignal:
    """input->output relationship change, seen through residual behaviour.

    If the learned relationship still holds, residuals on new data look like
    training residuals (same centre and spread). A KS shift in the residual
    distribution, or a materially larger bias, signals the mapping changed —
    the definition of concept drift.
    """
    ref_residuals = np.asarray(ref_residuals, float)
    cur_residuals = np.asarray(cur_residuals, float)
    ks = drift.ks_two_sample(ref_residuals, cur_residuals)
    ref_bias = float(np.mean(ref_residuals))
    cur_bias = float(np.mean(cur_residuals))
    ref_scale = float(np.std(ref_residuals)) or 1e-9
    # Bias shift measured in reference-residual std units; > 1 sigma is notable.
    bias_shift = abs(cur_bias - ref_bias) / ref_scale
    flagged = ks.drift or bias_shift > 1.0
    return DriftSignal("concept_drift", flagged, {
        "ks_stat": round(ks.statistic, 4), "ks_p": round(ks.p_value, 6),
        "ref_bias": round(ref_bias, 3), "cur_bias": round(cur_bias, 3),
        "bias_shift_sigmas": round(bias_shift, 2),
    })


def performance_decay(ref_errors: np.ndarray, cur_errors: np.ndarray,
                      ratio_threshold: float = 1.5) -> DriftSignal:
    """Forecast error growth vs baseline.

    Flags decay when recent mean absolute error exceeds the baseline mean by
    more than `ratio_threshold` (default: 50% worse). A ratio, not an absolute
    bar, so the same monitor works across targets of wildly different scale.
    """
    ref_mae = float(np.mean(np.abs(ref_errors)))
    cur_mae = float(np.mean(np.abs(cur_errors)))
    ratio = cur_mae / ref_mae if ref_mae > 0 else float("inf")
    return DriftSignal("performance_decay", bool(ratio > ratio_threshold), {
        "ref_mae": round(ref_mae, 4), "cur_mae": round(cur_mae, 4),
        "ratio": round(ratio, 3), "threshold": ratio_threshold,
    })


def run_all(reference: np.ndarray, current: np.ndarray,
            ref_predictions: np.ndarray, cur_predictions: np.ndarray,
            ref_residuals: np.ndarray, cur_residuals: np.ndarray,
            ref_errors: np.ndarray, cur_errors: np.ndarray) -> list[DriftSignal]:
    """Run all four monitors and return their signals."""
    return [
        data_drift(reference, current),
        prediction_drift(ref_predictions, cur_predictions),
        concept_drift(ref_residuals, cur_residuals),
        performance_decay(ref_errors, cur_errors),
    ]
