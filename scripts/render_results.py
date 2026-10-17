"""Render reports/*.json into the README (between markers) and into docs/MODEL-CARD.md.

python scripts/render_results.py --write     rewrite both files
python scripts/render_results.py --check     exit 1 if the reports are missing or unreadable
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def load(name: str) -> dict | None:
    path = REPORTS / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{digits}f}%"


def num(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def results_section(m: dict | None, f: dict | None, q: dict | None) -> str:
    lines = []
    if q:
        failed = [r for r in q["results"] if not r["passed"]]
        lines.append(
            f"- **Data quality:** {q['passed']} of {q['checks']} checks pass; {len(q['critical_failures'])} critical failures. "
            + (
                "Warnings: "
                + "; ".join(f"{r['check_name']} ({r['failing']:,} of {r['total']:,} rows)" for r in failed)
                + "."
                if failed
                else "No warnings."
            )
        )
    if m:
        c = m["cohort"]
        champ = m["champion"]
        t = m["models"][champ]["test"]
        other = next(k for k in m["models"] if k != champ)
        o = m["models"][other]["test"]
        lines.append(
            f"- **Readmission model** ({c['admissions_modeled']:,} admissions from {c['patients_modeled']:,} patients, "
            f"{pct(c['prevalence'])} readmitted within 30 days; patient-level split, {c['test']:,} held-out admissions from {c['test_patients']:,} patients): "
            f"champion **{champ}** with held-out **AUROC {num(t['auroc'])}** (95% bootstrap CI {num(t['auroc_ci95'][0])} to {num(t['auroc_ci95'][1])}), "
            f"AUPRC {num(t['auprc'])}, Brier {num(t['brier'])}, **ECE {num(t['ece'])}** after isotonic calibration "
            f"(uncalibrated ECE {num(m['models'][champ]['test_uncalibrated']['ece'])}); {other} AUROC {num(o['auroc'])}, ECE {num(o['ece'])}."
        )
        sex = {r["group"]: r for r in m["fairness"]["sex"]}
        if {"female", "male"} <= set(sex) and all("auroc" in sex[g] for g in ("female", "male")):
            lines.append(
                f"- **Fairness (held-out):** AUROC female {num(sex['female']['auroc'])} vs male {num(sex['male']['auroc'])}; "
                f"selection rate at the {m['threshold']:.0%} threshold female {pct(sex['female']['selection_rate'])} vs male {pct(sex['male']['selection_rate'])}. "
                "Age bands and race are in the model card."
            )
    if f and f.get("latency_s"):
        b = f.get("breakdown", {})
        lines.append(
            f"- **Summary faithfulness** ({f['model']}, {f['retrieval']}, one model call per section, {f['patients']} patients, {f['claims']} claims, "
            f"{f.get('claims_per_summary')} per summary, coverage {pct(f.get('coverage'))} of the facts shown): "
            f"**{pct(f['faithfulness_rate'])} of claims supported** by the resource they cite; "
            f"unsupported {b.get('unsupported', 0)}, missing citation {b.get('missing_citation', 0)}, unknown citation {b.get('unknown_citation', 0)}, "
            f"invented citation keys dropped {f.get('dropped_citations', 0)}; median generation {f['latency_s']['median']} s per patient on the local GPU."
        )
        other = load("summary-faithfulness-qwen2.5-7b.json")
        if other and other.get("latency_s"):
            ob = other.get("breakdown", {})
            lines.append(
                f"- **Same eval with {other['model']}:** {pct(other['faithfulness_rate'])} of {other['claims']} claims supported "
                f"(unsupported {ob.get('unsupported', 0)}, missing citation {ob.get('missing_citation', 0)}), coverage {pct(other.get('coverage'))}, "
                f"median {other['latency_s']['median']} s per patient. The checker is the same; the model is what changes."
            )
        fake = load("summary-faithfulness-fake.json")
        if fake:
            lines.append(
                f"- **Checker self-test:** the deterministic fake model, which copies facts verbatim, scores {pct(fake['faithfulness_rate'])} on {fake['claims']} claims."
            )
    return "\n".join(line for line in lines if line) or "_No reports yet: run `make pipeline`._"


def model_card(m: dict, f: dict | None) -> str:
    c = m["cohort"]
    champ = m["champion"]
    t = m["models"][champ]["test"]
    rows = []
    for name, r in m["models"].items():
        tt, tu = r["test"], r["test_uncalibrated"]
        rows.append(
            f"| {name}{' (champion)' if name == champ else ''} | {num(r['validation']['auroc'])} | {num(tt['auroc'])} | "
            f"{num(tt['auroc_ci95'][0])}-{num(tt['auroc_ci95'][1])} | {num(tt['auprc'])} | {num(tt['brier'])} | {num(tu['ece'])} | {num(tt['ece'])} | "
            f"{num(tt['calibration_slope'], 2)} / {num(tt['calibration_intercept'], 2)} |"
        )
    fairness = []
    for attr in ("sex", "age_band", "race", "ethnicity"):
        fairness.append(
            f"\n**By {attr.replace('_', ' ')}**\n\n| group | n | prevalence | mean predicted | AUROC | calibration-in-the-large | selection rate | TPR | FPR |\n|---|---|---|---|---|---|---|---|---|"
        )
        for r in m["fairness"][attr]:
            fairness.append(
                f"| {r['group']} | {r['n']} | {pct(r['prevalence'])} | {pct(r['mean_predicted'])} | {num(r.get('auroc'))} | "
                f"{num(r.get('calibration_in_the_large'))} | {pct(r['selection_rate'])} | {pct(r.get('tpr'))} | {pct(r.get('fpr'))} |"
            )
    shap = "\n".join(
        f"| {d['feature']} | {d['mean_abs_shap']:.4f} |" for d in m["explanations"]["xgboost_mean_abs_shap"][:12]
    )
    coef = "\n".join(
        f"| {d['feature']} | {d['coefficient']:+.3f} |"
        for d in m["explanations"]["logistic_regression_coefficients"][:12]
    )
    reliability = "\n".join(
        f"| {b['lower']:.1f}-{b['upper']:.1f} | {b['n']} | {pct(b['mean_predicted'])} | {pct(b['observed'])} |"
        for b in t["reliability"]
    )
    faith = ""
    if f:
        faith = (
            f"\n## Companion: summary faithfulness\n\nThe cited-summary feature is evaluated separately (`reports/summary-faithfulness.json`): "
            f"{pct(f['faithfulness_rate'])} of {f['claims']} claims over {f['patients']} patients were supported by the resource they cite "
            f"({f['model']}, {f['retrieval']}).\n"
        )
    return f"""# Model card: 30-day readmission risk ({m["model_version"]})

