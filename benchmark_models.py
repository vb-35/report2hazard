"""Paired, repeated full-pipeline model benchmark; uses only existing dependencies."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import random
import statistics
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from urllib.request import Request, urlopen

from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.core import read_json, write_json
from multi_hazard_pipeline.llm import ChatClient
from multi_hazard_pipeline.pipeline import create_run, execute_run, utc_now
from multi_hazard_pipeline.schemas import CONTROLLED_FIELDS, validate_classification_payload, validate_segment_chain

ROOT = Path(__file__).resolve().parent
MODELS = ("google/gemma-4-31B-it", "Inferact/Qwen3.8-27B-NVFP4")
RATINGS = ("factual_support", "taxonomy", "completeness", "causality", "translation")


def available_models():
    c = DEFAULT_CONFIG.llm
    req = Request(c.api_base_url.rstrip("/") + "/models",
                  headers={"Authorization": "Bearer " + c.api_key_from_env()})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(spec, directory):
    if spec.get("repeats", 0) < 2 or len(spec.get("reports", [])) < 2:
        raise ValueError("Use at least two reports and two repetitions")
    models = spec.get("models", list(MODELS))
    if len(models) < 2 or len(set(models)) != len(models):
        raise ValueError("Use at least two distinct explicit model IDs")
    efforts = spec.get("model_reasoning_effort", {})
    if any(model not in models or effort not in {"low", "medium", "high", "xhigh"}
           for model, effort in efforts.items()):
        raise ValueError("Reasoning settings need a planned model and a supported effort")
    ids = [r["id"] for r in spec["reports"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Report IDs must be unique")
    for report in spec["reports"]:
        if not report.get("inputs"):
            raise ValueError("Each report needs explicitly selected companion inputs")
        report["inputs"] = [str(Path(p).resolve()) for p in report["inputs"]]
        for p in report["inputs"]:
            if Path(p).suffix.lower() not in {".pdf", ".docx", ".txt"}:
                raise ValueError(f"Unsupported input: {p}")
        report["sha256"] = {p: fingerprint(p) for p in report["inputs"]}
    llm = replace(DEFAULT_CONFIG.llm, timeout_seconds=spec.get("timeout_seconds", 660))
    settings = asdict(replace(DEFAULT_CONFIG, llm=llm))
    settings["llm"].pop("api_base_url")
    settings["schema_path"] = str(settings["schema_path"])
    jobs = []
    # Keep each model loaded for every report and repetition before switching.
    for model in models:
        for report in spec["reports"]:
            for repetition in range(1, spec["repeats"] + 1):
                jobs.append({"report": report["id"], "model": model, "repetition": repetition,
                             "reasoning_effort": efforts.get(model),
                             "state": "pending", "result": None})
    # Fixed aliases permit blind review; keep the separate model key closed during scoring.
    aliases = [f"C{i:03d}" for i in range(1, len(jobs) + 1)]
    random.Random(spec.get("seed", 1729)).shuffle(aliases)
    for job, alias in zip(jobs, aliases):
        job["blind_id"] = alias
    directory.mkdir(parents=True, exist_ok=False)
    code_paths = [Path(__file__), *sorted((ROOT / "multi_hazard_pipeline").rglob("*.py")),
                  DEFAULT_CONFIG.schema_path, ROOT / "Translation resources/multi_hazard_keywords.csv"]
    plan = {"created_at": utc_now(), "status": "prepared", "schedule": "all_reports_per_model", "settings": settings,
            "code_sha256": {str(p): fingerprint(p) for p in code_paths},
            "runtime": {"python": platform.python_version(), "dependencies": {
                name: importlib.metadata.version(name) for name in ("pdfplumber", "pypdf", "python-docx", "jsonschema", "lingua-language-detector")}},
            "spec": spec, "jobs": jobs, "warmups": []}
    write_json(directory / "plan.json", plan)
    return plan


def run_metrics(run_dir, wall_seconds):
    manifest = read_json(run_dir / "manifest.json")
    logs = [json.loads(line) for line in (run_dir / "timings.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [row for row in logs if row["type"] == "llm_call"]
    first = [row for row in calls if row["attempt"] == 1]
    outcomes = Counter(row["outcome"] for row in calls)
    usage = [row["usage"] for row in calls if isinstance(row.get("usage"), dict)]
    stages = Counter()
    for row in logs:
        if row["type"] == "stage":
            stages[row["stage"]] += row["elapsed_seconds"]
    metrics = {"status": manifest["status"], "wall_seconds": round(wall_seconds, 3),
               "llm_attempts": len(calls), "logical_calls": len(first),
               "first_pass_calls": sum(row["outcome"] == "pass" for row in first),
               "retry_attempts": sum(row["attempt"] > 1 for row in calls),
               "request_errors": outcomes["request_error"], "parsing_errors": outcomes["parsing_error"],
               "validation_errors": outcomes["validation_error"],
               "retry_wait_seconds": sum(row.get("retry_delay_seconds") or 0 for row in calls),
               "correction_rounds": manifest["correction_rounds"],
               "usage_reported_attempts": len(usage), "stage_seconds": dict(stages),
               "errors": manifest["errors"], "candidate_present": False,
               "structural_checks_pass": None, "structural_error": None,
               "segment_count": None, "uncertain_label_fraction": None}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [u[key] for u in usage if isinstance(u.get(key), (int, float))]
        metrics[key] = sum(values) if values else None
        metrics[key + "_reported_attempts"] = len(values)
    candidate_path = run_dir / "candidate_report.json"
    if candidate_path.exists():
        candidate = read_json(candidate_path)
        rows = candidate["rows"]
        metrics.update(candidate_present=True, segment_count=len(rows))
        metrics["uncertain_label_fraction"] = (sum(row.get(f) == "unknown" for row in rows for f in CONTROLLED_FIELDS)
                                                / (len(rows) * len(CONTROLLED_FIELDS))) if rows else None
        try:
            if not rows:
                raise ValueError("Empty candidate chain")
            validate_segment_chain({"segments": rows}, read_json(run_dir / "source.json"))
            validate_classification_payload({"rows": rows}, DEFAULT_CONFIG)
        except Exception as exc:
            metrics.update(structural_checks_pass=False, structural_error=str(exc))
        else:
            metrics["structural_checks_pass"] = True
    return metrics


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def distribution(values):
    if not values:
        return None
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else None}


def summarize(directory):
    plan = read_json(directory / "plan.json")
    completed = [j for j in plan["jobs"] if j["result"] is not None]
    ratings_path = directory / "ratings.csv"
    with ratings_path.open(encoding="utf-8-sig", newline="") as stream:
        ratings = {r["blind_id"]: r for r in csv.DictReader(stream)}
    summary = {"status": plan["status"], "planned_runs": len(plan["jobs"]),
               "completed_runs": len(completed), "models": {}, "by_report": {}}
    for group, keys in (("models", list(dict.fromkeys(j["model"] for j in plan["jobs"]))),
                        ("by_report", list(dict.fromkeys(j["report"] for j in plan["jobs"])))):
        for key in keys:
            jobs = [j for j in completed if j["model" if group == "models" else "report"] == key]
            results = [j["result"] for j in jobs]
            total = sum(r["wall_seconds"] for r in results)
            ready = [r for r in results if r["status"] == "awaiting_human_review"]
            candidates = [r for r in results if r["candidate_present"]]
            scores = []
            for job in jobs:
                rating = ratings[job["blind_id"]]
                if all(rating.get(f, "").strip() for f in RATINGS):
                    values = [float(rating[f]) for f in RATINGS]
                    if any(not 0 <= value <= 4 for value in values):
                        raise ValueError("Quality ratings must be between 0 and 4")
                    scores.append(sum(values))
            attempts = sum(r["logical_calls"] for r in results)
            summary[group][key] = {"runs": len(results), "status_counts": dict(Counter(r["status"] for r in results)),
                "wall_seconds_all": distribution([r["wall_seconds"] for r in results]),
                "wall_seconds_ready": distribution([r["wall_seconds"] for r in ready]),
                "total_seconds_per_candidate": total / len(candidates) if candidates else None,
                "total_seconds_per_reviewer_pass": total / len(ready) if ready else None,
                "first_pass_call_rate": sum(r["first_pass_calls"] for r in results) / attempts if attempts else None,
                "retry_attempts": sum(r["retry_attempts"] for r in results),
                "request_errors": sum(r["request_errors"] for r in results),
                "output_errors": sum(r["parsing_errors"] + r["validation_errors"] for r in results),
                "correction_rounds": distribution([r["correction_rounds"] for r in results]),
                "structurally_valid_candidates": sum(r["structural_checks_pass"] is True for r in results),
                "manual_quality_out_of_20": distribution(scores), "rated_runs": len(scores)}
    write_json(directory / "summary.json", summary)
    fields = ["blind_id", "report", "model", "reasoning_effort", "repetition", "artifact_dir", "status", "wall_seconds",
              "logical_calls", "first_pass_calls", "llm_attempts", "retry_attempts", "request_errors",
              "parsing_errors", "validation_errors", "retry_wait_seconds", "correction_rounds",
              "candidate_present", "structural_checks_pass", "segment_count", "uncertain_label_fraction",
              "usage_reported_attempts", "prompt_tokens", "completion_tokens", "total_tokens"]
    write_csv(directory / "runs.csv", [{f: j.get(f, j["result"].get(f)) for f in fields} for j in completed], fields)
    write_json(directory / "model_key.json", [{k: j.get(k) for k in ("blind_id", "report", "model", "repetition", "artifact_dir")} for j in plan["jobs"]])
    lines = [f"Benchmark: {summary['completed_runs']}/{summary['planned_runs']} runs ({plan['status']}).",
             "", "Reviewer pass is readiness for human review, not measured semantic accuracy.", "",
             "| Model | Runs | Ready for review | Median seconds (all) | First-call pass | Retries | Rated quality /20 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for model, value in summary["models"].items():
        timing = value["wall_seconds_all"]
        quality = value["manual_quality_out_of_20"]
        rate = value["first_pass_call_rate"]
        lines.append(f"| {model} | {value['runs']} | {value['status_counts'].get('awaiting_human_review', 0)} | "
                     f"{round(timing['median'], 1) if timing else 'pending'} | {f'{rate:.1%}' if rate is not None else 'pending'} | "
                     f"{value['retry_attempts']} | {round(quality['mean'], 1) if quality else 'pending review'} |")
    lines.extend(["", "See runs.csv for each report/repetition, summary.json for dispersion and failure-adjusted time,",
                  "and review/ plus ratings.csv for blind source-grounded quality review.",
                  "Model switching warmups are recorded in plan.json and excluded from report timings."])
    for model, effort in plan["spec"].get("model_reasoning_effort", {}).items():
        lines.append(f"Reasoning effort: {model} = {effort}.")
    if plan.get("comparison_note"):
        lines.extend(["", plan["comparison_note"]])
    (directory / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def execute(directory):
    plan = read_json(directory / "plan.json")
    models_payload = available_models()
    ids = {item["id"] for item in models_payload["data"]}
    if any(j["model"] not in ids for j in plan["jobs"]):
        raise ValueError("A planned model is absent from the API model list")
    write_json(directory / "api_models.json", models_payload)
    if plan["spec"].get("model_reasoning_effort"):
        # Refuse a benchmark whose router silently discards the requested effort.
        schema_url = plan["spec"].get("router_openapi_url",
            DEFAULT_CONFIG.llm.api_base_url.rstrip("/").removesuffix("/v1") + "/openapi.json")
        req = Request(schema_url)
        with urlopen(req, timeout=30) as response:
            properties = json.load(response)["components"]["schemas"]["ChatCompletionRequest"]["properties"]
        if "reasoning_effort" not in properties:
            raise ValueError("API router does not forward reasoning_effort; install the forwarding fix first")
        write_json(directory / "reasoning_router_verification.json", {"verified_at": utc_now(),
                   "reasoning_effort_forwarded": True, "properties": properties})
    settings = dict(plan["settings"])
    llm_settings = dict(settings.pop("llm"))
    if plan["spec"].get("api_base_url"):
        llm_settings["api_base_url"] = plan["spec"]["api_base_url"]
    settings["schema_path"] = Path(settings["schema_path"])
    config = replace(DEFAULT_CONFIG, **settings, llm=replace(DEFAULT_CONFIG.llm, **llm_settings))
    if any(fingerprint(p) != digest for p, digest in plan["code_sha256"].items()):
        raise ValueError("Benchmark code or glossary changed after preparation")
    for report in plan["spec"]["reports"]:
        if any(fingerprint(p) != digest for p, digest in report["sha256"].items()):
            raise ValueError("Benchmark input changed after preparation")
    reports = {r["id"]: r for r in plan["spec"]["reports"]}
    plan.update(status="running", started_at=plan.get("started_at", utc_now()))
    active_model = None
    for job in plan["jobs"]:
        if job["result"] is not None:
            continue
        if job["state"] == "running":
            raise ValueError("An interrupted run needs inspection before rerunning; retained artifacts are in plan.json")
        c = replace(config, llm=replace(config.llm, model=job["model"], reasoning_effort=job.get("reasoning_effort")))
        client = replace(ChatClient.from_config(c.llm), timing_path=directory / "warmup_timings.jsonl")
        if active_model != job["model"]:
            start = perf_counter()
            warmup = {"model": job["model"], "started_at": utc_now()}
            try:
                client.complete_json(system_prompt='Return the JSON object {"ok":true}.', user_payload={"probe": "availability"},
                    response_schema={"name": "benchmark_availability", "strict": True, "schema": {
                        "type": "object", "properties": {"ok": {"type": "boolean", "enum": [True]}},
                        "required": ["ok"], "additionalProperties": False}},
                    validate=validate_probe)
                warmup["status"] = "pass"
            except Exception as exc:
                warmup.update(status="failed", error=str(exc))
            warmup["wall_seconds"] = round(perf_counter() - start, 3)
            plan["warmups"].append(warmup)
            active_model = job["model"]
        report = reports[job["report"]]
        paths = [Path(p) for p in report["inputs"]]
        manifest = create_run(paths[0].parent, ROOT / "results", c, input_paths=paths, doc_id=job["report"])
        job.update(state="running", artifact_dir=manifest["artifact_dir"], started_at=utc_now())
        write_json(directory / "plan.json", plan)
        print(f"START {job['blind_id']} {job['report']} {job['model']} repetition={job['repetition']}", flush=True)
        start = perf_counter()
        execute_run(job["artifact_dir"], c, client=client)
        run_dir = Path(job["artifact_dir"])
        job.update(state="completed", result=run_metrics(run_dir, perf_counter() - start), finished_at=utc_now())
        packet = {"blind_id": job["blind_id"], "report": job["report"], "candidate": None,
                  "source": read_json(run_dir / "source.json") if (run_dir / "source.json").exists() else None}
        if (run_dir / "candidate_report.json").exists():
            packet["candidate"] = {"rows": read_json(run_dir / "candidate_report.json")["rows"]}
        if (run_dir / "translated.json").exists():
            packet["translation"] = read_json(run_dir / "translated.json")
        write_json(directory / "review" / (job["blind_id"] + ".json"), packet)
        write_json(directory / "plan.json", plan)
        summarize(directory)
        print(f"DONE {job['blind_id']} {job['result']['status']} {job['result']['wall_seconds']}s", flush=True)
    plan.update(status="completed", finished_at=utc_now())
    write_json(directory / "plan.json", plan)
    summarize(directory)


def validate_probe(payload):
    if payload != {"ok": True}:
        raise ValueError("Invalid availability probe")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("models")
    prep = commands.add_parser("prepare")
    prep.add_argument("spec", type=Path)
    prep.add_argument("directory", type=Path)
    for name in ("run", "summarize"):
        commands.add_parser(name).add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "models":
        print(json.dumps(available_models(), indent=2))
    elif args.command == "prepare":
        plan = prepare(read_json(args.spec), args.directory)
        write_csv(args.directory / "ratings.csv", [{"blind_id": j["blind_id"], "report": j["report"],
                  **{f: "" for f in RATINGS}, "notes": ""} for j in sorted(plan["jobs"], key=lambda j: j["blind_id"])],
                  ["blind_id", "report", *RATINGS, "notes"])
        summarize(args.directory)
        print(f"Prepared {len(plan['jobs'])} runs in {args.directory}")
    elif args.command == "run":
        execute(args.directory)
    else:
        print(json.dumps(summarize(args.directory), indent=2))


if __name__ == "__main__":
    main()
