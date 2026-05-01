"""Layer 1: Deterministic extraction from bronze to silver.

Reads workpaper rows from `audit_dev.bronze.workpapers_raw`, runs
control-aware attribute checks, writes 4-row `ExtractedEvidence` records
to `audit_dev.silver.evidence` via idempotent MERGE.

No LLM, no randomness, no network beyond Databricks SQL. Same bronze
rows in → exactly the same silver output every time.
"""

from agentic_audit.layer1_extract.attribute_checks import check_attribute
from agentic_audit.layer1_extract.bronze_reader import (
    BronzeReader,
    BronzeWorkpaperRow,
    ExtractionError,
    parse_control_quarter_from_path,
)
from agentic_audit.layer1_extract.orchestrator import extract
from agentic_audit.layer1_extract.silver_writer import (
    SilverEvidenceRow,
    SilverWriter,
)

__all__ = [
    "BronzeReader",
    "BronzeWorkpaperRow",
    "ExtractionError",
    "SilverEvidenceRow",
    "SilverWriter",
    "check_attribute",
    "extract",
    "parse_control_quarter_from_path",
]