Generated by `scripts/render_results.py` from `reports/model-metrics.json` on {m["trained_at"][:10]}. Numbers change when the
pipeline is re-run; the text does not.

## Intended use

A discharge-time estimate of the probability that an inpatient admission is followed by another inpatient
admission within 30 days, for ranking a care-transition worklist. It is trained and evaluated on **Synthea
synthetic patients only**. It must not be used on real patients without retraining, re-validation on that
population, and clinical governance. The readmission dynamics in Synthea are driven by its disease modules
(for example chemotherapy cycles and post-surgical follow-up are coded as inpatient encounters), so the
discrimination below says something about the pipeline, not about real hospitals.

## Data

- Cohort: {c["admissions_total"]:,} inpatient admissions ({c["admissions_modeled"]:,} modeled after excluding {c["excluded_died"]} in-hospital
  deaths and {c["excluded_censored"]} admissions censored by the end of the data) from {c["patients_modeled"]:,} patients.
- Label: another inpatient admission 0 to 30 days after discharge; prevalence {pct(c["prevalence"])}.
- Split by patient: {c["train"]:,} train / {c["val"]:,} validation / {c["test"]:,} test admissions ({c["test_patients"]:,} test patients).
  The validation fold is used for early stopping, model selection and isotonic calibration; the test fold is
  touched once.
- Features ({len(m["features"])}): age at admission, sex, length of stay, prior utilisation (admissions, ED and ambulatory
  visits in the past year, days since the last discharge), active chronic-condition flags, medication counts,
  diagnoses and procedures during the stay, the latest vitals and labs before discharge, and the ten most
  common admission reasons. Race and ethnicity are **not** features; they are reported below.
- Label hash `{c["label_hash"]}` ties this card to the cohort it was computed on.

## Performance on the held-out cohort

| model | val AUROC | test AUROC | 95% CI | AUPRC | Brier | ECE raw | ECE calibrated | slope / intercept |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Reliability of the champion after isotonic calibration (10 equal-width bins):

| predicted | n | mean predicted | observed |
|---|---|---|---|
{reliability}

![reliability](../reports/calibration.png)

## Fairness

Operating threshold {m["threshold"]:.0%}. Groups with fewer than 30 admissions or one class only report counts.
{chr(10).join(fairness)}

## What drives the predictions

Mean absolute SHAP on the held-out cohort (boosted model):

| feature | mean abs SHAP |
|---|---|
{shap}

Largest standardised coefficients of the logistic regression (features with `missingindicator_` mark a value
that was absent):

| feature | coefficient |
|---|---|
{coef}

![shap](../reports/shap.png)

## Limitations

- Synthetic data: no real patients, no documentation quality problems, no social determinants, no payer
  data. The model has never seen a real record.
- The prediction is made at discharge with features that are all available by then; it is not a model of
  admission-time risk.
- Chronic-condition flags come from display-text patterns on SNOMED names, which is adequate for Synthea
  and would need a proper value set in production.
- Dates in the lakehouse are shifted per patient for de-identification; ages at admission are therefore
  approximate to within a month.
- Calibration is checked on {t["n"]:,} admissions; the per-group calibration figures for small groups are noisy.
{faith}
## Monitoring

Scores are written to `gold.risk_scores` with the split each admission belonged to. The API exposes the
model version as a Prometheus gauge; a retrain is a re-run of `cfp train`, which rewrites this card.
"""


def main() -> int:
    m, f, q = load("model-metrics.json"), load("summary-faithfulness.json"), load("data-quality.json")
    if "--check" in sys.argv:
        missing = [
            n
            for n, v in (("model-metrics.json", m), ("summary-faithfulness.json", f), ("data-quality.json", q))
            if v is None
        ]
        print("missing: " + ", ".join(missing) if missing else "reports present")
        return 1 if missing else 0
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    start, end = "<!-- RESULTS -->", "<!-- /RESULTS -->"
    if start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = before + start + "\n" + results_section(m, f, q) + "\n" + end + after
        if "--write" in sys.argv:
            readme.write_text(text, encoding="utf-8")
            print("wrote README.md")
    if m and "--write" in sys.argv:
        (ROOT / "docs" / "MODEL-CARD.md").write_text(model_card(m, f), encoding="utf-8")
        print("wrote docs/MODEL-CARD.md")
    if "--write" not in sys.argv:
        print(results_section(m, f, q))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
