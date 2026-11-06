from __future__ import annotations

from datetime import date, datetime, timedelta

import pyarrow as pa
import pytest

from cfp.gold import FLAG_COLUMNS, HISTORY_COLUMNS, LAB_COLUMNS, build_gold
from cfp.lake import Lake
from cfp.quality import CHECKS, Check, QualityError, enforce, run_checks, write_results
from cfp.silver import SCHEMAS
from conftest import N_PATIENTS, REFERENCE


@pytest.fixture(scope="session")
def gold_stats(lake):
    return build_gold(lake, readmission_days=30, reference_date=REFERENCE)


def test_quality_checks_pass_on_the_factory_cohort(lake, tmp_path):
    results = run_checks(lake)
    assert len(results) == len(CHECKS)
    failed = [r for r in results if not r.passed]
    assert failed == [], [(r.check_name, r.failing) for r in failed]
    summary = write_results(lake, results, tmp_path / "dq.json")
    assert summary["passed"] == len(CHECKS) and (tmp_path / "dq.json").exists()
    enforce(summary)
    assert lake.delta_exists("gold", "quality_results")


def test_a_failing_critical_check_raises(lake, tmp_path):
    bad = Check("always fails", "patients", "critical", "select 3", "select 10", "test")
    results = run_checks(lake, [bad])
    assert results[0].failing == 3 and results[0].failing_share == 0.3 and not results[0].passed
    summary = write_results(lake, results, None)
    with pytest.raises(QualityError):
        enforce(summary)


def test_admissions_have_labels_from_known_readmissions(lake, gold_stats, ctx):
    con = lake.connect()
    adm = con.sql("select * from gold_admissions order by patient_id, admit_ts").df()
    assert gold_stats.admissions == len(adm) and len(adm) >= N_PATIENTS
    # The factory readmits patients with index % 3 == 1 within 2 to 25 days and admits some others after 90 days.
    first = adm.groupby("patient_id").first().reset_index()
    readmitted = {pid for pid, r in zip(first["patient_id"], first["readmitted"], strict=True) if r}
    expected = {ctx.pseudo_id(f"{i:08x}-0000-4000-8000-000000000000") for i in range(N_PATIENTS) if i % 3 == 1}
    assert readmitted == expected
    gaps = first.loc[first["readmitted"], "days_to_next_admission"]
    assert gaps.between(0, 30).all()
    assert (first.loc[~first["readmitted"], "days_to_next_admission"].dropna() > 30).all()
    # Features exist and respect time: in-stay counts come from the admission itself, history from before it.
    for col in HISTORY_COLUMNS + FLAG_COLUMNS + LAB_COLUMNS:
        assert col in adm.columns, col
    assert (adm["prior_ambulatory_365d"] >= 0).all() and adm["n_conditions_in_stay"].max() >= 1
    assert adm["bmi"].notna().any() and adm["systolic_bp"].notna().any() and adm["hba1c"].notna().any()
    assert set(adm["age_band"].unique()) <= {"0-17", "18-39", "40-64", "65-89", "90+"}
    assert adm["died_in_hospital"].dtype == bool and adm["in_modeling_cohort"].sum() >= 1


def test_history_features_exclude_the_admission_and_later_events(lake, gold_stats):
    con = lake.connect()
    row = con.sql(
        "select admission_id, patient_id, admit_ts, prior_ambulatory_365d, prior_admissions_all from gold_admissions order by patient_id, admit_ts limit 1"
    ).fetchone()
    _admission_id, pid, admit_ts, prior_amb, prior_adm = row
    manual = con.sql(
        "select count(*) filter (where class_code = 'AMB' and start_ts >= ? - interval 365 day), count(*) filter (where class_code = 'IMP') "
        "from silver_encounters where patient_id = ? and start_ts < ?",
        params=[admit_ts, pid, admit_ts],
    ).fetchone()
    assert (prior_amb, prior_adm) == manual
    assert prior_adm == 0  # the first admission has none before it


def test_patient_facts_cite_real_resources_with_dates_in_text(lake, gold_stats):
    con = lake.connect()
    assert gold_stats.facts > 0 and gold_stats.patients_with_facts == N_PATIENTS
    sections = {r[0] for r in con.sql("select distinct section from gold_patient_facts").fetchall()}
    assert {"problems", "medications", "encounters", "results", "immunizations"} <= sections
    # Every cited condition id exists in silver, and every fact text carries a date.
    missing = con.sql(
        "select count(*) from gold_patient_facts f left join silver_conditions c on f.fact_id = 'Condition/' || c.condition_id "
        "where f.resource_type = 'Condition' and c.condition_id is null"
    ).fetchone()[0]
    assert missing == 0
    undated = con.sql(
        "select count(*) from gold_patient_facts where not regexp_matches(text, '[0-9]{4}-[0-9]{2}-[0-9]{2}')"
    ).fetchone()[0]
    assert undated == 0
    # Blood pressure components cite the parent Observation, once per code.
    bp = con.sql(
        "select text from gold_patient_facts where section = 'results' and text like 'Systolic%' limit 1"
    ).fetchone()
    assert bp is not None and "mm[Hg]" in bp[0]


def test_censoring_uses_the_earliest_end_a_shifted_record_can_have(tmp_path):
    """Records end at the reference date shifted by up to 30 days either way. A late encounter of a patient
    shifted forward must not extend the follow-up of a patient shifted back: only windows that close before
    reference - 30 days are complete for everyone, whether or not a readmission was seen inside them."""
    lake = Lake(str(tmp_path / "lake"))
    for table, schema in SCHEMAS.items():
        lake.write_delta("silver", table, pa.Table.from_pylist([], schema=schema))
    lake.write_delta(
        "silver",
        "patients",
        pa.Table.from_pylist(
            [{"patient_id": p, "sex": "female", "age_years": 60} for p in ("a", "b", "c")], SCHEMAS["patients"]
        ),
    )

    def stay(eid: str, pid: str, start: datetime) -> dict:
        end = start + timedelta(days=3)
        return {
            "encounter_id": eid,
            "patient_id": pid,
            "class_code": "IMP",
            "start_ts": start,
            "end_ts": end,
            "length_hours": 72.0,
        }

    encounters = [
        stay("early", "a", datetime(2024, 9, 25)),  # discharged 09-28: window closes 10-28
        stay("late", "a", datetime(2024, 11, 7)),  # discharged 11-10: window closes 12-10, after 12-02
        stay("shifted-forward", "b", datetime(2025, 1, 25)),  # the latest shifted date in the data
        stay("late-readmitted", "c", datetime(2024, 11, 1)),  # window closes 12-04, after 12-02 ...
        stay("readmission", "c", datetime(2024, 11, 20)),  # ... though this readmission falls inside it
    ]
    lake.write_delta("silver", "encounters", pa.Table.from_pylist(encounters, SCHEMAS["encounters"]))
    build_gold(lake, readmission_days=30, reference_date=date(2025, 1, 1), max_shift_days=30)
    adm = lake.read_delta("gold", "admissions").to_pandas().set_index("admission_id")
    assert adm["censored"].to_dict() == {
        "early": False,
        "late": True,
        "shifted-forward": True,
        "late-readmitted": True,
        "readmission": True,
    }
    assert adm.loc["late-readmitted", "readmitted"] and not adm.loc["late-readmitted", "in_modeling_cohort"]


def test_reference_date_type():
    assert isinstance(REFERENCE, date)
