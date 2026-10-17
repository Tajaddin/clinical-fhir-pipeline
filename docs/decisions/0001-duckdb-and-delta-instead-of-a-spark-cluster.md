# 0001. DuckDB and Delta Lake instead of a Spark cluster

Date: 2026-10-17

## Context

The brief asks for a bronze/silver/gold lakehouse with Delta or Iceberg tables, in PySpark or DuckDB. The
data is 2,263 Synthea patients: 2.1 million FHIR resources, 2.7 GB of JSON, 1.7 million observations after
extraction. A hospital's real feed would be larger, but the analytics in this project (cohort building,
feature aggregation, quality checks) are SQL over tables that fit in memory many times over.

## Decision

- **DuckDB** runs the SQL (gold cohort, features, facts, quality checks); **delta-rs** writes the silver
  and gold tables in **Delta Lake** format; bronze stays Parquet partitioned by resource type.
- Delta tables are read back through delta-rs and registered in DuckDB as Arrow datasets, which works the
  same on a laptop, in CI and against Azure Data Lake Storage (`abfs://`) without a DuckDB extension.
- Extraction from FHIR JSON into typed rows is plain Python per resource type, because it is the part that
  needs tests on individual documents (components, extensions, references); SQL starts once rows exist.

## Consequences

- The whole pipeline runs in about a minute on one machine (bronze 20 s, silver 23 s, gold 1 s), which
  is what makes a CI job that regenerates a cohort and re-runs everything affordable.
- The tables are readable from Spark, Databricks, Fabric or Trino unchanged, so moving the compute to a
  cluster later is a change of engine, not of storage.
- No cluster means no demonstration of partition tuning, shuffles or cluster sizing; the trade is accepted
  and stated in the README.
