"""Bronze: every resource of every bundle as one row (type, id, patient, raw JSON), Parquet partitioned by type.

Nothing is transformed here; this is the landing zone that silver is derived from, so a change in the
silver logic never needs the source files again. Bronze still holds identifiers, so in a real deployment it
sits behind the tightest access control; silver and gold are de-identified.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pyarrow as pa

from cfp.fhir import patient_id_of
from cfp.lake import Lake

ROW_SCHEMA = pa.schema(
    [
        ("resource_id", pa.string()),
        ("patient_id", pa.string()),
        ("source_file", pa.string()),
        ("json", pa.string()),
    ]
)
MANIFEST_SCHEMA = pa.schema(
    [
        ("source_file", pa.string()),
        ("patient_id", pa.string()),
        ("resources", pa.int64()),
        ("bytes", pa.int64()),
        ("ingested_at", pa.timestamp("us")),
    ]
)


@dataclass
class IngestStats:
    files: int = 0
    resources: int = 0
    bytes: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    without_patient: int = 0


def bundle_files(synthea_dir: Path) -> list[Path]:
    return sorted((Path(synthea_dir) / "fhir").glob("*.json"))


def resources_in(path: Path) -> Iterator[tuple[str, dict]]:
    bundle = orjson.loads(path.read_bytes())
    for entry in bundle.get("entry", []):
        resource = entry.get("resource")
        if isinstance(resource, dict) and resource.get("resourceType") and resource.get("id"):
            yield resource["resourceType"], resource


def ingest(synthea_dir: Path, lake: Lake, batch_rows: int = 50_000) -> IngestStats:
    """Read every bundle under <synthea_dir>/fhir and (re)write bronze/resources and bronze/manifest."""
    lake.remove("bronze", "resources")
    lake.remove("bronze", "manifest")
    stats = IngestStats()
    buffers: dict[str, list[tuple[str, str | None, str, str]]] = {}
    parts: dict[str, int] = {}
    manifest: list[dict] = []
    now = datetime.now(UTC).replace(tzinfo=None)

    def flush(resource_type: str) -> None:
        rows = buffers.get(resource_type)
        if not rows:
            return
        table = pa.Table.from_pylist(
            [{"resource_id": r[0], "patient_id": r[1], "source_file": r[2], "json": r[3]} for r in rows],
            schema=ROW_SCHEMA,
        )
        n = parts.get(resource_type, 0)
        lake.write_parquet("bronze", f"resources/resource_type={resource_type}", table, f"part-{n:05d}")
        parts[resource_type] = n + 1
        buffers[resource_type] = []

    for path in bundle_files(synthea_dir):
        stats.files += 1
        size = path.stat().st_size
        stats.bytes += size
        count = 0
        bundle_patient: str | None = None
        for resource_type, resource in resources_in(path):
            patient_id = patient_id_of(resource)
            if resource_type == "Patient":
                bundle_patient = patient_id
            if patient_id is None:
                stats.without_patient += 1
            buffers.setdefault(resource_type, []).append(
                (resource["id"], patient_id, path.name, orjson.dumps(resource).decode())
            )
            stats.by_type[resource_type] = stats.by_type.get(resource_type, 0) + 1
            count += 1
            if len(buffers[resource_type]) >= batch_rows:
                flush(resource_type)
        stats.resources += count
        manifest.append(
            {
                "source_file": path.name,
                "patient_id": bundle_patient,
                "resources": count,
                "bytes": size,
                "ingested_at": now,
            }
        )

    for resource_type in list(buffers):
        flush(resource_type)
    lake.write_parquet("bronze", "manifest", pa.Table.from_pylist(manifest, schema=MANIFEST_SCHEMA), "part-00000")
    return stats
