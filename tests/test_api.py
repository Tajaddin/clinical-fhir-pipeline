from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cfp.config import reset_settings_cache
from fhir_factory import PHONE, SSN, STREET, SURNAME


@pytest.fixture(scope="module")
def client(trained, tmp_path_factory):
    lake, _metrics, reports = trained
    db = tmp_path_factory.mktemp("db") / "cfp.db"
    env = {
        "CFP_LAKEHOUSE_ROOT": lake.root,
        "CFP_REPORTS_DIR": str(reports),
        "CFP_DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "CFP_API_KEYS": "analyst-key:analyst,admin-key:admin",
        "CFP_RATE_LIMIT": "6/minute",
        "CFP_LLM_BASE_URL": "",
        "CFP_EMBED_MODEL": "",
    }
    import os

    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    reset_settings_cache()
    from cfp.serving.app import create_app

    with TestClient(create_app()) as c:
        yield c
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    reset_settings_cache()


ANALYST = {"X-API-Key": "analyst-key", "X-Purpose": "treatment"}
ADMIN = {"X-API-Key": "admin-key"}


def test_health_and_metrics_are_open(client):
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.json()["risk_scores"] is True
    assert h.json()["model_version"] == "readmission-v1"
    assert "http_request_duration_seconds" in client.get("/metrics").text
    assert "X-Request-ID" in h.headers


def test_patient_routes_need_a_key_and_cohort_routes_too(client):
    assert client.get("/patients").status_code == 401
    assert client.get("/cohort").status_code == 401
    assert client.get("/patients", headers={"X-API-Key": "nope"}).status_code == 401


def test_cohort_quality_and_model_card(client):
    c = client.get("/cohort", headers=ANALYST).json()
    assert c["patients"] == 160 and c["admissions"]["total"] > 100 and c["model"]["champion"]
    assert c["quality"]["critical"]["passed"] == c["quality"]["critical"]["checks"]
    q = client.get("/quality", headers=ANALYST).json()
    assert len(q) >= 20 and all("passed" in r for r in q)
    card = client.get("/model/card", headers=ANALYST).json()
    assert "fairness" in card and "sex" in card["fairness"] and card["features"]


def test_patient_list_detail_risk_facts_without_identifiers(client):
    page = client.get("/patients", params={"limit": 5}, headers=ANALYST).json()
    assert page["total"] == 160 and len(page["items"]) == 5
    top = page["items"][0]
    assert top["risk"] is not None and top["pseudonym"].startswith("P-")
    pid = top["patient_id"]
    detail = client.get(f"/patients/{pid}", headers=ANALYST)
    assert detail.status_code == 200
    body = detail.text
    for needle in (SURNAME, SSN, STREET, PHONE, "birthDate", "Given"):
        assert needle not in body
    d = detail.json()
    assert d["age_band"] and "admissions" in d and "encounters" in d and "active_problems" in d
    risk = client.get(f"/patients/{pid}/risk", headers=ANALYST).json()
    assert 0 <= risk["risk"] <= 1 and risk["risk_band"] in {"low", "medium", "high"} and len(risk["top_factors"]) == 3
    facts = client.get(f"/patients/{pid}/facts", params={"section": "problems"}, headers=ANALYST).json()
    assert facts["count"] >= 1 and all(f["section"] == "problems" for f in facts["items"])
    assert client.get("/patients/00000000-0000-4000-8000-00000000dead", headers=ANALYST).status_code == 404
    assert client.get("/patients/not%20valid%3Bid", headers=ANALYST).status_code == 422


def test_summary_is_generated_checked_stored_and_cached(client):
    pid = client.get("/patients", params={"limit": 1}, headers=ANALYST).json()["items"][0]["patient_id"]
    first = client.get(f"/patients/{pid}/summary", headers=ANALYST).json()
    assert first["cached"] is False and first["sections"] and first["faithfulness"]["faithfulness_rate"] == 1.0
    claims = [c for s in first["sections"] for c in s["claims"]]
    assert all(c["citations"] for c in claims) and set(first["facts_shown"]) >= {
        c for cl in claims for c in cl["citations"]
    }
    second = client.get(f"/patients/{pid}/summary", headers=ANALYST).json()
    assert second["cached"] is True and second["sections"] == first["sections"]
    third = client.get(f"/patients/{pid}/summary", params={"refresh": "true"}, headers=ANALYST).json()
    assert third["cached"] is False
    text = client.get("/metrics").text
    assert "summaries_generated_total" in text and 'summary_faithfulness_rate{source="live"} 1.0' in text


def test_audit_log_records_every_patient_access_including_denied_ones(client):
    assert client.get("/audit", headers=ANALYST).status_code == 403
    rows = client.get("/audit", headers=ADMIN).json()["items"]
    actions = {r["action"] for r in rows}
    assert "GET /patients" in actions and "GET /patients/{patient_id}/summary" in actions and "GET /audit" in actions
    denied = [r for r in rows if r["status_code"] in (401, 403)]
    assert denied and any(r["actor"] == "anonymous" for r in denied)
    treated = [r for r in rows if r["purpose"] == "treatment"]
    assert treated and all(r["actor"].startswith("key-") and "analyst-key" not in r["actor"] for r in treated)
    pid = next(r["patient_id"] for r in rows if r["patient_id"])
    scoped = client.get("/audit", params={"patient_id": pid}, headers=ADMIN).json()["items"]
    assert scoped and all(r["patient_id"] == pid for r in scoped)


def test_rate_limit_is_per_key_and_route(client):
    pid = client.get("/patients", params={"limit": 1}, headers=ANALYST).json()["items"][0]["patient_id"]
    codes = [client.get(f"/patients/{pid}/facts", headers=ANALYST).status_code for _ in range(8)]
    first_429 = codes.index(429)
    # an earlier test already spent one call of this key's budget on the facts route
    assert 4 <= first_429 <= 6 and set(codes[:first_429]) == {200} and set(codes[first_429:]) == {429}
    assert client.get(f"/patients/{pid}/facts", headers=ADMIN).status_code == 200
    import re

    rejected = re.search(r"^rate_limit_rejections_total (\d+)", client.get("/metrics").text, re.M)
    assert rejected and int(rejected.group(1)) >= 1
