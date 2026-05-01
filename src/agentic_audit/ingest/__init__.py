"""Bronze-tier ingest helpers.

Pure-Python row extractors for the Step-1 corpus (workpaper xlsx + TOC json).
Framework-agnostic — no Spark / Databricks imports — so the row-shaping logic
is unit-testable from a normal pytest run. The Databricks notebook in
``infra/databricks/notebooks/bronze_smoke_ingest.py`` converts the records this
module yields into Spark rows and writes them to ``audit_dev.bronze.*``.
"""
