"""Data quality checks on silver, in SQL. A failing critical check stops the pipeline; every result is kept."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from cfp.lake import Lake


@dataclass(frozen=True)
class Check:
    name: str
    table: str
    severity: str  # critical | warning
    failing_sql: str  # count of failing rows
    total_sql: str  # denominator
    description: str


def _ref_check(child: str, column: str, parent: str, parent_key: str, severity: str = "critical") -> Check:
    return Check(
        name=f"{child}.{column} resolves",
        table=child,
        severity=severity,
        failing_sql=(
            f"select count(*) from silver_{child} c left join silver_{parent} p on c.{column} = p.{parent_key} "
            f"where c.{column} is not null and p.{parent_key} is null"
        ),
        total_sql=f"select count(*) from silver_{child} where {column} is not null",
        description=f"every {child}.{column} points at an existing {parent} row",
    )


def _range_check(code: str, label: str, low: float, high: float) -> Check:
    return Check(
        name=f"{label} within {low:g}-{high:g}",
        table="observations",
        severity="warning",
        failing_sql=f"select count(*) from silver_observations where code = '{code}' and value_num is not null and (value_num < {low} or value_num > {high})",
        total_sql=f"select count(*) from silver_observations where code = '{code}' and value_num is not null",
        description=f"{label} (LOINC {code}) values are physiologically plausible",
    )


CHECKS: list[Check] = [
    Check(
        "patients.patient_id unique",
        "patients",
        "critical",
        "select count(*) - count(distinct patient_id) from silver_patients",
        "select count(*) from silver_patients",
        "one row per patient",
    ),
    Check(
        "patients.sex is a known value",
        "patients",
        "critical",
        "select count(*) from silver_patients where sex not in ('male','female','other','unknown') or sex is null",
        "select count(*) from silver_patients",
        "administrative sex is one of the FHIR codes",
    ),
    Check(
        "patients.age_years within 0-90",
        "patients",
        "critical",
        "select count(*) from silver_patients where age_years is null or age_years < 0 or age_years > 90",
        "select count(*) from silver_patients",
        "ages are present and capped at 90 by de-identification",
    ),
    Check(
        "patients have no birth year over 89",
        "patients",
        "critical",
        "select count(*) from silver_patients where age_years >= 90 and birth_year is not null",
        "select count(*) from silver_patients where age_years >= 90",
        "Safe Harbor: no birth year for anyone 90 or older",
    ),
    _ref_check("encounters", "patient_id", "patients", "patient_id"),
    _ref_check("conditions", "patient_id", "patients", "patient_id"),
    _ref_check("observations", "patient_id", "patients", "patient_id"),
    _ref_check("medication_requests", "patient_id", "patients", "patient_id"),
    _ref_check("conditions", "encounter_id", "encounters", "encounter_id", "warning"),
    _ref_check("observations", "encounter_id", "encounters", "encounter_id", "warning"),
    _ref_check("medication_requests", "encounter_id", "encounters", "encounter_id", "warning"),
    _ref_check("procedures", "encounter_id", "encounters", "encounter_id", "warning"),
    Check(
        "encounters end after they start",
        "encounters",
        "critical",
        "select count(*) from silver_encounters where end_ts < start_ts",
        "select count(*) from silver_encounters where end_ts is not null and start_ts is not null",
        "no negative lengths of stay",
    ),
    Check(
        "encounters have a start",
        "encounters",
        "critical",
        "select count(*) from silver_encounters where start_ts is null",
        "select count(*) from silver_encounters",
        "every encounter is dated",
    ),
    Check(
        "encounters.class_code is known",
        "encounters",
        "warning",
        "select count(*) from silver_encounters where class_code not in ('AMB','EMER','IMP','HH','VR','OBSENC','SS','ACUTE','NONAC','PRENC','FLD')",
        "select count(*) from silver_encounters",
        "ActCode encounter classes only",
    ),
    Check(
        "conditions have a code and a display",
        "conditions",
        "critical",
        "select count(*) from silver_conditions where code is null or display is null",
        "select count(*) from silver_conditions",
        "coded problems",
    ),
    Check(
        "conditions resolve after onset",
        "conditions",
        "warning",
        "select count(*) from silver_conditions where abatement_ts < onset_ts",
        "select count(*) from silver_conditions where abatement_ts is not null",
        "abatement never precedes onset",
    ),
    Check(
        "medication requests have a display",
        "medication_requests",
        "critical",
        "select count(*) from silver_medication_requests where display is null",
        "select count(*) from silver_medication_requests",
        "named medications",
    ),
    Check(
        "observations have a value",
        "observations",
        "warning",
        "select count(*) from silver_observations where value_num is null and value_text is null and component_of is null and observation_id not in (select component_of from silver_observations where component_of is not null)",
        "select count(*) from silver_observations where component_of is null",
        "every leaf observation carries a numeric or coded value",
    ),
    _range_check("39156-5", "BMI", 8, 100),
    _range_check("8480-6", "systolic blood pressure", 50, 260),
    _range_check("8462-4", "diastolic blood pressure", 20, 160),
    _range_check("8867-4", "heart rate", 20, 250),
    _range_check("4548-4", "HbA1c", 3, 20),
    Check(
        "no encounters after death",
        "encounters",
        "warning",
        "select count(*) from silver_encounters e join silver_patients p using (patient_id) where p.deceased_ts is not null and e.start_ts > p.deceased_ts + interval 1 day",
        "select count(*) from silver_encounters e join silver_patients p using (patient_id) where p.deceased_ts is not null",
        "deceased patients stop having encounters",
    ),
]

RESULT_SCHEMA = pa.schema(
    [
        ("check_name", pa.string()),
        ("table_name", pa.string()),
        ("severity", pa.string()),
        ("description", pa.string()),
        ("failing", pa.int64()),
        ("total", pa.int64()),
        ("failing_share", pa.float64()),
        ("passed", pa.bool_()),
        ("ran_at", pa.timestamp("us")),
    ]
)


@dataclass
class QualityResult:
    check_name: str
    table_name: str
    severity: str
    description: str
    failing: int
    total: int
    failing_share: float
    passed: bool
    ran_at: datetime


class QualityError(RuntimeError):
    pass


def run_checks(lake: Lake, checks: list[Check] | None = None) -> list[QualityResult]:
    con = lake.connect()
    ran_at = datetime.now(UTC).replace(tzinfo=None)
    results = []
    for check in checks or CHECKS:
        failing_row = con.sql(check.failing_sql).fetchone()
        total_row = con.sql(check.total_sql).fetchone()
        failing = int(failing_row[0] or 0) if failing_row else 0
        total = int(total_row[0] or 0) if total_row else 0
        results.append(
            QualityResult(
                check.name,
                check.table,
                check.severity,
                check.description,
                failing,
                total,
                round(failing / total, 6) if total else 0.0,
                failing == 0,
                ran_at,
            )
        )
    return results


def write_results(lake: Lake, results: list[QualityResult], report_path: Path | None) -> dict:
    lake.write_delta(
        "gold", "quality_results", pa.Table.from_pylist([asdict(r) for r in results], schema=RESULT_SCHEMA)
    )
    summary = {
        "ran_at": results[0].ran_at.isoformat() if results else None,
        "checks": len(results),
        "passed": sum(r.passed for r in results),
        "critical_failures": [r.check_name for r in results if not r.passed and r.severity == "critical"],
        "warnings": [r.check_name for r in results if not r.passed and r.severity == "warning"],
        "results": [{**asdict(r), "ran_at": r.ran_at.isoformat()} for r in results],
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def enforce(summary: dict) -> None:
    if summary["critical_failures"]:
        raise QualityError("critical data quality checks failed: " + ", ".join(summary["critical_failures"]))
