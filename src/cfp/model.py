"""30-day readmission risk: logistic regression and gradient boosting on discharge-time features, split by
patient, calibrated on a validation fold, evaluated on a held-out cohort with calibration and fairness
tables. Scores for every admission land in gold; metrics and the model card inputs in reports/."""

from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from cfp.gold import FLAG_COLUMNS, HISTORY_COLUMNS, LAB_COLUMNS
from cfp.lake import Lake

MODEL_VERSION = "readmission-v1"
TOP_REASONS = 10
NUMERIC = ["age_at_admission", "los_days", *HISTORY_COLUMNS, *LAB_COLUMNS]
BINARY = [*FLAG_COLUMNS, "sex_female"]


@dataclass
class Split:
    train: pd.Index
    val: pd.Index
    test: pd.Index


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")[:40]


def build_features(adm: pd.DataFrame, reasons: list[str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Feature frame from gold.admissions rows. `reasons` fixes the one-hot vocabulary (from the training fold)."""
    x = adm[NUMERIC].astype(float).copy()
    for col in FLAG_COLUMNS:
        x[col] = adm[col].fillna(0).astype(int)
    x["sex_female"] = (adm["sex"] == "female").astype(int)
    if reasons is None:
        reasons = list(adm["reason_display"].dropna().value_counts().index[:TOP_REASONS])
    for r in reasons:
        x[f"reason__{_slug(r)}"] = (adm["reason_display"] == r).astype(int)
    return x, reasons


def split_by_patient(adm: pd.DataFrame, test_size: float, val_size: float, seed: int) -> Split:
    groups = adm["patient_id"].to_numpy()
    rest, test = next(GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed).split(adm, groups=groups))
    rest_df = adm.iloc[rest]
    tr, va = next(
        GroupShuffleSplit(n_splits=1, test_size=val_size / (1 - test_size), random_state=seed).split(
            rest_df, groups=rest_df["patient_id"].to_numpy()
        )
    )
    return Split(train=adm.index[rest][tr], val=adm.index[rest][va], test=adm.index[test])


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> tuple[float, list[dict]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    curve = []
    for b in range(bins):
        mask = idx == b
        if not mask.any():
            continue
        conf, acc, n = float(p[mask].mean()), float(y[mask].mean()), int(mask.sum())
        ece += n / len(p) * abs(conf - acc)
        curve.append(
            {
                "bin": b,
                "lower": float(edges[b]),
                "upper": float(edges[b + 1]),
                "mean_predicted": conf,
                "observed": acc,
                "n": n,
            }
        )
    return float(ece), curve


def bootstrap_auroc_ci(y: np.ndarray, p: np.ndarray, seed: int, n: int = 1000) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    scores = []
    size = len(y)
    for _ in range(n):
        i = rng.integers(0, size, size)
        if len(np.unique(y[i])) < 2:
            continue
        scores.append(roc_auc_score(y[i], p[i]))
    return float(np.percentile(scores, 2.5)), float(np.percentile(scores, 97.5))


def calibration_fit(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """Slope and intercept of a logistic recalibration (1 and 0 are perfect)."""
    logit = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6))).reshape(-1, 1)
    if len(np.unique(y)) < 2:
        return float("nan"), float("nan")
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(logit, y)
    return float(lr.coef_[0][0]), float(lr.intercept_[0])


def metrics_for(y: np.ndarray, p: np.ndarray, seed: int) -> dict[str, Any]:
    if len(np.unique(y)) < 2:
        return {"n": len(y), "positives": int(y.sum())}
    ece, curve = expected_calibration_error(y, p)
    lo, hi = bootstrap_auroc_ci(y, p, seed)
    slope, intercept = calibration_fit(y, p)
    return {
        "n": len(y),
        "positives": int(y.sum()),
        "prevalence": float(y.mean()),
        "auroc": float(roc_auc_score(y, p)),
        "auroc_ci95": [lo, hi],
        "auprc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
        "ece": ece,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "reliability": curve,
    }


def fairness_table(
    frame: pd.DataFrame, y: np.ndarray, p: np.ndarray, threshold: float, attribute: str, seed: int
) -> list[dict]:
    col = frame[attribute].fillna("unknown").astype(str).to_numpy()
    rows = []
    for value in sorted(set(col)):
        m = col == value
        yy, pp = y[m], p[m]
        row: dict[str, Any] = {
            "group": value,
            "n": int(m.sum()),
            "prevalence": float(yy.mean()) if len(yy) else None,
            "mean_predicted": float(pp.mean()) if len(pp) else None,
            "selection_rate": float((pp >= threshold).mean()) if len(pp) else None,
        }
        if len(yy) >= 30 and len(np.unique(yy)) == 2:
            row["auroc"] = float(roc_auc_score(yy, pp))
            row["calibration_in_the_large"] = float(pp.mean() - yy.mean())
            row["tpr"] = float(((pp >= threshold) & (yy == 1)).sum() / max(1, (yy == 1).sum()))
            row["fpr"] = float(((pp >= threshold) & (yy == 0)).sum() / max(1, (yy == 0).sum()))
        rows.append(row)
    return sorted(rows, key=lambda r: -r["n"])


def _lr() -> Pipeline:
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            ("lr", LogisticRegression(C=0.5, max_iter=3000)),
        ]
    )


def _xgb(seed: int):
    from xgboost import XGBClassifier

    return XGBClassifier(
        n_estimators=800,
        learning_rate=0.03,
        max_depth=4,
        min_child_weight=5,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        tree_method="hist",
        eval_metric="logloss",
        early_stopping_rounds=50,
        random_state=seed,
        n_jobs=4,
    )


def train(
    lake: Lake,
    reports_dir: Path,
    seed: int = 42,
    test_size: float = 0.2,
    val_size: float = 0.15,
    threshold: float = 0.3,
    mlflow_uri: str | None = None,
) -> dict[str, Any]:
    adm = lake.read_delta("gold", "admissions").to_pandas()
    cohort = adm[adm["in_modeling_cohort"]].reset_index(drop=True)
    if cohort["readmitted"].sum() < 10 or len(cohort) < 50:
        raise RuntimeError(
            f"cohort too small to model: {len(cohort)} admissions, {int(cohort['readmitted'].sum())} readmissions"
        )
    split = split_by_patient(cohort, test_size, val_size, seed)
    x_train, reasons = build_features(cohort.loc[split.train])
    x_val, _ = build_features(cohort.loc[split.val], reasons)
    x_test, _ = build_features(cohort.loc[split.test], reasons)
    y_train = cohort.loc[split.train, "readmitted"].astype(int).to_numpy()
    y_val = cohort.loc[split.val, "readmitted"].astype(int).to_numpy()
    y_test = cohort.loc[split.test, "readmitted"].astype(int).to_numpy()
    features = list(x_train.columns)

    lr = _lr().fit(x_train, y_train)
    xgb = _xgb(seed).fit(x_train, y_train, eval_set=[(x_val, y_val)], verbose=False)
    models: dict[str, Any] = {"logistic_regression": lr, "xgboost": xgb}
    results: dict[str, Any] = {}
    calibrators: dict[str, IsotonicRegression] = {}
    for name, m in models.items():
        p_val = m.predict_proba(x_val)[:, 1]
        p_test_raw = m.predict_proba(x_test)[:, 1]
        iso = IsotonicRegression(out_of_bounds="clip").fit(p_val, y_val)
        calibrators[name] = iso
        p_test = iso.predict(p_test_raw)
        results[name] = {
            "validation": {"auroc": float(roc_auc_score(y_val, p_val)) if len(np.unique(y_val)) == 2 else None},
            "test_uncalibrated": metrics_for(y_test, p_test_raw, seed),
            "test": metrics_for(y_test, p_test, seed),
        }
    champion = max(results, key=lambda k: results[k]["validation"]["auroc"] or 0.0)
    best = models[champion]
    p_test = calibrators[champion].predict(best.predict_proba(x_test)[:, 1])
    test_frame = cohort.loc[split.test].reset_index(drop=True)
    fairness = {
        attr: fairness_table(test_frame, y_test, p_test, threshold, attr, seed)
        for attr in ("sex", "age_band", "race", "ethnicity")
    }

    # Explanations: SHAP for the boosted model, coefficients for the linear one.
    explanations: dict[str, Any] = {}
    import shap

    explainer = shap.TreeExplainer(xgb)
    shap_test = np.asarray(explainer.shap_values(x_test))
    mean_abs = np.abs(shap_test).mean(axis=0)
    ranked = sorted(zip(features, mean_abs.tolist(), strict=True), key=lambda t: -t[1])[:20]
    explanations["xgboost_mean_abs_shap"] = [{"feature": f, "mean_abs_shap": float(v)} for f, v in ranked]
    coef = lr.named_steps["lr"].coef_[0]
    names_after_impute = list(lr.named_steps["impute"].get_feature_names_out(features))
    ranked_coef = sorted(zip(names_after_impute, coef.tolist(), strict=True), key=lambda t: -abs(t[1]))[:20]
    explanations["logistic_regression_coefficients"] = [{"feature": f, "coefficient": float(c)} for f, c in ranked_coef]

    # Score every admission (not only the modeling cohort) with the champion and its top factors.
    x_all, _ = build_features(adm, reasons)
    p_all = calibrators[champion].predict(best.predict_proba(x_all)[:, 1])
    shap_all = np.asarray(explainer.shap_values(x_all)) if champion == "xgboost" else None
    split_label = pd.Series("not_modeled", index=adm.index)
    for label, idx in (("train", split.train), ("val", split.val), ("test", split.test)):
        split_label.loc[adm.index[adm["admission_id"].isin(cohort.loc[idx, "admission_id"])]] = label
    factors = []
    for i in range(len(adm)):
        if shap_all is not None:
            magnitude = np.abs(shap_all[i]).astype(float)
            order = np.argsort(magnitude)[::-1][:3]
            factors.append(
                json.dumps(
                    [
                        {
                            "feature": features[j],
                            "value": _jsonable(x_all.iloc[i, j]),
                            "shap": round(float(shap_all[i][j]), 4),
                        }
                        for j in order
                    ]
                )
            )
        else:
            factors.append(json.dumps([]))
    scores = pa.Table.from_pandas(
        pd.DataFrame(
            {
                "admission_id": adm["admission_id"],
                "patient_id": adm["patient_id"],
                "discharge_ts": adm["discharge_ts"],
                "risk": np.round(p_all, 4),
                "readmitted": adm["readmitted"],
                "split": split_label.to_numpy(),
                "top_factors": factors,
                "model_version": MODEL_VERSION,
            }
        ),
        preserve_index=False,
    )
    lake.write_delta("gold", "risk_scores", scores)

    data_hash = hashlib.sha256(
        pd.util.hash_pandas_object(cohort[["admission_id", "readmitted"]], index=False).to_numpy().tobytes()
    ).hexdigest()[:12]
    metrics = {
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cohort": {
            "admissions_total": len(adm),
            "admissions_modeled": len(cohort),
            "patients_modeled": int(cohort["patient_id"].nunique()),
            "excluded_died": int(adm["died_in_hospital"].sum()),
            "excluded_censored": int(adm["censored"].sum()),
            "prevalence": float(cohort["readmitted"].mean()),
            "train": len(split.train),
            "val": len(split.val),
            "test": len(split.test),
            "test_patients": int(cohort.loc[split.test, "patient_id"].nunique()),
            "label_hash": data_hash,
        },
        "features": features,
        "threshold": threshold,
        "champion": champion,
        "models": results,
        "fairness": fairness,
        "explanations": explanations,
        "xgboost_best_iteration": int(getattr(xgb, "best_iteration", 0) or 0),
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "model-metrics.json").write_text(json.dumps(metrics, indent=2, default=_jsonable), encoding="utf-8")
    model_dir = reports_dir / "model"
    model_dir.mkdir(exist_ok=True)
    xgb.save_model(model_dir / "xgboost.json")
    with (model_dir / "calibrators.pkl").open("wb") as fh:
        pickle.dump(dict(calibrators), fh)
    with (model_dir / "logistic_regression.pkl").open("wb") as fh:
        pickle.dump(lr, fh)
    (model_dir / "features.json").write_text(
        json.dumps({"features": features, "reasons": reasons}, indent=2), encoding="utf-8"
    )
    _plots(metrics, reports_dir)
    if mlflow_uri:
        _log_mlflow(mlflow_uri, metrics, reports_dir)
    return metrics


def _jsonable(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, float) and np.isnan(v):
        return None
    return v


def _plots(metrics: dict, reports_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    for name, style in (("logistic_regression", "o-"), ("xgboost", "s-")):
        curve = metrics["models"][name]["test"].get("reliability", [])
        ax.plot(
            [c["mean_predicted"] for c in curve],
            [c["observed"] for c in curve],
            style,
            label=f"{name} (ECE {metrics['models'][name]['test'].get('ece', 0):.3f})",
        )
    ax.plot([0, 1], [0, 1], "k--", linewidth=0.8, label="perfect")
    ax.set_xlabel("mean predicted risk")
    ax.set_ylabel("observed 30-day readmission")
    ax.set_title("Reliability on the held-out cohort (isotonic-calibrated)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(reports_dir / "calibration.png", dpi=120)
    plt.close(fig)

    top = metrics["explanations"]["xgboost_mean_abs_shap"][:15][::-1]
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.barh([t["feature"] for t in top], [t["mean_abs_shap"] for t in top])
    ax.set_xlabel("mean |SHAP| (held-out cohort)")
    ax.set_title("What moves the boosted model")
    fig.tight_layout()
    fig.savefig(reports_dir / "shap.png", dpi=120)
    plt.close(fig)


def _log_mlflow(uri: str, metrics: dict, reports_dir: Path) -> None:
    import mlflow

    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("readmission")
    with mlflow.start_run(run_name=metrics["model_version"]):
        mlflow.log_params(
            {
                "champion": metrics["champion"],
                "threshold": metrics["threshold"],
                "features": len(metrics["features"]),
                "admissions_modeled": metrics["cohort"]["admissions_modeled"],
                "label_hash": metrics["cohort"]["label_hash"],
            }
        )
        for name, res in metrics["models"].items():
            for k in ("auroc", "auprc", "brier", "ece"):
                if k in res["test"]:
                    mlflow.log_metric(f"{name}_test_{k}", res["test"][k])
        for f in ("model-metrics.json", "calibration.png", "shap.png"):
            mlflow.log_artifact(str(reports_dir / f))
        mlflow.log_artifacts(str(reports_dir / "model"), artifact_path="model")
