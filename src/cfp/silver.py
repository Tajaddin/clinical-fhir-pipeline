"""Silver: typed, de-identified Delta tables, one per clinical resource type, derived from bronze.

Extraction is plain Python per resource (testable on a dict); the lakehouse SQL starts in gold. Billing
resources (Claim, ExplanationOfBenefit), Provenance, SupplyDelivery, CareTeam, Device, ImagingStudy and
MedicationAdministration are out of scope and not extracted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import orjson
import pyarrow as pa
import pyarrow.dataset as ds

from cfp.deid import DeidContext, age_at, age_band, pseudonym, shift_ts, zip3
from cfp.fhir import coding, ref_id
from cfp.lake import Lake

TS = pa.timestamp("us")

SCHEMAS: dict[str, pa.Schema] = {
    "patients": pa.schema(
        [
            ("patient_id", pa.string()),
            ("pseudonym", pa.string()),
            ("sex", pa.string()),
            ("birth_year", pa.int32()),
            ("age_years", pa.int32()),
            ("age_band", pa.string()),
            ("race", pa.string()),
            ("ethnicity", pa.string()),
            ("marital_status", pa.string()),
            ("state", pa.string()),
            ("zip3", pa.string()),
            ("deceased", pa.bool_()),
            ("deceased_ts", TS),
        ]
    ),
    "encounters": pa.schema(
        [
            ("encounter_id", pa.string()),
            ("patient_id", pa.string()),
            ("class_code", pa.string()),
            ("status", pa.string()),
            ("type_code", pa.string()),
            ("type_display", pa.string()),
            ("start_ts", TS),
            ("end_ts", TS),
            ("length_hours", pa.float64()),
            ("reason_code", pa.string()),
            ("reason_display", pa.string()),
            ("organization_id", pa.string()),
        ]
    ),
    "conditions": pa.schema(
        [
            ("condition_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("category", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("clinical_status", pa.string()),
            ("verification_status", pa.string()),
            ("onset_ts", TS),
            ("abatement_ts", TS),
            ("recorded_ts", TS),
        ]
    ),
    "observations": pa.schema(
        [
            ("observation_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("category", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("effective_ts", TS),
            ("value_num", pa.float64()),
            ("unit", pa.string()),
            ("value_text", pa.string()),
            ("component_of", pa.string()),
        ]
    ),
    "medication_requests": pa.schema(
        [
            ("medication_request_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("status", pa.string()),
            ("intent", pa.string()),
            ("authored_ts", TS),
            ("reason_display", pa.string()),
        ]
    ),
    "procedures": pa.schema(
        [
            ("procedure_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("status", pa.string()),
            ("start_ts", TS),
            ("end_ts", TS),
            ("reason_display", pa.string()),
        ]
    ),
    "immunizations": pa.schema(
        [
            ("immunization_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("status", pa.string()),
            ("occurrence_ts", TS),
        ]
    ),
    "allergies": pa.schema(
        [
            ("allergy_id", pa.string()),
            ("patient_id", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("category", pa.string()),
            ("criticality", pa.string()),
            ("clinical_status", pa.string()),
            ("recorded_ts", TS),
            ("reactions", pa.string()),
        ]
    ),
    "diagnostic_reports": pa.schema(
        [
            ("report_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("category", pa.string()),
            ("code", pa.string()),
            ("display", pa.string()),
            ("effective_ts", TS),
            ("result_count", pa.int32()),
        ]
    ),
    "careplans": pa.schema(
        [
            ("careplan_id", pa.string()),
            ("patient_id", pa.string()),
            ("encounter_id", pa.string()),
            ("category_code", pa.string()),
            ("category_display", pa.string()),
            ("status", pa.string()),
            ("start_ts", TS),
            ("end_ts", TS),
            ("addresses_display", pa.string()),
        ]
    ),
}

RESOURCE_TABLE: dict[str, str] = {
    "Patient": "patients",
    "Encounter": "encounters",
    "Condition": "conditions",
    "Observation": "observations",
    "MedicationRequest": "medication_requests",
    "Procedure": "procedures",
    "Immunization": "immunizations",
    "AllergyIntolerance": "allergies",
    "DiagnosticReport": "diagnostic_reports",
    "CarePlan": "careplans",
}


# ---- per-resource extraction -----------------------------------------------------------------------------
def _first_code(concepts: list[dict] | None) -> tuple[str | None, str | None]:
    if not concepts:
        return None, None
    _, code, display = coding(concepts[0])
    return code, display


def _ext_text(resource: dict, url_suffix: str) -> str | None:
    for ext in resource.get("extension") or []:
        if (ext.get("url") or "").endswith(url_suffix):
            for inner in ext.get("extension") or []:
                if inner.get("url") == "text":
                    return inner.get("valueString")
            return ext.get("valueString")
    return None


def _hours(start, end) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 3600.0, 3)


def extract_patient(r: dict, ctx: DeidContext) -> list[dict]:
    pid = r["id"]
    off = ctx.offset_days(pid)
    birth = r.get("birthDate")
    age = age_at(birth, ctx.reference_date) if birth else None
    over_89 = age is not None and age > 89
    address = (r.get("address") or [{}])[0]
    marital = r.get("maritalStatus") or {}
    return [
        {
            "patient_id": pid,
            "pseudonym": pseudonym(pid, ctx.salt),
            "sex": r.get("gender"),
            "birth_year": None if (over_89 or not birth) else int(birth[:4]),
            "age_years": 90 if over_89 else age,
            "age_band": age_band(age),
            "race": _ext_text(r, "us-core-race"),
            "ethnicity": _ext_text(r, "us-core-ethnicity"),
            "marital_status": marital.get("text") or coding(marital)[2],
            "state": address.get("state"),
            "zip3": zip3(address.get("postalCode")),
            "deceased": bool(r.get("deceasedDateTime") or r.get("deceasedBoolean")),
            "deceased_ts": shift_ts(r.get("deceasedDateTime"), off),
        }
    ]


def extract_encounter(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    period = r.get("period") or {}
    start, end = shift_ts(period.get("start"), off), shift_ts(period.get("end"), off)
    type_code, type_display = _first_code(r.get("type"))
    reason_code, reason_display = _first_code(r.get("reasonCode"))
    org = (r.get("serviceProvider") or {}).get("reference")
    return [
        {
            "encounter_id": r["id"],
            "patient_id": pid,
            "class_code": (r.get("class") or {}).get("code"),
            "status": r.get("status"),
            "type_code": type_code,
            "type_display": type_display,
            "start_ts": start,
            "end_ts": end,
            "length_hours": _hours(start, end),
            "reason_code": reason_code,
            "reason_display": reason_display,
            "organization_id": org.split("|")[-1] if org else None,
        }
    ]


def extract_condition(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    _, code, display = coding(r.get("code"))
    return [
        {
            "condition_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "category": _first_code(r.get("category"))[0],
            "code": code,
            "display": display,
            "clinical_status": coding(r.get("clinicalStatus"))[1],
            "verification_status": coding(r.get("verificationStatus"))[1],
            "onset_ts": shift_ts(r.get("onsetDateTime"), off),
            "abatement_ts": shift_ts(r.get("abatementDateTime"), off),
            "recorded_ts": shift_ts(r.get("recordedDate"), off),
        }
    ]


def _value(obs: dict) -> tuple[float | None, str | None, str | None]:
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        return (float(q["value"]) if q.get("value") is not None else None), q.get("unit") or q.get("code"), None
    if "valueCodeableConcept" in obs:
        return None, None, coding(obs["valueCodeableConcept"])[2]
    if "valueString" in obs:
        return None, None, obs["valueString"]
    if "valueBoolean" in obs:
        return None, None, str(obs["valueBoolean"]).lower()
    if "valueInteger" in obs:
        return float(obs["valueInteger"]), None, None
    return None, None, None


def extract_observation(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    effective = shift_ts(r.get("effectiveDateTime") or r.get("issued"), off)
    encounter_id = ref_id((r.get("encounter") or {}).get("reference"))
    category = _first_code(r.get("category"))[0]
    rows = []
    _, code, display = coding(r.get("code"))
    value_num, unit, value_text = _value(r)
    components = r.get("component") or []
    rows.append(
        {
            "observation_id": r["id"],
            "patient_id": pid,
            "encounter_id": encounter_id,
            "category": category,
            "code": code,
            "display": display,
            "effective_ts": effective,
            "value_num": value_num,
            "unit": unit,
            "value_text": value_text,
            "component_of": None,
        }
    )
    for i, comp in enumerate(components):
        _, ccode, cdisplay = coding(comp.get("code"))
        cnum, cunit, ctext = _value(comp)
        rows.append(
            {
                "observation_id": f"{r['id']}#{i}",
                "patient_id": pid,
                "encounter_id": encounter_id,
                "category": category,
                "code": ccode,
                "display": cdisplay,
                "effective_ts": effective,
                "value_num": cnum,
                "unit": cunit,
                "value_text": ctext,
                "component_of": r["id"],
            }
        )
    return rows


def _reason(r: dict) -> str | None:
    refs = r.get("reasonReference") or []
    if refs and refs[0].get("display"):
        return refs[0]["display"]
    return _first_code(r.get("reasonCode"))[1]


def extract_medication_request(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    _, code, display = coding(r.get("medicationCodeableConcept"))
    return [
        {
            "medication_request_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "code": code,
            "display": display,
            "status": r.get("status"),
            "intent": r.get("intent"),
            "authored_ts": shift_ts(r.get("authoredOn"), off),
            "reason_display": _reason(r),
        }
    ]


def extract_procedure(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    period = r.get("performedPeriod") or {}
    start = shift_ts(period.get("start") or r.get("performedDateTime"), off)
    _, code, display = coding(r.get("code"))
    return [
        {
            "procedure_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "code": code,
            "display": display,
            "status": r.get("status"),
            "start_ts": start,
            "end_ts": shift_ts(period.get("end"), off),
            "reason_display": _reason(r),
        }
    ]


def extract_immunization(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    _, code, display = coding(r.get("vaccineCode"))
    return [
        {
            "immunization_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "code": code,
            "display": display,
            "status": r.get("status"),
            "occurrence_ts": shift_ts(r.get("occurrenceDateTime"), off),
        }
    ]


def extract_allergy(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    _, code, display = coding(r.get("code"))
    reactions = []
    for reaction in r.get("reaction") or []:
        for m in reaction.get("manifestation") or []:
            text = coding(m)[2]
            if text:
                reactions.append(text + (f" ({reaction['severity']})" if reaction.get("severity") else ""))
    categories = r.get("category") or []
    return [
        {
            "allergy_id": r["id"],
            "patient_id": pid,
            "code": code,
            "display": display,
            "category": categories[0] if categories else None,
            "criticality": r.get("criticality"),
            "clinical_status": coding(r.get("clinicalStatus"))[1],
            "recorded_ts": shift_ts(r.get("recordedDate"), off),
            "reactions": "; ".join(reactions) or None,
        }
    ]


def extract_diagnostic_report(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    _, code, display = coding(r.get("code"))
    return [
        {
            "report_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "category": _first_code(r.get("category"))[0],
            "code": code,
            "display": display,
            "effective_ts": shift_ts(r.get("effectiveDateTime") or r.get("issued"), off),
            "result_count": len(r.get("result") or []),
        }
    ]


def extract_careplan(r: dict, ctx: DeidContext, pid: str) -> list[dict]:
    off = ctx.offset_days(pid)
    period = r.get("period") or {}
    cat_code, cat_display = _first_code(r.get("category"))
    addresses = [a.get("display") for a in r.get("addresses") or [] if a.get("display")]
    return [
        {
            "careplan_id": r["id"],
            "patient_id": pid,
            "encounter_id": ref_id((r.get("encounter") or {}).get("reference")),
            "category_code": cat_code,
            "category_display": cat_display,
            "status": r.get("status"),
            "start_ts": shift_ts(period.get("start"), off),
            "end_ts": shift_ts(period.get("end"), off),
            "addresses_display": "; ".join(addresses) or None,
        }
    ]


EXTRACTORS: dict[str, Callable[..., list[dict]]] = {
    "Encounter": extract_encounter,
    "Condition": extract_condition,
    "Observation": extract_observation,
    "MedicationRequest": extract_medication_request,
    "Procedure": extract_procedure,
    "Immunization": extract_immunization,
    "AllergyIntolerance": extract_allergy,
    "DiagnosticReport": extract_diagnostic_report,
    "CarePlan": extract_careplan,
}


# ---- the step ----------------------------------------------------------------------------------------------
@dataclass
class SilverStats:
    rows: dict[str, int] = field(default_factory=dict)
    skipped_without_patient: int = 0


def _bronze_rows(lake: Lake, resource_type: str, batch_size: int) -> Iterator[pa.RecordBatch]:
    dataset = lake.parquet_dataset("bronze", "resources")
    if "resource_type" not in dataset.schema.names:
        return
    scanner = dataset.scanner(
        filter=ds.field("resource_type") == resource_type,
        columns=["resource_id", "patient_id", "json"],
        batch_size=batch_size,
    )
    yield from scanner.to_batches()


class _DeltaAppender:
    """Writes a table in chunks: the first chunk overwrites, the rest append; an empty table is still created."""

    def __init__(self, lake: Lake, table: str, schema: pa.Schema) -> None:
        self.lake, self.table, self.schema = lake, table, schema
        self.rows: list[dict] = []
        self.written = 0

    def add(self, rows: list[dict], flush_at: int) -> None:
        self.rows.extend(rows)
        if len(self.rows) >= flush_at:
            self.flush()

    def flush(self) -> None:
        if self.rows or self.written == 0:
            mode: Literal["overwrite", "append"] = "overwrite" if self.written == 0 else "append"
            self.lake.write_delta("silver", self.table, pa.Table.from_pylist(self.rows, schema=self.schema), mode=mode)
            self.written += len(self.rows)
            self.rows = []


def build_silver(lake: Lake, ctx: DeidContext, batch_size: int = 20_000) -> SilverStats:
    stats = SilverStats()
    for resource_type, table in RESOURCE_TABLE.items():
        out = _DeltaAppender(lake, table, SCHEMAS[table])
        for batch in _bronze_rows(lake, resource_type, batch_size):
            rows: list[dict] = []
            for pid, raw in zip(batch.column("patient_id").to_pylist(), batch.column("json").to_pylist(), strict=True):
                resource = orjson.loads(raw)
                if resource_type == "Patient":
                    rows.extend(extract_patient(resource, ctx))
                elif pid is None:
                    stats.skipped_without_patient += 1
                else:
                    rows.extend(EXTRACTORS[resource_type](resource, ctx, pid))
            out.add(rows, batch_size)
        out.flush()
        stats.rows[table] = out.written
    return stats


def context_from_settings(salt: str, reference_date: str, max_shift_days: int) -> DeidContext:
    return DeidContext(salt=salt, reference_date=date.fromisoformat(reference_date), max_shift_days=max_shift_days)
