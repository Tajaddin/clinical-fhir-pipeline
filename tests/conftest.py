from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cfp.bronze import ingest
from cfp.deid import DeidContext
from cfp.lake import Lake
from cfp.silver import build_silver
from fhir_factory import make_cohort, write_bundles

N_PATIENTS = 30
SALT = "unit-test-salt"
REFERENCE = date(2025, 1, 1)


@pytest.fixture(scope="session")
def synthea_dir(tmp_path_factory) -> Path:
    return write_bundles(tmp_path_factory.mktemp("synthea"), make_cohort(N_PATIENTS))


@pytest.fixture(scope="session")
def ctx() -> DeidContext:
    return DeidContext(salt=SALT, reference_date=REFERENCE, max_shift_days=30)


@pytest.fixture(scope="session")
def lake(tmp_path_factory, synthea_dir, ctx) -> Lake:
    lake = Lake(str(tmp_path_factory.mktemp("lakehouse")))
    ingest(synthea_dir, lake)
    build_silver(lake, ctx, batch_size=500)
    return lake


@pytest.fixture(scope="session")
def trained(tmp_path_factory, ctx):
    """160 factory patients through bronze, silver, gold and training: (lake, metrics, reports_dir)."""
    from cfp.gold import build_gold
    from cfp.model import train
    from cfp.quality import run_checks, write_results

    synthea = write_bundles(tmp_path_factory.mktemp("synthea-big"), make_cohort(160, seed=3))
    lake = Lake(str(tmp_path_factory.mktemp("lake-big")))
    ingest(synthea, lake)
    build_silver(lake, ctx, batch_size=2000)
    build_gold(lake, readmission_days=30, reference_date=REFERENCE)
    reports = tmp_path_factory.mktemp("reports")
    write_results(lake, run_checks(lake), reports / "data-quality.json")
    metrics = train(lake, reports, seed=1, mlflow_uri=f"sqlite:///{(reports / 'mlflow.db').as_posix()}")
    return lake, metrics, reports
