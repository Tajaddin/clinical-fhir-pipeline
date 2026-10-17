from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfp.db import audit_counts, current_summary, init_db, list_audit, make_engine, save_summary, write_audit
from cfp.faithfulness import evaluate, judge_summary, supports
from cfp.gold import build_gold
from cfp.llm import Completion, FakeLLM, _extract_json
from cfp.summaries import SECTIONS, Fact, Retriever, build_prompt, load_facts, load_patient, summarize
from conftest import REFERENCE

# ---- summaries and faithfulness ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gold_lake(lake):
    build_gold(lake, readmission_days=30, reference_date=REFERENCE)
    return lake


@pytest.fixture(scope="module")
def a_patient(gold_lake) -> str:
    con = gold_lake.connect([("gold", "patient_facts")])
    return con.sql("select patient_id from gold_patient_facts group by 1 order by count(*) desc limit 1").fetchone()[0]


def test_retrieval_is_per_section_bounded_and_prefers_active_problems(gold_lake, a_patient):
    facts = load_facts(gold_lake, a_patient)
    assert len(facts) >= 20
    retrieved = Retriever(per_section=3).retrieve(facts)
    assert set(retrieved) <= set(SECTIONS) and all(len(v) <= 3 for v in retrieved.values())
    problems = retrieved["problems"]
    assert problems and all(f.section == "problems" for f in problems)
    if any("(active)" in f.text for f in facts if f.section == "problems"):
        assert "(active)" in problems[0].text


def test_prompt_lists_facts_with_ids_and_no_identifiers(gold_lake, a_patient):
    patient = load_patient(gold_lake, a_patient)
    retrieved = Retriever(per_section=4).retrieve(load_facts(gold_lake, a_patient))
    prompt = build_prompt(patient, retrieved)
    assert patient["pseudonym"] in prompt and "## Problems" in prompt
    assert "[P1]" in prompt and "Condition/" not in prompt and "Fakeson" not in prompt and "999-12-3456" not in prompt


def test_fake_llm_summary_cites_shown_facts_and_is_fully_faithful(gold_lake, a_patient):
    summary = summarize(gold_lake, a_patient, FakeLLM(), Retriever(per_section=4))
    assert summary is not None and summary.sections and summary.dropped_citations == 0
    assert summary.coverage == 1.0 and summary.model_calls == summary.sections_shown == len(summary.sections)
    claims = summary.claims()
    assert claims and all(c.citations and set(c.citations) <= set(summary.facts_shown) for c in claims)
    verdicts = judge_summary(summary, load_facts(gold_lake, a_patient))
    assert {v.status for v in verdicts} == {"supported"}


def test_invented_claims_and_unknown_citations_are_caught(gold_lake, a_patient):
    facts = load_facts(gold_lake, a_patient)
    summary = summarize(gold_lake, a_patient, FakeLLM(unsupported_every=3), Retriever(per_section=4))
    verdicts = judge_summary(summary, facts)
    statuses = [v.status for v in verdicts]
    assert statuses.count("unsupported") == len(statuses) // 3
    assert all(v.anchor for v in verdicts if v.status == "supported")

    class BadCiter(FakeLLM):
        def complete_json(self, system, user, max_tokens=1800):
            c = super().complete_json(system, user, max_tokens)
            c.content["claims"][0]["citations"] = ["Condition/does-not-exist"]
            c.content["claims"].append({"text": "No citation here", "citations": []})
            return c

    bad = summarize(gold_lake, a_patient, BadCiter(), Retriever(per_section=4))
    assert bad.dropped_citations == bad.sections_shown
    kinds = {v.status for v in judge_summary(bad, facts)}
    assert "missing_citation" in kinds and "supported" in kinds


def test_supports_uses_name_value_or_date_anchors():
    f = Fact(
        "Observation/1", "Observation", "results", None, "Hemoglobin A1c/Hemoglobin.total in Blood: 6.4 % on 2024-02-01"
    )
    assert supports("HbA1c was 6.4% on 2024-02-01.", f) is not None
    assert supports("Hemoglobin A1c 6.4 percent", f) is not None
    assert supports("Blood pressure 120/80 last year", f) is None
    c = Fact(
        "Condition/2", "Condition", "problems", None, "Diabetes mellitus type 2 (disorder), since 2019-03-02 (active)"
    )
    assert supports("Type 2 diabetes mellitus, active since 2019.", c).startswith("name:")
    assert supports("Patient has asthma.", c) is None
    bmi = Fact(
        "Observation/3", "Observation", "results", None, "Body mass index (BMI) [Ratio]: 30.4 kg/m2 on 2024-08-21"
    )
    assert supports("BMI 30.4 kg/m2 as of 2024-08-21.", bmi) in ("date:2024-08-21", "value:30.4")
    weight = Fact("Observation/4", "Observation", "results", None, "Body Weight: 75.5 kg on 2024-08-21")
    assert supports("Patient weighs 75.5 kg as of 2024-08-21.", weight).startswith("value+date:")
    enc = Fact(
        "Encounter/5",
        "Encounter",
        "encounters",
        None,
        "Outpatient visit on 2024-09-04: Encounter for check up (procedure), reason Patient referral for dental care (procedure)",
    )
    assert supports("Check-up on 2024-09-04 for a dental care referral.", enc).startswith("date:")
    assert supports("Patient is a male aged 65-89.", enc) is None


