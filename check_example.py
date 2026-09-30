from __future__ import annotations

import os
import shutil
from pathlib import Path

from multi_hazard_pipeline.agents import extract_docx
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.pipeline import run_pipeline


ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "results" / "inputs" / "tmp_single_input"
OUTPUT_DIR = ROOT / "results" / "example_check"
EXPECTED_COLUMNS = DEFAULT_CONFIG.export_columns
REMOVED_COLUMNS = {
    "elevation_zone",
    "f_ebi",
    "g_mrn",
    "h_dd",
    "scw",
    "mrn_0_4",
    "dd_0_4",
    "all_equal",
}


def main() -> int:
    docx_chunks, _ = extract_docx(INPUT_DIR / "Schnannerbach_extract1.docx", "sample", 1)
    assert docx_chunks, "docx extraction returned no paragraph chunks"

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    if not os.environ.get("TW_LLM_API_KEY"):
        raise SystemExit("TW_LLM_API_KEY is required for the regression run")

    result = run_pipeline(INPUT_DIR, OUTPUT_DIR)
    assert result["status"] == "awaiting_human_review", result
    candidate_path = Path(result["artifact_dir"]) / "candidate_report.json"
    import json

    rows = json.loads(candidate_path.read_text(encoding="utf-8"))["rows"]
    assert len(rows) >= 5, len(rows)
    assert rows[0]["event"], rows[0]
    for row in rows:
        assert row["generalized_category"] in DEFAULT_CONFIG.generalized_categories, row
        assert row["interaction_type"] in DEFAULT_CONFIG.interaction_types, row
        assert row["sediment_transport_phase"] in DEFAULT_CONFIG.sediment_transport_phases, row
        assert not (REMOVED_COLUMNS & row.keys()), row
    assert not (Path(result["artifact_dir"]) / "final_rows.json").exists()
    assert not (Path(result["artifact_dir"]) / "final_rows.csv").exists()
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
