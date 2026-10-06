"""Focused offline client checks. Run with python test_llm.py."""

import json
import socket
import ssl
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib import error, request

from multi_hazard_pipeline import human_review, llm, pipeline
from multi_hazard_pipeline.agents.review_agent import CHECK_NAMES
from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.core import read_json
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.payloads import compact_json
from test_regressions import SmokeClient, blocked_network


TASK = {"system_prompt": "Use only the supplied evidence; return a complete answer.",
        "user_payload": {"chunks": [{"chunk_id": "c1", "text": "Überflutung reached the bridge."}]},
        "response_schema": {"name": "offline_answer", "strict": True,
                            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                       "required": ["ok"], "additionalProperties": False}}}
USAGE = {"prompt_tokens": 73, "completion_tokens": 12, "total_tokens": 85}


def validate(answer):
    if answer != {"ok": True}:
        raise ValueError("ok must be true and no fields may be missing or added")


def completion(answer, **extra):
    return {"choices": [{"finish_reason": "stop", "message": {"content": answer}}], "usage": USAGE, **extra}


class Response:
    def __init__(self, payload):
        self.body = payload if isinstance(payload, bytes) else compact_json(payload).encode("utf-8")

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.body


def http_failure(status, headers=None, detail=None):
    fields = Message()
    for name, value in (headers or {}).items():
        fields[name] = value
    return error.HTTPError("https://offline.invalid/chat/completions", status, "offline failure", fields,
                           BytesIO(compact_json(detail or {"error": {"message": "offline failure"}}).encode()))


def client(root, name, **config):
    return llm.ChatClient("offline", replace(DEFAULT_CONFIG.llm, **{"api_base_url": "https://offline.invalid", **config}),
                          root / f"{name}.jsonl")


def timings(model):
    return [json.loads(line) for line in model.timing_path.read_text(encoding="utf-8").splitlines()]


