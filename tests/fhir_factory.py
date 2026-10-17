"""Small synthetic FHIR R4 transaction bundles in Synthea's shape, with identifiers that de-identification
must remove and inpatient admissions that produce known readmission labels."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

SURNAME = "Fakeson"
SSN = "999-12-3456"
STREET = "1 Imaginary Street"
PHONE = "555-010-9999"


def _ref(rid: str) -> dict:
    return {"reference": f"urn:uuid:{rid}"}


def _concept(system: str, code: str, display: str) -> dict:
    return {"coding": [{"system": system, "code": code, "display": display}], "text": display}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S-04:00")


def make_bundle(i: int, rng: random.Random, readmit: bool, inpatient: bool = True) -> dict:
    pid = f"{i:08x}-0000-4000-8000-000000000000"
    birth_year = rng.choice([1940, 1955, 1968, 1980, 1995, 2010, 1930])
    sex = rng.choice(["male", "female"])
    patient = {
        "resourceType": "Patient",
        "id": pid,
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "extension": [
                    {"url": "text", "valueString": rng.choice(["White", "Black or African American", "Asian"])}
                ],
            },
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
                "extension": [
                    {"url": "text", "valueString": rng.choice(["Hispanic or Latino", "Not Hispanic or Latino"])}
                ],
            },
        ],
        "identifier": [
            {"system": "http://hl7.org/fhir/sid/us-ssn", "value": SSN},
            {"system": "urn:oid:2.16.840.1.113883.4.3.25", "value": f"S999{i:05d}"},
        ],
        "name": [{"use": "official", "family": f"{SURNAME}{i}", "given": [f"Given{i}"], "prefix": ["Mr."]}],
        "telecom": [{"system": "phone", "value": PHONE, "use": "home"}],
        "gender": sex,
        "birthDate": f"{birth_year}-06-15",
        "address": [{"line": [STREET], "city": "Tampa", "state": "FL", "postalCode": "33602", "country": "US"}],
        "maritalStatus": {
            "coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": "M", "display": "Married"}
            ],
            "text": "M",
        },
    }
    if i % 7 == 0:
        patient["deceasedDateTime"] = _iso(datetime(2024, 3, 1, 8))
    entries = [patient]
    n = 0

    def rid() -> str:
        nonlocal n
        n += 1
        return f"{i:08x}-{n:04x}-4000-8000-000000000000"

    def encounter(
        start: datetime, hours: float, cls: str, type_code: str, type_display: str, reason: str | None = None
    ) -> dict:
        enc = {
            "resourceType": "Encounter",
            "id": rid(),
            "status": "finished",
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls},
            "type": [_concept("http://snomed.info/sct", type_code, type_display)],
            "subject": {**_ref(pid), "display": f"Mr. Given{i} {SURNAME}{i}"},
            "period": {"start": _iso(start), "end": _iso(start + timedelta(hours=hours))},
            "serviceProvider": {
                "reference": "Organization?identifier=https://github.com/synthetichealth/synthea|org-1",
                "display": "TEST HOSPITAL",
            },
        }
        if reason:
            enc["reasonCode"] = [_concept("http://snomed.info/sct", "88805009", reason)]
        entries.append(enc)
        return enc

    def condition(enc: dict, code: str, display: str, when: datetime, abated: datetime | None = None) -> None:
        c = {
            "resourceType": "Condition",
            "id": rid(),
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": "resolved" if abated else "active",
                    }
                ]
            },
            "verificationStatus": {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}
                ]
            },
            "category": [
                _concept(
                    "http://terminology.hl7.org/CodeSystem/condition-category",
                    "encounter-diagnosis",
                    "Encounter Diagnosis",
                )
            ],
            "code": _concept("http://snomed.info/sct", code, display),
            "subject": _ref(pid),
            "encounter": _ref(enc["id"]),
            "onsetDateTime": _iso(when),
            "recordedDate": _iso(when),
        }
        if abated:
            c["abatementDateTime"] = _iso(abated)
        entries.append(c)

    def observation(
        enc: dict, when: datetime, code: str, display: str, value: float, unit: str, category: str = "vital-signs"
    ) -> None:
        entries.append(
            {
                "resourceType": "Observation",
                "id": rid(),
                "status": "final",
                "category": [
                    _concept("http://terminology.hl7.org/CodeSystem/observation-category", category, category)
                ],
                "code": _concept("http://loinc.org", code, display),
                "subject": _ref(pid),
                "encounter": _ref(enc["id"]),
                "effectiveDateTime": _iso(when),
                "issued": _iso(when),
                "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
            }
        )

    def blood_pressure(enc: dict, when: datetime, systolic: float, diastolic: float) -> None:
        entries.append(
            {
                "resourceType": "Observation",
                "id": rid(),
                "status": "final",
                "category": [
                    _concept("http://terminology.hl7.org/CodeSystem/observation-category", "vital-signs", "Vital signs")
                ],
                "code": _concept("http://loinc.org", "85354-9", "Blood pressure panel with all children optional"),
                "subject": _ref(pid),
                "encounter": _ref(enc["id"]),
                "effectiveDateTime": _iso(when),
                "component": [
                    {
                        "code": _concept("http://loinc.org", "8462-4", "Diastolic Blood Pressure"),
                        "valueQuantity": {"value": diastolic, "unit": "mm[Hg]"},
                    },
                    {
                        "code": _concept("http://loinc.org", "8480-6", "Systolic Blood Pressure"),
                        "valueQuantity": {"value": systolic, "unit": "mm[Hg]"},
                    },
                ],
            }
        )

    def medication(enc: dict, when: datetime, code: str, display: str, reason: str) -> None:
        entries.append(
            {
                "resourceType": "MedicationRequest",
                "id": rid(),
                "status": "active",
                "intent": "order",
                "medicationCodeableConcept": _concept("http://www.nlm.nih.gov/research/umls/rxnorm", code, display),
                "subject": _ref(pid),
                "encounter": _ref(enc["id"]),
                "authoredOn": _iso(when),
                "requester": {
                    "reference": "Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|9999999999",
                    "display": "Dr. Fake Doctor",
                },
                "reasonReference": [{"reference": "urn:uuid:none", "display": reason}],
            }
        )

    base = datetime(2016, 1, 10, 9) + timedelta(days=rng.randint(0, 2000))
    chronic = rng.random() < 0.6
    visits = []
    for k in range(4):
        start = base + timedelta(days=200 * k + rng.randint(0, 30))
        enc = encounter(start, 0.5, "AMB", "162673000", "General examination of patient (procedure)")
        visits.append((enc, start))
        observation(enc, start, "39156-5", "Body mass index (BMI) [Ratio]", 22 + rng.random() * 15, "kg/m2")
        blood_pressure(enc, start, 110 + rng.random() * 50, 70 + rng.random() * 25)
        observation(
            enc, start, "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", 5 + rng.random() * 4, "%", "laboratory"
        )
        if k == 0 and chronic:
            condition(enc, "44054006", "Diabetes mellitus type 2 (disorder)", start)
            condition(enc, "38341003", "Hypertensive disorder (disorder)", start)
            medication(
                enc,
                start,
                "860975",
                "24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet",
                "Diabetes mellitus type 2 (disorder)",
            )
        if k == 1:
            condition(enc, "195662009", "Acute viral pharyngitis (disorder)", start, start + timedelta(days=10))
    if inpatient:
        admit = visits[2][1] + timedelta(days=40)
        stay = encounter(admit, 72, "IMP", "32485007", "Hospital admission (procedure)", "Pneumonia (disorder)")
        condition(stay, "233604007", "Pneumonia (disorder)", admit, admit + timedelta(days=20))
        medication(stay, admit, "309362", "Amoxicillin 500 MG Oral Tablet", "Pneumonia (disorder)")
        entries.append(
            {
                "resourceType": "Procedure",
                "id": rid(),
                "status": "completed",
                "code": _concept("http://snomed.info/sct", "399208008", "Plain chest X-ray (procedure)"),
                "subject": _ref(pid),
                "encounter": _ref(stay["id"]),
                "performedPeriod": {"start": _iso(admit + timedelta(hours=1)), "end": _iso(admit + timedelta(hours=2))},
                "reasonReference": [{"reference": "urn:uuid:none", "display": "Pneumonia (disorder)"}],
            }
        )
        if readmit:
            back = admit + timedelta(days=72 / 24 + rng.randint(2, 25))
            encounter(back, 48, "IMP", "32485007", "Hospital admission (procedure)", "Pneumonia (disorder)")
        elif rng.random() < 0.5:
            later = admit + timedelta(days=90 + rng.randint(0, 60))
            encounter(later, 24, "IMP", "32485007", "Hospital admission (procedure)")
    entries.append(
        {
            "resourceType": "Immunization",
            "id": rid(),
            "status": "completed",
            "vaccineCode": _concept(
                "http://hl7.org/fhir/sid/cvx", "140", "Influenza, seasonal, injectable, preservative free"
            ),
            "patient": _ref(pid),
            "encounter": _ref(visits[0][0]["id"]),
            "occurrenceDateTime": _iso(visits[0][1]),
            "primarySource": True,
        }
    )
    if i % 3 == 0:
        entries.append(
            {
                "resourceType": "AllergyIntolerance",
                "id": rid(),
                "clinicalStatus": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                            "code": "active",
                        }
                    ]
                },
                "type": "allergy",
                "category": ["medication"],
                "criticality": "high",
                "code": _concept("http://www.nlm.nih.gov/research/umls/rxnorm", "7980", "Penicillin G"),
                "patient": _ref(pid),
                "recordedDate": _iso(visits[0][1] - timedelta(days=3000)),
                "reaction": [
                    {
                        "manifestation": [
                            _concept("http://snomed.info/sct", "271807003", "Eruption of skin (disorder)")
                        ],
                        "severity": "moderate",
                    }
                ],
            }
        )
    entries.append(
        {
            "resourceType": "DiagnosticReport",
            "id": rid(),
            "status": "final",
            "category": [_concept("http://terminology.hl7.org/CodeSystem/v2-0074", "LAB", "Laboratory")],
            "code": _concept("http://loinc.org", "57698-3", "Lipid panel with direct LDL - Serum or Plasma"),
            "subject": _ref(pid),
            "encounter": _ref(visits[1][0]["id"]),
            "effectiveDateTime": _iso(visits[1][1]),
            "result": [
                {"reference": "urn:uuid:none", "display": "Cholesterol"},
                {"reference": "urn:uuid:none", "display": "Triglyceride"},
            ],
            "presentedForm": [{"contentType": "text/plain", "data": "SGVsbG8gRmFrZXNvbg=="}],
        }
    )
    if chronic:
        entries.append(
            {
                "resourceType": "CarePlan",
                "id": rid(),
                "status": "active",
                "intent": "order",
                "category": [
                    _concept("http://snomed.info/sct", "698360004", "Diabetes self management plan (record artifact)")
                ],
                "subject": _ref(pid),
                "encounter": _ref(visits[0][0]["id"]),
                "period": {"start": _iso(visits[0][1])},
                "addresses": [{"reference": "urn:uuid:none", "display": "Diabetes mellitus type 2 (disorder)"}],
            }
        )
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"fullUrl": f"urn:uuid:{e['id']}", "resource": e, "request": {"method": "POST", "url": e["resourceType"]}}
            for e in entries
        ],
    }


def make_cohort(n_patients: int = 30, seed: int = 7, readmit_fraction: float = 0.3) -> list[dict]:
    rng = random.Random(seed)
    return [make_bundle(i, rng, readmit=(i % round(1 / readmit_fraction) == 1)) for i in range(n_patients)]


def write_bundles(target: Path, bundles: list[dict]) -> Path:
    fhir = Path(target) / "fhir"
    fhir.mkdir(parents=True, exist_ok=True)
    for b in bundles:
        patient = b["entry"][0]["resource"]
        name = f"{patient['name'][0]['given'][0]}_{patient['name'][0]['family']}_{patient['id']}.json"
        (fhir / name).write_text(json.dumps(b), encoding="utf-8")
    return Path(target)
