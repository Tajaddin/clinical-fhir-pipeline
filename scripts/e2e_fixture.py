"""A deterministic cohort for end-to-end tests: 160 factory patients through bronze, silver, quality, gold
and training under data/e2e, in about half a minute, with no Java and no model server."""

from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from cfp.bronze import ingest  # noqa: E402
from cfp.deid import DeidContext  # noqa: E402
from cfp.gold import build_gold  # noqa: E402
from cfp.lake import Lake  # noqa: E402
from cfp.model import train  # noqa: E402
from cfp.quality import run_checks, write_results  # noqa: E402
from cfp.silver import build_silver  # noqa: E402
from fhir_factory import make_cohort, write_bundles  # noqa: E402


def main(target: Path = ROOT / "data" / "e2e") -> None:
    shutil.rmtree(target, ignore_errors=True)
    synthea = write_bundles(target / "synthea", make_cohort(160, seed=3))
    lake = Lake(str(target / "lakehouse"))
    ingest(synthea, lake)
    build_silver(lake, DeidContext(salt="e2e-salt", reference_date=date(2025, 1, 1)))
    reports = target / "reports"
    write_results(lake, run_checks(lake), reports / "data-quality.json")
    build_gold(lake, readmission_days=30, reference_date=date(2025, 1, 1))
    metrics = train(lake, reports, seed=1)
    print(
        f"e2e fixture ready under {target}: champion {metrics['champion']}, {metrics['cohort']['admissions_total']} admissions"
    )


if __name__ == "__main__":
    main()