def check_request_failures(root):
    for status in (400, 401, 403, 404, 413, 422, 501, 505):
        model = client(root, f"permanent-{status}")
        with (patch.object(request, "urlopen", side_effect=http_failure(status)) as endpoint,
              patch.object(llm, "sleep") as sleep):
            with TestCase().assertRaisesRegex(PipelineError, f"request_error.*1 attempt.*HTTP {status}"):
                model.complete_json(**TASK, validate=validate)
            assert endpoint.call_count == 1 and not sleep.called
        assert [row["outcome"] for row in timings(model)] == ["request_error"]

    # Even a 429 is permanent when the provider explicitly reports exhausted billing quota.
    model = client(root, "quota")
    with patch.object(request, "urlopen", side_effect=http_failure(429, detail={"error": {"code": "insufficient_quota"}})) as endpoint:
        with TestCase().assertRaises(PipelineError):
            model.complete_json(**TASK, validate=validate)
        assert endpoint.call_count == 1

    for name, settings in (("oversized", {"max_request_chars": 50}),
                           ("bad-url", {"api_base_url": "file:///wrong"}),
                           ("bad-port", {"api_base_url": "https://offline.invalid:wrong"}),
                           ("bad-model", {"model": ""}), ("bad-timeout", {"timeout_seconds": 0})):
        model = client(root, name, **settings)
        with patch.object(request, "urlopen", blocked_network), patch.object(llm, "sleep") as sleep:
            with TestCase().assertRaisesRegex(PipelineError, "request_error.*1 attempt"):
                model.complete_json(**TASK, validate=validate)
            assert len(timings(model)) == 1 and not sleep.called

    model = client(root, "bad-key")
    model.api_key = ""
    with patch.object(request, "urlopen", blocked_network):
        with TestCase().assertRaisesRegex(PipelineError, "configuration error"):
            model.complete_json(**TASK, validate=validate)

    for status, headers, delay in ((429, {"Retry-After": "2.5"}, 2.5),
                                   (503, {"retry-after-ms": "250"}, .25), (502, {}, 1)):
        model = client(root, f"transient-{status}")
        with (patch.object(request, "urlopen", side_effect=[http_failure(status, headers), Response(completion('{"ok":true}'))]) as endpoint,
              patch.object(llm, "sleep") as sleep):
            assert model.complete_json(**TASK, validate=validate) == {"ok": True}
            assert endpoint.call_count == 2
            sleep.assert_called_once_with(delay)
            assert endpoint.call_args_list[0].args[0].data == endpoint.call_args_list[1].args[0].data
        assert [row["outcome"] for row in timings(model)] == ["request_error", "pass"]
        assert timings(model)[1]["usage"] == USAGE

    with patch.object(llm, "datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 6, tzinfo=UTC)
        assert llm._retry_after({"Retry-After": "Tue, 06 Oct 2026 00:00:07 GMT"}) == 7
        assert llm._retry_after({"Retry-After": "invalid"}) is None
        assert llm._retry_after({"retry-after-ms": "NaN", "Retry-After": "3"}) == 3

    for name, failure, calls in (("timeout", TimeoutError("offline timeout"), 2),
                                 ("short-read", IncompleteRead(b"partial", 10), 2),
                                 ("connection", error.URLError(ConnectionResetError("offline reset")), 2),
                                 ("url-error", error.URLError("temporary connection failure"), 2),
                                 ("certificate", error.URLError(ssl.SSLCertVerificationError("bad certificate")), 1)):
        model = client(root, name)
        with (patch.object(request, "urlopen", side_effect=[failure, Response(completion('{"ok":true}'))]) as endpoint,
              patch.object(llm, "sleep")):
            if calls == 2:
                assert model.complete_json(**TASK, validate=validate) == {"ok": True}
            else:
                with TestCase().assertRaises(PipelineError):
                    model.complete_json(**TASK, validate=validate)
            assert endpoint.call_count == calls

    for limit in (1, 2, 3):
        model = client(root, f"retry-limit-{limit}", retries=limit)
        with (patch.object(request, "urlopen", side_effect=[http_failure(503) for _ in range(limit)]) as endpoint,
              patch.object(llm, "sleep") as sleep):
            with TestCase().assertRaisesRegex(PipelineError, f"request_error.*{limit} attempt"):
                model.complete_json(**TASK, validate=validate)
            assert endpoint.call_count == limit and sleep.call_count == limit - 1
        assert len(timings(model)) == limit


def check_repairs(root):
    faulty = '{"ok":false}'
    model = client(root, "repair")
    task_before = deepcopy(TASK)

    def mutating_validate(answer):
        if not answer["ok"]:
            answer["mutated"] = True
            raise ValueError("ok is false; must be true")
        validate(answer)

    with patch.object(request, "urlopen", side_effect=[Response(completion(faulty)), Response(completion('{"ok":true}'))]) as endpoint:
        assert model.complete_json(**TASK, validate=mutating_validate) == {"ok": True}
    sent = [json.loads(call.args[0].data) for call in endpoint.call_args_list]
    assert TASK == task_before
    assert sent[1]["messages"][:2] == sent[0]["messages"]
    assert sent[1]["response_format"] == sent[0]["response_format"]
    assert sent[1]["messages"][2] == {"role": "assistant", "content": faulty}
    assert "ValueError: ok is false; must be true" in sent[1]["messages"][3]["content"]
    assert [row["outcome"] for row in timings(model)] == ["validation_error", "pass"]
    assert all(row["usage"] == USAGE for row in timings(model))

    model = client(root, "bounded-repair", max_request_chars=1300)
    faulty = '{"ok":false,"noise":' + json.dumps('"\\\nÜ' * 1000) + '}'
    with patch.object(request, "urlopen", side_effect=[Response(completion(faulty)), Response(completion('{"ok":true}'))]) as endpoint:
        assert model.complete_json(**TASK, validate=validate) == {"ok": True}
    original, repaired = [json.loads(call.args[0].data) for call in endpoint.call_args_list]
    assert all(len(call.args[0].data.decode("utf-8")) <= 1300 for call in endpoint.call_args_list)
    assert repaired["messages"][:2] == original["messages"] and repaired["response_format"] == original["response_format"]
    assert repaired["messages"][2]["content"].startswith('{"ok":false')
    assert repaired["messages"][2]["content"].endswith("[failed answer truncated]")
    assert "ok must be true" in repaired["messages"][3]["content"]

    # If feedback cannot fit, fail locally without dropping evidence or submitting an oversize repair.
    model = client(root, "no-repair-room")
    model.config = replace(model.config, max_request_chars=len(model._request_body(**TASK)))
    with patch.object(request, "urlopen", return_value=Response(completion('{"ok":false}'))) as endpoint:
        with TestCase().assertRaisesRegex(PipelineError, "request_error.*original context and error cannot fit"):
            model.complete_json(**TASK, validate=validate)
        assert endpoint.call_count == 1

    # Mixed failures share the original total-attempt budget and retain repair context across a network retry.
    model = client(root, "mixed-limit", retries=3)
    with (patch.object(request, "urlopen", side_effect=[Response(completion('{"ok":false}')), http_failure(503),
                                                        Response(completion('{"ok":false}'))]) as endpoint,
          patch.object(llm, "sleep") as sleep):
        with TestCase().assertRaisesRegex(PipelineError, "validation_error.*3 attempt"):
            model.complete_json(**TASK, validate=validate)
        assert endpoint.call_count == 3 and sleep.call_count == 1
        assert endpoint.call_args_list[1].args[0].data == endpoint.call_args_list[2].args[0].data


def check_parsing_and_partial_output(root):
    malformed = '{"ok":tru'
    partial = completion('{"ok":true}')
    partial["choices"][0]["finish_reason"] = "length"
    for name, faulty in (("parsing", completion(malformed)), ("bad-envelope", b'{"choices":'),
                         ("trailing-partial", completion('{"ok":true}{"unfinished":')),
                         ("partial", partial), ("failed", completion('{"ok":true}', status="failed")),
                         ("pending", completion('{"ok":true}', status="in_progress")),
                         ("incomplete-output", {"output": [{"content": [{"parsed": {"ok": True}}]},
                                                            {"status": "incomplete", "content": []}]})):
        model = client(root, name)
        with patch.object(request, "urlopen", side_effect=[Response(faulty), Response(completion('{"ok":true}'))]) as endpoint:
            assert model.complete_json(**TASK, validate=validate) == {"ok": True}
        assert [row["outcome"] for row in timings(model)] == ["parsing_error", "pass"]
        feedback = json.loads(endpoint.call_args_list[1].args[0].data)["messages"]
        assert feedback[2]["content"] and "parsing_error" in feedback[3]["content"]

    for name, response, outcome in (("invalid-limit", completion('{"ok":false}'), "validation_error"),
                                     ("partial-limit", partial, "parsing_error"),
                                     ("error-envelope", {"error": {"code": "invalid_api_key"}}, "request_error")):
        model = client(root, name, retries=2)
        with patch.object(request, "urlopen", return_value=Response(response)) as endpoint:
            with TestCase().assertRaisesRegex(PipelineError, outcome):
                model.complete_json(**TASK, validate=validate)
            assert endpoint.call_count == (1 if outcome == "request_error" else 2)

    # A permanent failure inside a validator's nested citation call must not restart the parent call.
    model = client(root, "nested")
    with patch.object(request, "urlopen", side_effect=[Response(completion('{"ok":true}')), http_failure(401)]) as endpoint:
        with TestCase().assertRaisesRegex(PipelineError, "request_error"):
            model.complete_json(**TASK, validate=lambda answer: model.complete_json(**TASK, validate=validate))
        assert endpoint.call_count == 2


def check_human_timing_and_failed_save(root):
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "report.txt").write_text("Heavy rainfall mobilized sediment into the channel.", encoding="utf-8")
    manifest = pipeline.run_pipeline(inputs, root / "runs", client=SmokeClient())
    assert manifest["status"] == "awaiting_human_review", manifest["errors"]
    run = Path(manifest["artifact_dir"])
    model = replace(client(root, "unused"), timing_path=None)
    review = {"status": "pass", "summary": "Offline review", "issues": [], "checks": dict.fromkeys(CHECK_NAMES, True)}

    def answer(req, **kwargs):
        body = json.loads(req.data)
        if body["response_format"]["json_schema"]["name"] == "whole_report_evaluation":
            result = review
        else:
            data = json.loads(body["messages"][1]["content"])
            rows = read_json(run / "classified.json")["rows"]
            result = {"rows": [{key: row[key] for key in ("segment", "generalized_category", "interaction_type",
                                                         "sediment_transport_phase", "classification_rationale")}
                               for row in rows if row["segment"] in data["requested_segment_ids"]]}
        return Response(completion(compact_json(result)))

    with patch.object(request, "urlopen", answer):
        assert human_review.apply_candidate_edits(run, [{"segment": 1, "field": "interaction_type",
                                                         "new_value": "Process-topography"}], client=model)["status"] == "awaiting_human_review"
        assert human_review.request_correction(run, requested_stage="categorization", segment_ids=[1],
                                               client=model)["status"] == "awaiting_human_review"
    rows = [json.loads(line) for line in (run / "timings.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [row for row in rows if row["type"] == "llm_call"]
    assert [row["task"] for row in calls] == ["whole_report_evaluation", "classified_segments", "whole_report_evaluation"]
    assert all(row["usage"] == USAGE and row["outcome"] == "pass" for row in calls)
    assert model.timing_path is None

    original_history = read_json(run / "self_evaluation.json")
    with patch.object(request, "urlopen", side_effect=http_failure(401)):
        with TestCase().assertRaises(PipelineError):
            human_review.apply_candidate_edits(run, [{"segment": 1, "field": "interaction_type",
                                                     "new_value": "Process-process"}], client=model)
    assert read_json(run / "manifest.json")["status"] == "failed"
    assert read_json(run / "self_evaluation.json") == original_history
    with TestCase().assertRaises(PipelineError):
        human_review.approve_run(run)
    assert not (run / "final_rows.json").exists() and not (run / "final_rows.csv").exists()

    # Invalid model output cannot become a valid saved segmentation or candidate.
    with patch.object(request, "urlopen", return_value=Response(completion('{"segments":[]}'))):
        failed = pipeline.run_pipeline(inputs, root / "invalid-runs", client=model)
    assert failed["status"] == "failed"
    failed_dir = Path(failed["artifact_dir"])
    assert not (failed_dir / "segments.json").exists() and not (failed_dir / "candidate_report.json").exists()


def main():
    with (TemporaryDirectory(prefix="hazard-llm-") as directory,
          patch.object(request.OpenerDirector, "open", blocked_network),
          patch.object(socket.socket, "connect", blocked_network),
          patch.object(socket.socket, "connect_ex", blocked_network)):
        root = Path(directory)
        check_request_failures(root)
        check_repairs(root)
        check_parsing_and_partial_output(root)
        check_human_timing_and_failed_save(root)
    print("Offline LLM checks passed: permanent/transient failures, bounded retries, repair context/size, usage, timing and save gates.")


if __name__ == "__main__":
    main()
