"""Operational store for the API: the audit log of every access to patient data, and generated summaries.
SQLite on a laptop, Postgres in Compose and in the cloud; the schema is the same."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    desc,
    select,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("actor", String(64), nullable=False),  # api key id (prefix), never the key
    Column("role", String(16), nullable=False),
    Column("action", String(64), nullable=False),
    Column("patient_id", String(64), index=True),
    Column("resource", String(128)),
    Column("purpose", String(128)),
    Column("request_id", String(64)),
    Column("client_ip", String(64)),
    Column("status_code", Integer),
    Column("detail", Text),
)

summaries = Table(
    "summaries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("model", String(64), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("faithfulness", JSON),
    Column("current", Boolean, nullable=False, default=True),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False}, future=True)
    return create_engine(url, pool_pre_ping=True, future=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def write_audit(engine: Engine, **row: Any) -> None:
    row.setdefault("ts", datetime.now(UTC).replace(tzinfo=None))
    with engine.begin() as con:
        con.execute(audit_log.insert().values(**row))


def list_audit(engine: Engine, limit: int = 100, patient_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(audit_log).order_by(desc(audit_log.c.id)).limit(limit)
    if patient_id:
        stmt = stmt.where(audit_log.c.patient_id == patient_id)
    with engine.connect() as con:
        return [dict(r._mapping) for r in con.execute(stmt)]


def audit_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as con:
        rows = con.execute(select(audit_log.c.action, audit_log.c.id).order_by(audit_log.c.id)).fetchall()
    out: dict[str, int] = {}
    for action, _ in rows:
        out[action] = out.get(action, 0) + 1
    return out


def save_summary(
    engine: Engine, patient_id: str, model: str, prompt_version: str, payload: dict, faithfulness: dict | None
) -> int:
    with engine.begin() as con:
        con.execute(summaries.update().where(summaries.c.patient_id == patient_id).values(current=False))
        result = con.execute(
            summaries.insert().values(
                patient_id=patient_id,
                model=model,
                prompt_version=prompt_version,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                payload=json.loads(json.dumps(payload, default=str)),
                faithfulness=faithfulness,
                current=True,
            )
        )
        pk = result.inserted_primary_key
        return int(pk[0]) if pk else 0


def current_summary(engine: Engine, patient_id: str) -> dict[str, Any] | None:
    stmt = (
        select(summaries)
        .where(summaries.c.patient_id == patient_id, summaries.c.current.is_(True))
        .order_by(desc(summaries.c.id))
        .limit(1)
    )
    with engine.connect() as con:
        row = con.execute(stmt).first()
    return dict(row._mapping) if row else None
