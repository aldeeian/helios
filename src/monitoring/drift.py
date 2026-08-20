"""Distribution-drift statistics implemented from first principles.

PSI and KS are hand-rolled deliberately (spec Phase 6): the point is to be able
to defend the math in an interview, not to import a black box. Evidently AI is
supported as an optional cross-check in `evidently_check` but is never required.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# PSI interpretation thresholds (industry standard).
PSI_STABLE = 0.10
PSI_MODERATE = 0.25


@dataclass
class PSIResult:
    psi: float
    per_bin: list[dict]
    verdict: str  # "stable" | "moderate_shift" | "significant_shift"


def population_stability_index(expected: np.ndarray, actual: np.ndarray,
                               bins: int = 10, epsilon: float = 1e-6) -> PSIResult:
    """PSI of `actual` vs a reference `expected` distribution.

    Formula, summed over bins:  (a% - e%) * ln(a% / e%)

    Bin edges are the quantiles of `expected` (equal-frequency binning), so the
    reference is spread evenly and the statistic reflects the actual sample's
    departure from it. Empty bins are floored with epsilon to keep the log and
    ratio finite — the standard treatment.
    """
    expected = np.asarray(expected, float)
    actual = np.asarray(actual, float)

    # Quantile edges from the reference; unique() guards against ties collapsing
    # bins. Outer edges pushed to +/-inf so out-of-range actuals still land.
    quantiles = np.linspace(0, 1, bins + 1)
    edges = np.unique(np.quantile(expected, quantiles))
    edges[0], edges[-1] = -np.inf, np.inf

    e_counts, _ = np.histogram(expected, bins=edges)
    a_counts, _ = np.histogram(actual, bins=edges)
    e_pct = np.clip(e_counts / e_counts.sum(), epsilon, None)
    a_pct = np.clip(a_counts / a_counts.sum(), epsilon, None)

    contributions = (a_pct - e_pct) * np.log(a_pct / e_pct)
    psi = float(contributions.sum())
    per_bin = [
        {"bin": i, "expected_pct": round(float(e), 4),
         "actual_pct": round(float(a), 4), "contribution": round(float(c), 5)}
        for i, (e, a, c) in enumerate(zip(e_pct, a_pct, contributions))
    ]
    if psi < PSI_STABLE:
        verdict = "stable"
    elif psi < PSI_MODERATE:
        verdict = "moderate_shift"
    else:
        verdict = "significant_shift"
    return PSIResult(psi=psi, per_bin=per_bin, verdict=verdict)


@dataclass
class KSResult:
    statistic: float
    p_value: float
    drift: bool  # True if p < alpha


def ks_two_sample(a: np.ndarray, b: np.ndarray, alpha: float = 0.05) -> KSResult:
    """Two-sample Kolmogorov-Smirnov test, from scratch.

    Statistic D = max_x |F_a(x) - F_b(x)| over the pooled support, where F are
    empirical CDFs. The p-value uses the asymptotic Kolmogorov distribution
    with the standard small-sample effective-n correction:

        p = Q_ks( (sqrt(n_e) + 0.12 + 0.11/sqrt(n_e)) * D ),  n_e = n_a n_b/(n_a+n_b)

    matching scipy's `ks_2samp` asymptotic mode closely enough for a drift gate.
    """
    a = np.sort(np.asarray(a, float))
    b = np.sort(np.asarray(b, float))
    na, nb = len(a), len(b)

    pooled = np.concatenate([a, b])
    pooled.sort()
    cdf_a = np.searchsorted(a, pooled, side="right") / na
    cdf_b = np.searchsorted(b, pooled, side="right") / nb
    d = float(np.max(np.abs(cdf_a - cdf_b)))

    ne = na * nb / (na + nb)
    lam = (np.sqrt(ne) + 0.12 + 0.11 / np.sqrt(ne)) * d
    p = _kolmogorov_q(lam)
    return KSResult(statistic=d, p_value=float(p), drift=bool(p < alpha))


def _kolmogorov_q(lam: float) -> float:
    """Q_ks(lambda) = 2 * Σ_{j=1}^inf (-1)^{j-1} exp(-2 j^2 lambda^2).

    The survival function of the Kolmogorov distribution; series converges fast.
    """
    if lam <= 0:
        return 1.0
    total = 0.0
    for j in range(1, 101):
        term = 2 * (-1) ** (j - 1) * np.exp(-2 * j * j * lam * lam)
        total += term
        if abs(term) < 1e-10:
            break
    return float(min(max(total, 0.0), 1.0))


def evidently_check(expected: np.ndarray, actual: np.ndarray) -> dict | None:
    """Optional cross-check against Evidently AI, if installed. Returns None
    when the package is absent so the pipeline never hard-depends on it."""
    try:
        import pandas as pd
        from evidently import Report
        from evidently.metrics import ValueDrift
    except Exception:
        return None
    ref = pd.DataFrame({"value": expected})
    cur = pd.DataFrame({"value": actual})
    report = Report(metrics=[ValueDrift(column="value")])
    snapshot = report.run(reference_data=ref, current_data=cur)
    return snapshot.dict()