def test_evaluate_writes_a_report_with_rates_and_examples(gold_lake, tmp_path):
    con = gold_lake.connect([("gold", "patient_facts")])
    ids = [r[0] for r in con.sql("select distinct patient_id from gold_patient_facts order by 1 limit 5").fetchall()]
    report = evaluate(
        gold_lake, FakeLLM(unsupported_every=4), Retriever(per_section=3), ids, tmp_path / "f.json", tmp_path / "ex"
    )
    assert report["patients"] == 5 and report["claims"] > 10 and report["coverage"] == 1.0
    assert 0.6 <= report["faithfulness_rate"] <= 0.8
    assert report["breakdown"]["unsupported"] >= 1 and set(report["by_section"]) <= {
        title for title, _ in SECTIONS.values()
    }
    assert (tmp_path / "f.json").exists() and len(list((tmp_path / "ex").glob("*.json"))) == 3


def test_extract_json_handles_fences_prose_and_truncation():
    assert _extract_json('```json\n{"a": 1}\n```') == ({"a": 1}, False)
    assert _extract_json('Here you go: {"sections": []} thanks') == ({"sections": []}, False)
    cut = '{"sections": [{"title": "Problems", "claims": [{"text": "a", "citations": ["P1"]}, {"text": "b", "cit'
    content, repaired = _extract_json(cut)
    assert repaired and content["sections"][0]["claims"] == [{"text": "a", "citations": ["P1"]}]
    with pytest.raises(json.JSONDecodeError):
        _extract_json("no json at all")
    assert isinstance(Completion(content={}, latency_s=0.1), Completion)


# ---- audit log and summaries store -------------------------------------------------------------------------


def test_audit_log_and_summary_store_roundtrip(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'cfp.db').as_posix()}")
    init_db(engine)
    write_audit(
        engine,
        actor="key-abc",
        role="analyst",
        action="patient.read",
        patient_id="p1",
        resource="/patients/p1",
        purpose="treatment",
        request_id="r1",
        client_ip="127.0.0.1",
        status_code=200,
    )
    write_audit(
        engine,
        actor="key-abc",
        role="analyst",
        action="summary.read",
        patient_id="p1",
        resource="/patients/p1/summary",
        purpose="treatment",
        request_id="r2",
        client_ip="127.0.0.1",
        status_code=200,
    )
    write_audit(
        engine,
        actor="key-xyz",
        role="admin",
        action="audit.read",
        patient_id=None,
        resource="/audit",
        purpose=None,
        request_id="r3",
        client_ip="127.0.0.1",
        status_code=200,
    )
    rows = list_audit(engine, limit=10)
    assert [r["action"] for r in rows] == ["audit.read", "summary.read", "patient.read"]
    assert len(list_audit(engine, patient_id="p1")) == 2 and audit_counts(engine)["patient.read"] == 1
    first = save_summary(engine, "p1", "fake", "summary-v1", {"sections": [1]}, {"faithfulness_rate": 1.0})
    second = save_summary(engine, "p1", "fake", "summary-v1", {"sections": [2]}, None)
    cur = current_summary(engine, "p1")
    assert second > first and cur["id"] == second and cur["payload"] == {"sections": [2]} and cur["current"] is True
    assert current_summary(engine, "nobody") is None


# ---- model -------------------------------------------------------------------------------------------------


def test_train_produces_calibrated_metrics_fairness_scores_and_artifacts(trained):
    big_lake, metrics, reports = trained
    assert metrics["champion"] in {"logistic_regression", "xgboost"}
    cohort = metrics["cohort"]
    assert cohort["train"] + cohort["val"] + cohort["test"] == cohort["admissions_modeled"]
    for name in ("logistic_regression", "xgboost"):
        t = metrics["models"][name]["test"]
        assert 0.5 <= t["auroc"] <= 1.0 and 0 <= t["ece"] <= 0.5 and t["reliability"]
        assert t["auroc_ci95"][0] <= t["auroc"] <= t["auroc_ci95"][1]
    assert {r["group"] for r in metrics["fairness"]["sex"]} <= {"male", "female"}
    assert metrics["explanations"]["xgboost_mean_abs_shap"][0]["mean_abs_shap"] >= 0
    scores = big_lake.read_delta("gold", "risk_scores").to_pandas()
    assert len(scores) == cohort["admissions_total"] and scores["risk"].between(0, 1).all()
    assert set(scores["split"]) >= {"train", "test"} and json.loads(scores["top_factors"].iloc[0])
    for f in (
        "model-metrics.json",
        "calibration.png",
        "shap.png",
        "model/xgboost.json",
        "model/calibrators.pkl",
        "model/features.json",
    ):
        assert (reports / f).exists(), f
    # Patients never straddle the split.
    adm = (
        big_lake.read_delta("gold", "admissions")
        .to_pandas()
        .merge(scores[["admission_id", "split"]], on="admission_id")
    )
    per_patient = adm[adm["split"] != "not_modeled"].groupby("patient_id")["split"].nunique()
    assert (per_patient == 1).all()
    assert Path(reports / "mlflow.db").exists()
