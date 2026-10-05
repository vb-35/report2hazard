from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib import error, request

from .config import LLMConfig
from .core import parse_json_object
from .errors import PipelineError


def append_timing(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), **record}, ensure_ascii=False) + "\n")


def extract_response_json(payload: dict[str, Any]) -> Any:
    for candidate in (payload, payload.get("result")):
        if isinstance(candidate, dict) and candidate:
            if "choices" not in candidate and "output" not in candidate:
                return candidate
    for choice in payload.get("choices", []):
        message = choice.get("message", {})
        parsed = message.get("parsed")
        if isinstance(parsed, dict):
            return parsed
    for item in payload.get("output", []):
        for part in item.get("content", []):
            parsed = part.get("parsed") if isinstance(part, dict) else None
            if isinstance(parsed, dict):
                return parsed
    try:
        return parse_json_object(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise PipelineError(f"unexpected chat completion payload: {payload}") from exc


@dataclass
class ChatClient:
    api_key: str
    config: LLMConfig
    timing_path: Path | None = None

    @classmethod
    def from_config(cls, config: LLMConfig) -> "ChatClient":
        if not config.api_base_url:
            raise PipelineError("missing required environment variable TW_LLM_API_BASE_URL")
        return cls(api_key=config.api_key_from_env(), config=config)

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Any,
        validate: Any,
        response_schema: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        retry_prompt = system_prompt
        task = response_schema["name"] if response_schema else "unstructured_json"
        input_chars = len(json.dumps(user_payload, ensure_ascii=True))
        chunks = user_payload.get("chunks", user_payload.get("source_chunks", user_payload.get("fragments", []))) if isinstance(user_payload, dict) else []
        segment_rows = user_payload.get("segments", user_payload.get("batch_segments", [])) if isinstance(user_payload, dict) else []
        if isinstance(user_payload, dict) and "candidate_report" in user_payload:
            segment_rows = user_payload["candidate_report"].get("rows", [])
        chunk_sizes = [len(chunk.get("text", "")) for chunk in chunks]
        for attempt in range(1, self.config.retries + 1):
            started = perf_counter()
            prompt_chars = len(retry_prompt)
            network_seconds = 0.0
            response_chars = 0
            outcome = "pass"
            failure: Exception | None = None
            try:
                network_started = perf_counter()
                payload = self._post(
                    system_prompt=retry_prompt,
                    user_payload=user_payload,
                    response_schema=response_schema,
                )
                network_seconds = perf_counter() - network_started
                response_chars = len(json.dumps(payload, ensure_ascii=False))
                parsed = extract_response_json(payload)
            except Exception as exc:  # ponytail: fixed retry loop, add backoff only if the endpoint proves flaky.
                network_seconds = network_seconds or perf_counter() - network_started
                last_error = exc
                outcome = "request_error"
                failure = exc
            else:
                try:
                    validate(parsed)
                except Exception as exc:
                    last_error = exc
                    outcome = "validation_error"
                    failure = exc
                    retry_prompt = (
                        f"{system_prompt}\n\nYour previous response failed validation: {exc}. "
                        "Return a corrected JSON object matching the response schema exactly."
                    )
            if self.timing_path is not None:
                append_timing(self.timing_path, {
                    "type": "llm_call", "task": task, "attempt": attempt, "outcome": outcome,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "network_seconds": round(network_seconds, 3),
                    "timeout_seconds": self.config.timeout_seconds,
                    "prompt_chars": prompt_chars, "input_chars": input_chars,
                    "response_chars": response_chars, "chunk_count": len(chunks),
                    "chunk_text_chars": sum(chunk_sizes), "max_chunk_text_chars": max(chunk_sizes, default=0),
                    "segment_count": len(segment_rows),
                    "error_type": type(failure).__name__ if failure else None,
                })
            if failure is None:
                return parsed
        raise PipelineError(f"model response validation failed after {self.config.retries} attempts: {last_error}") from last_error

    def _post(self, *, system_prompt: str, user_payload: Any, response_schema: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self.config.api_base_url.rstrip('/')}{self.config.text_endpoint}"
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
            ],
        }
        if response_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": response_schema}
        body = json.dumps(payload)
        if len(body) > self.config.max_request_chars:
            raise PipelineError(
                f"chat request contains {len(body)} characters, exceeding max_request_chars="
                f"{self.config.max_request_chars}; use a smaller report or adjust the configured limit"
            )
        req = request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise PipelineError(f"chat completion HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise PipelineError(f"chat completion request failed: {exc.reason}") from exc
