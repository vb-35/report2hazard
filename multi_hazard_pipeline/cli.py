from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG
from .human_review import approve_run, reject_run, request_correction
from .llm import ChatClient
from .pipeline import run_pipeline
from .splitter import split_event_reports


EXIT_CODES = {
    "approved": 0,
    "awaiting_human_review": 3,
    "revision_required": 4,
    "rejected": 5,
    "failed": 1,
}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="python -m multi_hazard_pipeline")
    subcommands = command.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run through whole-report self-evaluation")
    run.add_argument("input_dir")
    run.add_argument("output_dir", nargs="?", default="results")
    split = subcommands.add_parser("split", help="split a PDF collection into event-report PDFs")
    split.add_argument("input_pdf")
    split.add_argument("output_dir", nargs="?", default="results/split")
    approve = subcommands.add_parser("approve", help="approve an evaluation-passed candidate")
    approve.add_argument("artifact_dir")
    approve.add_argument("--comment", default="")
    reject = subcommands.add_parser("reject", help="reject a candidate")
    reject.add_argument("artifact_dir")
    reject.add_argument("--comment", default="")
    correct = subcommands.add_parser("correct", help="request a targeted correction")
    correct.add_argument("artifact_dir")
    correct.add_argument(
        "--stage", choices=("translation", "segmentation", "categorization"), required=True
    )
    correct.add_argument("--segments", default="", help="comma-separated segment IDs")
    correct.add_argument("--comment", default="")
    serve = subcommands.add_parser("serve", help="start the local human-review interface")
    serve.add_argument("output_dir", nargs="?", default="results")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=5000, type=int)
    return command


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if len(raw) == 2 and raw[0] not in {"run", "split", "approve", "reject", "correct", "serve"}:
        raw = ["run", *raw]
    args = parser().parse_args(raw)
    if args.command == "run":
        result = run_pipeline(args.input_dir, args.output_dir)
    elif args.command == "split":
        try:
            paths = split_event_reports(
                Path(args.input_pdf),
                Path(args.output_dir),
                ChatClient.from_config(DEFAULT_CONFIG.llm),
            )
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for path in paths:
            print(path)
        return 0
    elif args.command == "approve":
        result = approve_run(args.artifact_dir, global_comment=args.comment)
    elif args.command == "reject":
        result = reject_run(args.artifact_dir, global_comment=args.comment)
    elif args.command == "correct":
        segment_ids = [int(item) for item in args.segments.split(",") if item.strip()]
        result = request_correction(
            args.artifact_dir,
            requested_stage=args.stage,
            segment_ids=segment_ids,
            global_comment=args.comment,
        )
    else:
        from .web import create_app

        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        create_app(args.output_dir).run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=True))
    if result.get("artifact_dir"):
        print(f"Run artifacts: {result['artifact_dir']}")
        human_path = Path(result["artifact_dir"]) / "human_review.json"
        if human_path.exists():
            print(f"Human review: {human_path}")
    return EXIT_CODES.get(result.get("status"), 1)
