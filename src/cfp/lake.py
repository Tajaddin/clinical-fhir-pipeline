"""The lakehouse: Parquet for bronze, Delta tables for silver and gold, DuckDB for SQL over both.

The root is a local folder or an object-store URL understood by delta-rs and pyarrow (abfs://, s3://).
Delta tables are read through delta-rs and registered in DuckDB as Arrow datasets, which works the same
locally, in CI and against cloud storage without a DuckDB extension.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from deltalake import DeltaTable, write_deltalake
from pyarrow import fs

LAYERS = ("bronze", "silver", "gold")


class Lake:
    def __init__(self, root: str) -> None:
        self.root = root.rstrip("/")
        self.is_local = "://" not in self.root
        if self.is_local:
            Path(self.root).mkdir(parents=True, exist_ok=True)
            self.root = Path(self.root).resolve().as_posix()
        self.storage_options = {
            k: v
            for k, v in {
                "AZURE_STORAGE_ACCOUNT_NAME": os.environ.get("AZURE_STORAGE_ACCOUNT_NAME"),
                "AZURE_STORAGE_ACCOUNT_KEY": os.environ.get("AZURE_STORAGE_ACCOUNT_KEY"),
                "AWS_REGION": os.environ.get("AWS_REGION"),
            }.items()
            if v
        }

    # ---- paths -------------------------------------------------------------------------------------------
    def path(self, layer: str, table: str) -> str:
        assert layer in LAYERS
        return f"{self.root}/{layer}/{table}"

    def _filesystem(self) -> tuple[fs.FileSystem, str]:
        if self.is_local:
            return fs.LocalFileSystem(), self.root
        return fs.FileSystem.from_uri(self.root)

    # ---- bronze: Parquet ---------------------------------------------------------------------------------
    def write_parquet(self, layer: str, table: str, batch: pa.Table, part: str) -> str:
        filesystem, base = self._filesystem()
        directory = f"{base}/{layer}/{table}"
        filesystem.create_dir(directory, recursive=True)
        target = f"{directory}/{part}.parquet"
        pq.write_table(batch, target, filesystem=filesystem, compression="zstd")
        return target

    def remove(self, layer: str, table: str) -> None:
        filesystem, base = self._filesystem()
        directory = f"{base}/{layer}/{table}"
        with contextlib.suppress(FileNotFoundError):
            filesystem.delete_dir(directory)

    def parquet_dataset(self, layer: str, table: str) -> ds.Dataset:
        filesystem, base = self._filesystem()
        return ds.dataset(f"{base}/{layer}/{table}", filesystem=filesystem, format="parquet", partitioning="hive")

    def parquet_exists(self, layer: str, table: str) -> bool:
        filesystem, base = self._filesystem()
        info = filesystem.get_file_info(f"{base}/{layer}/{table}")
        return info.type != fs.FileType.NotFound

    # ---- silver and gold: Delta --------------------------------------------------------------------------
    def write_delta(
        self,
        layer: str,
        table: str,
        data: pa.Table,
        mode: Literal["overwrite", "append"] = "overwrite",
        partition_by: list[str] | None = None,
    ) -> None:
        options = self.storage_options or None
        if mode == "overwrite":
            write_deltalake(
                self.path(layer, table),
                data,
                mode="overwrite",
                schema_mode="overwrite",
                partition_by=partition_by,
                storage_options=options,
            )
        else:
            write_deltalake(
                self.path(layer, table), data, mode="append", partition_by=partition_by, storage_options=options
            )

    def delta(self, layer: str, table: str) -> DeltaTable:
        return DeltaTable(self.path(layer, table), storage_options=self.storage_options or None)

    def delta_exists(self, layer: str, table: str) -> bool:
        return DeltaTable.is_deltatable(self.path(layer, table), storage_options=self.storage_options or None)

    def read_delta(self, layer: str, table: str) -> pa.Table:
        return self.delta(layer, table).to_pyarrow_table()

    def list_delta(self, layer: str) -> list[str]:
        filesystem, base = self._filesystem()
        selector = fs.FileSelector(f"{base}/{layer}", allow_not_found=True)
        return sorted(
            Path(i.path).name
            for i in filesystem.get_file_info(selector)
            if i.type == fs.FileType.Directory and self.delta_exists(layer, Path(i.path).name)
        )

    # ---- DuckDB ------------------------------------------------------------------------------------------
    def connect(self, tables: Iterable[tuple[str, str]] | None = None) -> duckdb.DuckDBPyConnection:
        """A DuckDB connection with silver and gold Delta tables registered as views named <layer>_<table>."""
        con = duckdb.connect()
        wanted = (
            list(tables)
            if tables is not None
            else [(lyr, t) for lyr in ("silver", "gold") for t in self.list_delta(lyr)]
        )
        for layer, table in wanted:
            if self.delta_exists(layer, table):
                con.register(f"{layer}_{table}", self.delta(layer, table).to_pyarrow_dataset())
        return con
