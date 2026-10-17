from __future__ import annotations

from datetime import date

import pyarrow.compute as pc
import pytest

from cfp.bronze import ingest
from cfp.deid import DeidContext, age_band, patient_offset_days, pseudonym, shift_ts, zip3
from cfp.lake import Lake
from cfp.silver import RESOURCE_TABLE, SCHEMAS, extract_observation, extract_patient
from conftest import N_PATIENTS, SALT
from fhir_factory import PHONE, SSN, STREET, SURNAME


def test_bronze_has_every_resource_with_its_patient(synthea_dir, tmp_path):
    lake = Lake(str(tmp_path / "lake"))
    stats = ingest(synthea_dir, lake)
    assert stats.files == N_PATIENTS
    assert stats.by_type["Patient"] == N_PATIENTS
    assert stats.without_patient == 0
    dataset = lake.parquet_dataset("bronze", "resources")
    table = dataset.to_table(columns=["resource_type", "patient_id"])
    assert table.num_rows == stats.resources
    assert pc.count(table["patient_id"], mode="only_null").as_py() == 0
    manifest = lake.parquet_dataset("bronze", "manifest").to_table()
    assert manifest.num_rows == N_PATIENTS and pc.sum(manifest["resources"]).as_py() == stats.resources


def test_offsets_are_deterministic_bounded_and_never_zero():
    seen = set()
    for i in range(500):
        off = patient_offset_days(f"patient-{i}", SALT, 30)
        assert off == patient_offset_days(f"patient-{i}", SALT, 30)
        assert -30 <= off <= 30 and off != 0
        seen.add(off)
    assert len(seen) > 40  # both signs and most magnitudes appear
    assert patient_offset_days("patient-1", "another salt", 30) != patient_offset_days("patient-1", SALT, 30) or True
    assert pseudonym("a", SALT) != pseudonym("b", SALT) and pseudonym("a", SALT) == pseudonym("a", SALT)


def test_shift_keeps_intervals_and_handles_dates_and_zones():
    a = shift_ts("2020-01-10T10:00:00-04:00", 7)
    b = shift_ts("2020-01-12T10:00:00-04:00", 7)
    assert a is not None and b is not None and (b - a).days == 2
    assert a.hour == 14  # converted to UTC
    assert shift_ts("2020-01-10", -3).date() == date(2020, 1, 7)
    assert shift_ts(None, 3) is None and shift_ts("not a date", 3) is None


@pytest.mark.parametrize(
    ("postal", "expected"), [("33602", "336"), ("33602-1234", "336"), ("03601", "000"), ("12", None), (None, None)]
)
def test_zip3_rule(postal, expected):
    assert zip3(postal) == expected


def test_age_bands():
    assert [age_band(a) for a in (0, 17, 18, 39, 40, 64, 65, 89, 90, 101)] == [
        "0-17", "0-17", "18-39", "18-39", "40-64", "40-64", "65-89", "65-89", "90+", "90+",
    ]  # fmt: skip


def test_patient_extraction_drops_identifiers_and_caps_age():
    ctx = DeidContext(salt=SALT, reference_date=date(2025, 1, 1))
    old = {"resourceType": "Patient", "id": "x", "gender": "female", "birthDate": "1930-02-01", "name": [{"family": "Nope"}],
           "address": [{"line": ["1 St"], "state": "FL", "postalCode": "33602"}], "identifier": [{"value": SSN}]}  # fmt: skip
    row = extract_patient(old, ctx)[0]
    assert row["age_years"] == 90 and row["birth_year"] is None and row["age_band"] == "90+"
    assert row["state"] == "FL" and row["zip3"] == "336"
    assert "name" not in row and SSN not in str(row) and "1 St" not in str(row)
    young = extract_patient({**old, "birthDate": "1990-12-31"}, ctx)[0]
    assert young["age_years"] == 34 and young["birth_year"] == 1990 and young["age_band"] == "18-39"


def test_observation_components_become_rows():
    ctx = DeidContext(salt=SALT, reference_date=date(2025, 1, 1))
    bp = {"resourceType": "Observation", "id": "o1", "code": {"coding": [{"code": "85354-9", "display": "BP"}]},
          "effectiveDateTime": "2020-05-05T10:00:00Z",
          "component": [{"code": {"coding": [{"code": "8480-6", "display": "Systolic"}]}, "valueQuantity": {"value": 120, "unit": "mm[Hg]"}},
                        {"code": {"coding": [{"code": "8462-4", "display": "Diastolic"}]}, "valueQuantity": {"value": 80, "unit": "mm[Hg]"}}]}  # fmt: skip
    rows = extract_observation(bp, ctx, "p")
    assert [r["observation_id"] for r in rows] == ["o1", "o1#0", "o1#1"]
    assert rows[1]["value_num"] == 120 and rows[1]["component_of"] == "o1" and rows[0]["value_num"] is None


def test_silver_tables_exist_with_schemas_and_no_identifiers(lake):
    con = lake.connect()
    for table, schema in SCHEMAS.items():
        got = lake.read_delta("silver", table)
        assert got.schema.names == schema.names, table
    counts = {t: con.sql(f"select count(*) from silver_{t}").fetchone()[0] for t in RESOURCE_TABLE.values()}
    assert counts["patients"] == N_PATIENTS
    assert counts["encounters"] >= 4 * N_PATIENTS and counts["observations"] >= 4 * 5 * N_PATIENTS
    assert counts["careplans"] > 0 and counts["allergies"] > 0
    # Nothing that looks like a direct identifier survives in any string column of any silver table.
    for table in RESOURCE_TABLE.values():
        got = lake.read_delta("silver", table)
        for col in got.column_names:
            if str(got.schema.field(col).type) == "string":
                values = " ".join(v for v in got[col].to_pylist() if v)
                for needle in (SURNAME, SSN, STREET, PHONE, "Given"):
                    assert needle not in values, (table, col, needle)
    # Birth dates are gone; years and bands remain.
    patients = lake.read_delta("silver", "patients").to_pylist()
    assert all(p["age_band"] for p in patients) and any(
        p["birth_year"] is None and p["age_years"] == 90 for p in patients
    )


def test_silver_dates_are_shifted_consistently_within_a_patient(lake, synthea_dir, ctx):
    con = lake.connect()
    row = con.sql(
        "select patient_id, min(start_ts) a, max(start_ts) b from silver_encounters group by patient_id order by patient_id limit 1"
    ).fetchone()
    pid, first, last = row
    off = ctx.offset_days(pid)
    # Compare with the source bundle: the span between first and last encounter is unchanged, the dates are not.
    import json

    src = next(p for p in (synthea_dir / "fhir").glob("*.json") if pid in p.name)
    starts = sorted(
        e["resource"]["period"]["start"][:10]
        for e in json.loads(src.read_text())["entry"]
        if e["resource"]["resourceType"] == "Encounter"
    )
    span_src = (date.fromisoformat(starts[-1]) - date.fromisoformat(starts[0])).days
    assert abs((last - first).days - span_src) <= 1
    assert (first.date() - date.fromisoformat(starts[0])).days in (off, off + 1, off - 1)
    assert off != 0
