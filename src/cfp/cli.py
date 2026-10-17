"""Command line: each pipeline step and the whole thing."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date

import typer

from cfp.config import settings
from cfp.lake import Lake
from cfp.logging_setup import configure_logging, get_logger
from cfp.wiring import make_llm, make_retriever

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Clinical FHIR pipeline")
log = get_logger("cli")


def _lake() -> Lake:
    return Lake(settings().lakehouse_root)


def _echo(title: str, payload: dict, started: float) -> None:
    typer.echo(f"{title} in {time.time() - started:.1f}s")
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.callback()
def _setup() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    configure_logging(settings().log_level, "cfp")


@app.command()
def synthea(patients: int = 2000, seed: int = 42) -> None:
    """Generate the synthetic cohort with the pinned Synthea release (needs Java and bash)."""
    s = settings()
    cmd = ["bash", "scripts/synthea.sh", str(patients), str(seed), str(s.synthea_dir)]
    typer.echo(" ".join(cmd))
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def bronze() -> None:
    """Land every resource of every bundle as Parquet."""
    from cfp.bronze import ingest

    started = time.time()
    stats = ingest(settings().synthea_dir, _lake())
    _echo("bronze", stats.__dict__, started)


@app.command()
def silver() -> None:
    """Typed, de-identified Delta tables from bronze."""
    from cfp.silver import build_silver, context_from_settings

    s = settings()
    started = time.time()
    stats = build_silver(_lake(), context_from_settings(s.deid_salt, s.reference_date, s.date_shift_max_days))
    _echo("silver", stats.__dict__, started)


@app.command()
def quality(allow_failures: bool = False) -> None:
    """Run the data quality checks; exit 1 on a critical failure unless --allow-failures."""
    from cfp.quality import enforce, run_checks, write_results

    s = settings()
    started = time.time()
    results = run_checks(_lake())
    summary = write_results(_lake(), results, s.reports_dir / "data-quality.json")
    _echo("quality", {k: v for k, v in summary.items() if k != "results"}, started)
    if not allow_failures:
        enforce(summary)


@app.command()
def gold() -> None:
    """Readmission cohort with features, and the patient facts used by the summaries."""
    from cfp.gold import build_gold

    s = settings()
    started = time.time()
    stats = build_gold(_lake(), s.readmission_days, date.fromisoformat(s.reference_date))
    _echo("gold", stats.__dict__, started)


@app.command()
def train(mlflow: bool = True) -> None:
    """Fit, calibrate and evaluate the readmission models; score every admission into gold.risk_scores."""
    import os

    from cfp.model import train as train_model

    s = settings()
    started = time.time()
    metrics = train_model(
        _lake(),
        s.reports_dir,
        seed=s.seed,
        test_size=s.test_size,
        val_size=s.val_size,
        threshold=s.risk_threshold,
        mlflow_uri=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db") if mlflow else None,
    )
    brief = {
        "champion": metrics["champion"],
        "cohort": metrics["cohort"],
        **{
            name: {k: round(v, 4) for k, v in r["test"].items() if isinstance(v, int | float)}
            for name, r in metrics["models"].items()
        },
    }
    _echo("train", brief, started)


@app.command()
def summarize(patient_id: str) -> None:
    """One cited summary for a patient, printed as JSON (no audit log: that is the API's job)."""
    from cfp.summaries import summarize as run

    s = settings()
    started = time.time()
    summary = run(_lake(), patient_id, make_llm(s), make_retriever(s))
    if summary is None:
        typer.echo("no such patient", err=True)
        raise typer.Exit(1)
    _echo("summary", summary.to_dict(), started)


@app.command()
def eval_summaries(patients: int = 40, llm_judge: bool = False) -> None:
    """Generate summaries for a deterministic sample of patients and measure faithfulness."""
    from cfp.faithfulness import evaluate

    s = settings()
    started = time.time()
    con = _lake().connect([("gold", "patient_facts")])
    ids = [
        r[0]
        for r in con.sql(
            f"select patient_id from (select patient_id, count(*) n from gold_patient_facts group by 1) where n >= 20 order by patient_id limit {int(patients)}"  # noqa: S608
        ).fetchall()
    ]
    report = evaluate(
        _lake(),
        make_llm(s),
        make_retriever(s),
        ids,
        s.reports_dir / "summary-faithfulness.json",
        examples_dir=s.reports_dir / "summaries",
        with_llm_judge=llm_judge,
    )
    _echo("eval-summaries", {k: v for k, v in report.items() if k != "per_patient"}, started)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104
    """Run the API."""
    import os

    import uvicorn

    # Behind the dashboard's nginx (Compose) or a cloud ingress the client address arrives in X-Forwarded-For;
    # only proxies listed here are trusted to set it.
    uvicorn.run(
        "cfp.serving.app:app",
        host=host,
        port=port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("CFP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


@app.command()
def pipeline(allow_quality_failures: bool = False, with_train: bool = True, eval_patients: int = 20) -> None:
    """bronze -> silver -> quality -> gold -> train -> eval-summaries."""
    bronze()
    silver()
    quality(allow_failures=allow_quality_failures)
    gold()
    if with_train:
        train()
    eval_summaries(patients=eval_patients)


if __name__ == "__main__":
    app()
