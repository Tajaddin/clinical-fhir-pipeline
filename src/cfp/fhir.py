"""Small helpers for FHIR R4 resources as plain dicts."""

from __future__ import annotations

from typing import Any

PATIENT_REF_KEYS = ("subject", "patient")


def ref_id(reference: str | None) -> str | None:
    """'urn:uuid:<id>' or 'Patient/<id>' -> '<id>'."""
    if not reference:
        return None
    if reference.startswith("urn:uuid:"):
        return reference[9:]
    return reference.rsplit("/", 1)[-1]


def patient_id_of(resource: dict[str, Any]) -> str | None:
    if resource.get("resourceType") == "Patient":
        return resource.get("id")
    for key in PATIENT_REF_KEYS:
        ref = resource.get(key)
        if isinstance(ref, dict) and ref.get("reference"):
            return ref_id(ref["reference"])
    return None


def coding(concept: dict[str, Any] | None, system_hint: str | None = None) -> tuple[str | None, str | None, str | None]:
    """(system, code, display) of the first coding, preferring one whose system contains system_hint."""
    if not concept:
        return None, None, None
    codings = concept.get("coding") or []
    chosen = None
    if system_hint:
        chosen = next((c for c in codings if system_hint in (c.get("system") or "")), None)
    chosen = chosen or (codings[0] if codings else None)
    if not chosen:
        return None, None, concept.get("text")
    return chosen.get("system"), chosen.get("code"), chosen.get("display") or concept.get("text")
