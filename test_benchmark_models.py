"""Offline check of benchmark ordering, retry accounting, failure handling and blind scoring."""
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import benchmark_models as b
from multi_hazard_pipeline.core import write_json


def main():
    with TemporaryDirectory(dir=b.ROOT / "tmp") as temp:
        root = Path(temp)
        source = root / "report.txt"
        source.write_text("The flood carried sediment into the basin.", encoding="utf-8")
        spec = {"repeats": 3, "models": list(b.MODELS), "reports": [
            {"id": "r1", "inputs": [str(source)]}, {"id": "r2", "inputs": [str(source)]}]}
        spec["model_reasoning_effort"] = {b.MODELS[1]: "medium"}
        with patch("urllib.request.urlopen", side_effect=AssertionError("Offline check attempted network access")):
            plan = b.prepare(spec, root / "bench")
            assert len(plan["jobs"]) == 12
            assert [j["model"] for j in plan["jobs"][:6]] == [b.MODELS[0]] * 6
            assert [j["model"] for j in plan["jobs"][6:]] == [b.MODELS[1]] * 6
            assert all(j["reasoning_effort"] is None for j in plan["jobs"][:6])
            assert all(j["reasoning_effort"] == "medium" for j in plan["jobs"][6:])
            from dataclasses import replace
            import json
            normal = b.ChatClient("offline", b.DEFAULT_CONFIG.llm)
            medium = b.ChatClient("offline", replace(b.DEFAULT_CONFIG.llm, reasoning_effort="medium"))
            args = dict(system_prompt="test", user_payload={}, response_schema=None)
            assert "reasoning_effort" not in json.loads(normal._request_body(**args))
            assert json.loads(medium._request_body(**args))["reasoning_effort"] == "medium"
            from io import BytesIO
            from unittest import TestCase
            with (patch.object(b, "available_models", return_value={"data": [{"id": m} for m in b.MODELS]}),
                  patch.object(b, "urlopen", return_value=BytesIO(json.dumps({"components": {"schemas": {
                      "ChatCompletionRequest": {"properties": {}}}}}).encode()))):
                with TestCase().assertRaisesRegex(ValueError, "does not forward reasoning_effort"):
                    b.execute(root / "bench")
            assert [j["report"] for j in plan["jobs"][:6]] == ["r1"] * 3 + ["r2"] * 3
            assert len({j["blind_id"] for j in plan["jobs"]}) == 12
            run = root / "run"
            write_json(run / "manifest.json", {"status": "failed", "correction_rounds": 1, "errors": []})
            logs = [
                {"type": "llm_call", "attempt": 1, "outcome": "validation_error", "usage": {"total_tokens": 10}},
                {"type": "llm_call", "attempt": 2, "outcome": "pass", "usage": {"total_tokens": 20}},
                {"type": "llm_call", "attempt": 1, "outcome": "request_error", "usage": None, "retry_delay_seconds": 2},
                {"type": "llm_call", "attempt": 2, "outcome": "request_error", "usage": None},
                {"type": "stage", "stage": "segmentation", "elapsed_seconds": 9}]
            import json
            (run / "timings.jsonl").write_text("\n".join(json.dumps(x) for x in logs), encoding="utf-8")
            m = b.run_metrics(run, 15)
            assert (m["logical_calls"], m["retry_attempts"], m["first_pass_calls"]) == (2, 2, 0)
            assert m["request_errors"] == 2 and m["validation_errors"] == 1
            assert m["total_tokens"] == 30 and m["prompt_tokens"] is None and m["usage_reported_attempts"] == 2
            assert m["candidate_present"] is False and m["retry_wait_seconds"] == 2
            write_json(run / "candidate_report.json", {"rows": []})
            m = b.run_metrics(run, 15)
            assert m["structural_checks_pass"] is False
            plan["jobs"][0]["result"] = m
            write_json(root / "bench/plan.json", plan)
            b.write_csv(root / "bench/ratings.csv", [{"blind_id": j["blind_id"], "report": j["report"],
                        **{f: "" for f in b.RATINGS}, "notes": ""} for j in plan["jobs"]],
                        ["blind_id", "report", *b.RATINGS, "notes"])
            summary = b.summarize(root / "bench")
            assert summary["completed_runs"] == 1
            first = summary["models"][b.MODELS[0]]
            assert first["status_counts"] == {"failed": 1} and first["manual_quality_out_of_20"] is None
            assert first["total_seconds_per_reviewer_pass"] is None
            with (root / "bench/ratings.csv").open(encoding="utf-8-sig", newline="") as stream:
                ratings = list(csv.DictReader(stream))
            ratings[0].update({f: "3" for f in b.RATINGS})
            b.write_csv(root / "bench/ratings.csv", ratings, list(ratings[0]))
            assert b.summarize(root / "bench")["models"][b.MODELS[0]]["manual_quality_out_of_20"]["mean"] == 15
    print("Benchmark offline check passed")


if __name__ == "__main__":
    main()
