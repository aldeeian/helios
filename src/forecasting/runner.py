"""Backtest all model families on all targets, track in MLflow, pick champions.

Champion rule (honest by construction): a candidate model is only promoted if
its pooled backtest MAPE beats BOTH naive baselines on that target; otherwise
the best baseline IS the champion and the report says so. Shipping "seasonal
naive won" is a feature of this project, not an embarrassment.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import sqlalchemy as sa

from .. import config
from . import backtest as bt
from . import datasets
from .models import BASELINES, CANDIDATES, ForecastFn

REPORTS_DIR = config.DATA_DIR / "reports"


def _mlflow_setup():
    import mlflow

    mlflow_dir = config.DATA_DIR / "mlflow"
    mlflow_dir.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{(mlflow_dir / 'mlflow.db').as_posix()}")
    if mlflow.get_experiment_by_name("helios-forecasting") is None:
        mlflow.create_experiment(
            "helios-forecasting",
            artifact_location=(mlflow_dir / "artifacts").as_uri(),
        )
    mlflow.set_experiment("helios-forecasting")
    return mlflow


def _registry_name(target: str) -> str:
    return "helios-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", target)


def _make_refit_forecaster(model_name: str):
    """MLflow pyfunc wrapper: stores WHICH family won; refits on the history
    it is given at predict time. For 36-point monthly series, refit-on-predict
    is cheaper and safer than shipping frozen coefficients that go stale.

    Built by a factory so mlflow (a heavy import) stays optional for the
    --no-mlflow path; the class must subclass mlflow.pyfunc.PythonModel or
    log_model rejects it.
    """
    import mlflow.pyfunc

    class RefitForecaster(mlflow.pyfunc.PythonModel):
        def __init__(self, name: str):
            self.model_name = name

        def predict(self, context, model_input: pd.DataFrame, params=None):
            fn = {**BASELINES, **CANDIDATES}[self.model_name]
            horizon = int((params or {}).get("horizon", 3))
            series = pd.Series(
                model_input["y"].to_numpy(float),
                index=pd.DatetimeIndex(pd.to_datetime(model_input["ds"])),
            )
            fc = fn(series, horizon)
            return pd.DataFrame({"mean": fc.mean, "lo80": fc.lo80, "hi80": fc.hi80})

    return RefitForecaster(model_name)


def run(engine: sa.Engine | None = None, include_prophet: bool = True,
        use_mlflow: bool = True) -> dict:
    targets = datasets.load_targets(engine)
    families: dict[str, ForecastFn] = {**BASELINES, **CANDIDATES}
    if not include_prophet:
        families = {k: v for k, v in families.items() if k != "prophet"}

    mlflow = _mlflow_setup() if use_mlflow else None
    all_metrics: dict[str, dict[str, dict]] = {}
    for target, series in targets.items():
        all_metrics[target] = {}
        for name, fn in families.items():
            result = bt.backtest(series, fn, name, target)
            m = result.metrics()
            all_metrics[target][name] = m
            if mlflow:
                with mlflow.start_run(run_name=f"{target}::{name}"):
                    mlflow.log_params({
                        "model": name, "target": target,
                        "min_train": bt.MIN_TRAIN, "horizon": bt.HORIZON,
                        "step": bt.STEP, "cv": "rolling-origin-expanding",
                    })
                    mlflow.log_metrics(m)

    champions: dict[str, dict] = {}
    for target, table in all_metrics.items():
        baseline_best = min(
            (n for n in table if n in BASELINES), key=lambda n: table[n]["mape"]
        )
        candidates = {n: v for n, v in table.items() if n in CANDIDATES}
        beat_baselines = {
            n: v for n, v in candidates.items()
            if v["mape"] < min(table[b]["mape"] for b in BASELINES if b in table)
        }
        if beat_baselines:
            champ = min(beat_baselines, key=lambda n: beat_baselines[n]["mape"])
        else:
            champ = baseline_best  # the honest outcome: baseline stays champion
        champions[target] = {
            "champion": champ,
            "champion_mape": table[champ]["mape"],
            "baseline_best": baseline_best,
            "baseline_mape": table[baseline_best]["mape"],
            "beat_baseline": champ in CANDIDATES,
        }
        if mlflow:
            with mlflow.start_run(run_name=f"{target}::champion"):
                mlflow.set_tag("champion", "true")
                mlflow.log_params({"target": target, "model": champ})
                mlflow.log_metrics({"mape": table[champ]["mape"],
                                    "coverage80": table[champ]["coverage80"]})
                s = targets[target]
                example = pd.DataFrame({
                    "ds": s.index[:5].astype(str), "y": s.to_numpy(float)[:5],
                })
                # Signature must declare the horizon param or pyfunc silently
                # ignores it at inference time and every caller gets 3 months.
                from mlflow.models import infer_signature

                out_example = pd.DataFrame(
                    {"mean": [0.0], "lo80": [0.0], "hi80": [0.0]}
                )
                mlflow.pyfunc.log_model(
                    name="model",
                    python_model=_make_refit_forecaster(champ),
                    registered_model_name=_registry_name(target),
                    input_example=example,
                    signature=infer_signature(example, out_example,
                                              params={"horizon": 3}),
                )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = {"metrics": all_metrics, "champions": champions}
    (REPORTS_DIR / "backtest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def format_report(report: dict) -> str:
    lines = []
    for target, table in report["metrics"].items():
        lines.append(f"\n{target}")
        for name, m in sorted(table.items(), key=lambda kv: kv[1]["mape"]):
            tag = " <- champion" if report["champions"][target]["champion"] == name else ""
            lines.append(
                f"  {name:15s} MAPE {m['mape']:7.2%}  RMSE {m['rmse']:12.2f}  "
                f"cov80 {m['coverage80']:5.0%}{tag}"
            )
    n_beat = sum(c["beat_baseline"] for c in report["champions"].values())
    lines.append(
        f"\nCandidates beat baselines on {n_beat}/{len(report['champions'])} targets"
    )
    return "\n".join(lines)
