"""FastAPI: de-identified cohort and patient views, readmission risk, cited summaries, and the audit log.

Every request that touches patient data (anything under /patients, plus /audit) writes an audit row before
the response leaves the process; if the audit store is down the request fails rather than going unlogged.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from cfp import __version__
from cfp.config import Settings, settings
from cfp.db import current_summary, init_db, list_audit, make_engine, save_summary, write_audit
from cfp.faithfulness import judge_summary
from cfp.lake import Lake
from cfp.logging_setup import configure_logging, get_logger
from cfp.summaries import load_facts, summarize
from cfp.wiring import make_llm, make_retriever

REQUESTS = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
PATIENT_READS = Counter("patient_reads_total", "Reads of patient-level data", ["route"])
SUMMARIES = Counter("summaries_generated_total", "Summaries produced by the model", ["model"])
SUMMARY_LATENCY = Histogram(
    "summary_generation_seconds", "LLM summary latency", buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160)
)
SUMMARY_FAITHFULNESS = Gauge("summary_faithfulness_rate", "Supported claims / all claims, last measured", ["source"])
LLM_FAILURES = Counter("llm_failures_total", "Summary generations that raised")
AUDIT_FAILURES = Counter("audit_write_failures_total", "Audit rows that could not be written")
AUDIT_ROWS = Counter("audit_rows_total", "Audit rows written", ["action"])
RATE_LIMITED = Counter("rate_limit_rejections_total", "Requests answered 429")
AUTH_DENIED = Counter("auth_denied_total", "Requests answered 401 or 403")
MODEL_INFO = Gauge("model_info", "Loaded readmission model", ["version", "champion"])

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
log = get_logger("api")


def _key_or_ip(request: Request) -> str:
    return request.headers.get("x-api-key") or get_remote_address(request)


def _actor(key: str | None) -> str:
    return "anonymous" if not key else "key-" + hashlib.sha256(key.encode()).hexdigest()[:10]


class State:
    """What the API holds between requests."""

    def __init__(self, s: Settings) -> None:
        self.settings = s
        self.lake = Lake(s.lakehouse_root)
        self.engine = make_engine(s.database_url)
        init_db(self.engine)
        self.llm = make_llm(s)
        self.retriever = make_retriever(s)
        self.has_scores = self.lake.delta_exists("gold", "risk_scores")
        self.has_admissions = self.lake.delta_exists("gold", "admissions")
        self.model_metrics = self._json(s.reports_dir / "model-metrics.json")
        self.faithfulness = self._json(s.reports_dir / "summary-faithfulness.json")
        self.cohort = self._cohort()
        if self.model_metrics:
            MODEL_INFO.labels(
                self.model_metrics.get("model_version", "?"), self.model_metrics.get("champion", "?")
            ).set(1)
        if self.faithfulness and self.faithfulness.get("faithfulness_rate") is not None:
            SUMMARY_FAITHFULNESS.labels("offline_eval").set(self.faithfulness["faithfulness_rate"])

    @staticmethod
    def _json(path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def con(self):
        tables = [
            ("silver", t) for t in ("patients", "encounters", "conditions", "medication_requests", "allergies")
        ] + [("gold", t) for t in ("admissions", "risk_scores", "patient_facts", "quality_results")]
        return self.lake.connect(tables)

    def _cohort(self) -> dict[str, Any]:
        con = self.con()
        out: dict[str, Any] = {}
        out["patients"] = con.sql("select count(*) from silver_patients").fetchone()[0]
        out["by_sex"] = dict(
            con.sql("select coalesce(sex,'unknown'), count(*) from silver_patients group by 1 order by 1").fetchall()
        )
        out["by_age_band"] = dict(
            con.sql(
                "select coalesce(age_band,'unknown'), count(*) from silver_patients group by 1 order by 1"
            ).fetchall()
        )
        out["deceased"] = con.sql("select count(*) from silver_patients where deceased").fetchone()[0]
        out["encounters"] = con.sql("select count(*) from silver_encounters").fetchone()[0]
        if self.has_admissions:
            row = con.sql(
                "select count(*), count(*) filter (where in_modeling_cohort), count(*) filter (where in_modeling_cohort and readmitted), "
                "round(avg(los_days), 2) from gold_admissions"
            ).fetchone()
            out["admissions"] = {
                "total": row[0],
                "modeled": row[1],
                "readmitted": row[2],
                "readmission_rate": round(row[2] / row[1], 4) if row[1] else None,
                "mean_los_days": row[3],
            }
            out["readmission_by_age_band"] = [
                {"age_band": r[0], "admissions": r[1], "rate": round(r[2], 4)}
                for r in con.sql(
                    "select age_band, count(*), avg(readmitted::int) from gold_admissions where in_modeling_cohort group by 1 order by 1"
                ).fetchall()
            ]
        if self.lake.delta_exists("gold", "quality_results"):
            rows = con.sql(
                "select severity, count(*), count(*) filter (where passed) from gold_quality_results group by 1"
            ).fetchall()
            out["quality"] = {r[0]: {"checks": r[1], "passed": r[2]} for r in rows}
        if self.model_metrics:
            champ = self.model_metrics["champion"]
            test = self.model_metrics["models"][champ]["test"]
            out["model"] = {
                "version": self.model_metrics["model_version"],
                "champion": champ,
                "auroc": test.get("auroc"),
                "auroc_ci95": test.get("auroc_ci95"),
                "auprc": test.get("auprc"),
                "brier": test.get("brier"),
                "ece": test.get("ece"),
                "test_admissions": test.get("n"),
            }
        if self.faithfulness:
            out["summaries"] = {
                k: self.faithfulness.get(k) for k in ("model", "patients", "claims", "faithfulness_rate", "breakdown")
            }
        return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    app.state.api = State(s)
    log.info("api ready", patients=app.state.api.cohort.get("patients"), scores=app.state.api.has_scores)
    yield


def create_app() -> FastAPI:
    s = settings()
    configure_logging(s.log_level, s.service_name)
    app = FastAPI(title="Clinical FHIR pipeline", version=__version__, lifespan=lifespan, docs_url="/docs")
    # One limiter per app: slowapi accumulates limits per endpoint name across factory calls otherwise.
    limiter = Limiter(key_func=_key_or_ip, default_limits=[])
    app.state.limiter = limiter
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "X-Purpose", "Content-Type"],
    )
    if s.service_name and __import__("os").environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        _setup_tracing(app, s)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        RATE_LIMITED.inc()
        return JSONResponse(
            {"error": "rate limit exceeded", "detail": str(exc.detail)}, status_code=429, headers={"Retry-After": "60"}
        )

    @app.middleware("http")
    async def _observe_and_audit(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        if template.startswith("/patients") or template.startswith("/audit"):
            state: State = request.app.state.api
            actor = _actor(request.headers.get("x-api-key"))
            role = state.settings.api_key_roles.get(request.headers.get("x-api-key") or "", "none")
            action = f"{request.method} {template}"
            patient_id = request.path_params.get("patient_id")
            try:
                await run_in_threadpool(
                    write_audit,
                    state.engine,
                    actor=actor,
                    role=role,
                    action=action,
                    patient_id=patient_id,
                    resource=str(request.url.path),
                    purpose=request.headers.get("x-purpose"),
                    request_id=request_id,
                    client_ip=get_remote_address(request),
                    status_code=response.status_code,
                )
                AUDIT_ROWS.labels(action).inc()
            except Exception as exc:
                AUDIT_FAILURES.inc()
                log.error("audit write failed", error=str(exc)[:200])
                response = JSONResponse({"error": "audit log unavailable; request refused"}, status_code=503)
        elapsed = time.perf_counter() - started
        REQUESTS.labels(request.method, template, str(response.status_code)).observe(elapsed)
        if response.status_code in (401, 403):
            AUTH_DENIED.inc()
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request", method=request.method, route=template, status=response.status_code, ms=round(elapsed * 1000, 1)
        )
        structlog.contextvars.unbind_contextvars("request_id")
        return response

    def _state(request: Request) -> State:
        return request.app.state.api

    def require_role(*roles: str):
        def dep(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
            known = _state(request).settings.api_key_roles
            if not x_api_key or x_api_key not in known:
                raise HTTPException(401, "missing or unknown API key")
            role = known[x_api_key]
            if roles and role not in roles:
                raise HTTPException(403, f"role {role} may not do this")
            return role

        return dep

    def _pid(patient_id: str) -> str:
        if not ID_RE.match(patient_id):
            raise HTTPException(422, "malformed patient id")
        return patient_id

    # ---- open endpoints ----------------------------------------------------------------------------------
    @app.get("/health")
    def health(request: Request):
        st = _state(request)
        try:
            with st.engine.connect() as c:
                c.exec_driver_sql("select 1")
            db_ok = True
        except Exception:
            db_ok = False
        return {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "patients": st.cohort.get("patients"),
            "risk_scores": st.has_scores,
            "model_version": (st.model_metrics or {}).get("model_version"),
            "llm": st.llm.name,
            "audit_store": "ok" if db_ok else "unavailable",
        }

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---- cohort-level (analyst) --------------------------------------------------------------------------
    @app.get("/cohort", dependencies=[Depends(require_role("analyst", "admin"))])
    def cohort(request: Request):
        return _state(request).cohort

    @app.get("/quality", dependencies=[Depends(require_role("analyst", "admin"))])
    def quality(request: Request):
        st = _state(request)
        if not st.lake.delta_exists("gold", "quality_results"):
            raise HTTPException(404, "quality checks have not run")
        return (
            st.con()
            .sql("select * from gold_quality_results order by severity, check_name")
            .df()
            .to_dict(orient="records")
        )

    @app.get("/model/card", dependencies=[Depends(require_role("analyst", "admin"))])
    def model_card(request: Request):
        st = _state(request)
        if not st.model_metrics:
            raise HTTPException(404, "no trained model")
        return st.model_metrics

    # ---- patient-level (analyst), all audited --------------------------------------------------------------
    @app.get("/patients", dependencies=[Depends(require_role("analyst", "admin"))])
    @limiter.limit(s.rate_limit)
    def patients(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        min_risk: Annotated[float | None, Query(ge=0, le=1)] = None,
        search: Annotated[str | None, Query(max_length=40)] = None,
    ):
        st = _state(request)
        PATIENT_READS.labels("/patients").inc()
        con = st.con()
        if st.has_scores:
            sql = (
                "with latest as (select patient_id, arg_max(risk, discharge_ts) as risk, max(discharge_ts) as last_discharge, count(*) as admissions, "
                "max(readmitted::int)::boolean as ever_readmitted from gold_risk_scores group by 1) "
                "select p.patient_id, p.pseudonym, p.sex, p.age_band, p.age_years, p.deceased, l.risk, l.admissions, l.last_discharge "
                "from silver_patients p left join latest l using (patient_id) "
                "where ($min_risk is null or l.risk >= $min_risk) and ($search is null or p.pseudonym ilike '%' || $search || '%') "
                "order by l.risk desc nulls last, p.pseudonym limit $limit offset $offset"
            )
        else:
            sql = (
                "select p.patient_id, p.pseudonym, p.sex, p.age_band, p.age_years, p.deceased, null as risk, null as admissions, null as last_discharge "
                "from silver_patients p where ($min_risk is null or false) and ($search is null or p.pseudonym ilike '%' || $search || '%') "
                "order by p.pseudonym limit $limit offset $offset"
            )
        rows = con.execute(sql, {"min_risk": min_risk, "search": search, "limit": limit, "offset": offset}).df()
        total = con.sql("select count(*) from silver_patients").fetchone()[0]
        return {"total": total, "items": json.loads(rows.to_json(orient="records", date_format="iso"))}

    @app.get("/patients/{patient_id}", dependencies=[Depends(require_role("analyst", "admin"))])
    @limiter.limit(s.rate_limit)
    def patient(request: Request, patient_id: str):
        st = _state(request)
        pid = _pid(patient_id)
        PATIENT_READS.labels("/patients/{id}").inc()
        con = st.con()
        row = con.execute("select * from silver_patients where patient_id = ?", [pid]).df()
        if row.empty:
            raise HTTPException(404, "no such patient")
        out = json.loads(row.to_json(orient="records", date_format="iso"))[0]
        q = lambda sql: json.loads(con.execute(sql, [pid]).df().to_json(orient="records", date_format="iso"))  # noqa: E731
        out["active_problems"] = q(
            "select condition_id, display, onset_ts from silver_conditions where patient_id = ? and abatement_ts is null and clinical_status = 'active' order by onset_ts desc limit 25"
        )
        out["medications"] = q(
            "select medication_request_id, display, status, authored_ts, reason_display from silver_medication_requests where patient_id = ? order by authored_ts desc limit 20"
        )
        out["encounters"] = q(
            "select encounter_id, class_code, type_display, reason_display, start_ts, length_hours from silver_encounters where patient_id = ? order by start_ts desc limit 15"
        )
        out["allergies"] = q(
            "select allergy_id, display, category, criticality, reactions from silver_allergies where patient_id = ?"
        )
        if st.has_admissions:
            out["admissions"] = q(
                "select a.admission_id, a.admit_ts, a.discharge_ts, a.los_days, a.reason_display, a.readmitted, a.days_to_next_admission"  # noqa: S608
                + (", r.risk, r.split" if st.has_scores else ", null as risk, null as split")
                + " from gold_admissions a "
                + ("left join gold_risk_scores r using (admission_id) " if st.has_scores else "")
                + "where a.patient_id = ? order by a.admit_ts desc"
            )
        return out

    @app.get("/patients/{patient_id}/risk", dependencies=[Depends(require_role("analyst", "admin"))])
    @limiter.limit(s.rate_limit)
    def risk(request: Request, patient_id: str):
        st = _state(request)
        pid = _pid(patient_id)
        PATIENT_READS.labels("/patients/{id}/risk").inc()
        if not st.has_scores:
            raise HTTPException(404, "no risk scores: train the model first")
        con = st.con()
        row = con.execute(
            "select r.admission_id, r.discharge_ts, r.risk, r.split, r.top_factors, r.model_version, a.reason_display, a.los_days "
            "from gold_risk_scores r join gold_admissions a using (admission_id) where r.patient_id = ? order by r.discharge_ts desc limit 1",
            [pid],
        ).fetchone()
        if row is None:
            raise HTTPException(404, "patient has no inpatient admission")
        return {
            "patient_id": pid,
            "admission_id": row[0],
            "discharge_ts": row[1],
            "risk": row[2],
            "risk_band": "high"
            if row[2] >= st.settings.risk_threshold
            else ("medium" if row[2] >= st.settings.risk_threshold / 2 else "low"),
            "threshold": st.settings.risk_threshold,
            "split": row[3],
            "top_factors": json.loads(row[4] or "[]"),
            "model_version": row[5],
            "reason_display": row[6],
            "los_days": row[7],
            "note": "the label for admissions in the train or val split was seen during fitting",
        }

    @app.get("/patients/{patient_id}/facts", dependencies=[Depends(require_role("analyst", "admin"))])
    @limiter.limit(s.rate_limit)
    def facts(request: Request, patient_id: str, section: Annotated[str | None, Query(max_length=20)] = None):
        st = _state(request)
        pid = _pid(patient_id)
        PATIENT_READS.labels("/patients/{id}/facts").inc()
        items = [asdict(f) for f in load_facts(st.lake, pid) if section is None or f.section == section]
        if not items and not st.con().execute("select 1 from silver_patients where patient_id = ?", [pid]).fetchone():
            raise HTTPException(404, "no such patient")
        return {"patient_id": pid, "count": len(items), "items": items}

    @app.get("/patients/{patient_id}/summary", dependencies=[Depends(require_role("analyst", "admin"))])
    @limiter.limit(s.rate_limit)
    def summary(request: Request, patient_id: str, refresh: bool = False):
        st = _state(request)
        pid = _pid(patient_id)
        PATIENT_READS.labels("/patients/{id}/summary").inc()
        if not refresh:
            stored = current_summary(st.engine, pid)
            if stored and stored["model"] == st.llm.name:
                return {
                    "cached": True,
                    "created_at": stored["created_at"],
                    "faithfulness": stored["faithfulness"],
                    **stored["payload"],
                }
        started = time.perf_counter()
        try:
            result = summarize(st.lake, pid, st.llm, st.retriever)
        except Exception as exc:
            LLM_FAILURES.inc()
            log.error("summary failed", error=str(exc)[:200])
            raise HTTPException(502, "summary model unavailable") from exc
        if result is None:
            raise HTTPException(404, "no such patient")
        SUMMARY_LATENCY.observe(time.perf_counter() - started)
        SUMMARIES.labels(st.llm.name).inc()
        verdicts = judge_summary(result, load_facts(st.lake, pid))
        supported = sum(v.status == "supported" for v in verdicts)
        faith = {
            "claims": len(verdicts),
            "supported": supported,
            "faithfulness_rate": round(supported / len(verdicts), 4) if verdicts else None,
            "verdicts": [asdict(v) for v in verdicts],
        }
        if verdicts:
            SUMMARY_FAITHFULNESS.labels("live").set(supported / len(verdicts))
        payload = result.to_dict()
        save_summary(st.engine, pid, result.model, result.prompt_version, payload, faith)
        return {"cached": False, "faithfulness": faith, **payload}

    # ---- admin --------------------------------------------------------------------------------------------
    @app.get("/audit", dependencies=[Depends(require_role("admin"))])
    def audit(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        patient_id: Annotated[str | None, Query(max_length=64)] = None,
    ):
        st = _state(request)
        return {"items": list_audit(st.engine, limit=limit, patient_id=patient_id)}

    return app


def _setup_tracing(app: FastAPI, s: Settings) -> None:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": s.service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


app = create_app()
