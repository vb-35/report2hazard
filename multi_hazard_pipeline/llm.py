from __future__ import annotations

import json
import math
import re
import ssl
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from threading import Thread
from time import perf_counter, sleep
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit

from .config import LLMConfig
from .errors import PipelineError
from .payloads import compact_json, model_payload


JSON_ESCAPING_RULE = r'''Inside JSON strings, escape double quotes as \", backslashes as \\, and line breaks as \n. Never emit literal control characters. Use plain punctuation in generated descriptions where possible; preserve required labels and evidence content.'''


def append_timing(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), **record}, ensure_ascii=False) + "\n")


def extract_response_json(payload: dict[str, Any]) -> Any:
    if not isinstance(payload, dict):
        raise ValueError("completion payload must be an object")
    if payload.get("error") or payload.get("status") in {"failed", "incomplete", "cancelled", "queued", "in_progress"}:
        raise ValueError(f"completion failed or is incomplete (status={payload.get('status')!r})")
    for choice in payload.get("choices", []):
        if choice.get("finish_reason") not in {None, "stop"} or choice.get("message", {}).get("refusal"):
            raise ValueError(f"completion did not finish successfully (finish_reason={choice.get('finish_reason')!r}, "
                             f"refusal={choice.get('message', {}).get('refusal')!r})")
    for item in payload.get("output", []):
        if item.get("status") in {"failed", "incomplete", "cancelled", "queued", "in_progress"}:
            raise ValueError(f"completion output is incomplete (status={item['status']!r})")
        if any(isinstance(part, dict) and part.get("type") == "refusal" for part in item.get("content", [])):
            raise ValueError("completion output contains a refusal")
    for candidate in (payload.get("result"), payload):
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
        raw = payload["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
        # Require the entire answer to be JSON; never salvage a valid prefix of partial output.
        return json.loads(raw)
    except (KeyError, IndexError, TypeError) as exc:
        raise PipelineError("unexpected chat completion payload: missing JSON answer") from exc


class _RequestError(PipelineError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after(headers: Any) -> float | None:
    if not headers:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("Retry-After", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                seconds = (date.replace(tzinfo=date.tzinfo or UTC) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds):
            return max(0.0, seconds)
    return None


def _failed_answer(payload: Any) -> str:
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                if isinstance(message.get("content"), str):
                    return message["content"]
                if "parsed" in message:
                    return compact_json(message["parsed"])
    return compact_json(payload)


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
        timeout_seconds: float | None = None,
        total_timeout_seconds: float | None = None,
    ) -> Any:
        last_error: Exception | None = None
        if self.config.retries < 1:
            raise PipelineError("chat request configuration requires at least one attempt")
        for limit in (timeout_seconds, total_timeout_seconds):
            if limit is not None and (not math.isfinite(limit) or limit <= 0):
                raise PipelineError("chat time limits must be finite and positive")
        deadline = perf_counter() + total_timeout_seconds if total_timeout_seconds is not None else None
        user_payload = model_payload(user_payload)
        repair = None
        task = response_schema["name"] if response_schema else "unstructured_json"
        input_chars = len(compact_json(user_payload))
        chunks = user_payload.get("chunks", user_payload.get("source_chunks", user_payload.get("fragments", []))) if isinstance(user_payload, dict) else []
        segment_rows = user_payload.get("segments", user_payload.get("batch_segments", [])) if isinstance(user_payload, dict) else []
        if isinstance(user_payload, dict) and "candidate_report" in user_payload:
            segment_rows = user_payload["candidate_report"].get("rows", [])
        chunk_sizes = [len(chunk.get("text", "")) for chunk in chunks]
        for attempt in range(1, self.config.retries + 1):
            started = perf_counter()
            request_timeout = self.config.timeout_seconds
            if timeout_seconds is not None:
                request_timeout = min(request_timeout, timeout_seconds)
            if deadline is not None:
                request_timeout = min(request_timeout, deadline - started)
            prompt_chars = len(system_prompt)
            network_seconds = 0.0
            response_chars = 0
            payload = None
            failed_answer = ""
            outcome = "pass"
            failure: Exception | None = None
            retryable = False
            retry_delay = 0.0
            network_started = perf_counter()
            try:
                if deadline is not None and request_timeout <= 0:
                    raise _RequestError(f"{task} exceeded its {total_timeout_seconds:g}-second total time limit")
                post = self._post_with_timeout if timeout_seconds is not None or deadline is not None else self._post
                payload = post(
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    response_schema=response_schema,
                    repair=repair,
                    timeout_seconds=request_timeout,
                )
                network_seconds = perf_counter() - network_started
                response_chars = len(json.dumps(payload, ensure_ascii=False))
                failed_answer = _failed_answer(payload)
                if isinstance(payload, dict) and payload.get("error"):
                    raise _RequestError(f"chat completion endpoint error: {compact_json(payload['error'])}")
                outcome = "parsing_error"
                parsed = extract_response_json(payload)
                outcome = "validation_error"
                validate(parsed)
                if deadline is not None and perf_counter() >= deadline:
                    raise _RequestError(f"{task} exceeded its {total_timeout_seconds:g}-second total time limit")
            except Exception as exc:
                network_seconds = network_seconds or perf_counter() - network_started
                last_error = exc
                failure = exc
                if isinstance(exc, _RequestError):
                    # Validators can make nested model calls (citation verification).
                    outcome = "request_error"
                    retryable = exc.retryable
                    retry_delay = exc.retry_after if exc.retry_after is not None else min(2 ** (attempt - 1), 8)
                elif outcome == "pass":
                    outcome = "parsing_error" if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else "request_error"
                    if isinstance(exc, json.JSONDecodeError):
                        failed_answer = exc.doc
                        response_chars = len(exc.doc)
                    elif isinstance(exc, UnicodeDecodeError):
                        failed_answer = exc.object.decode("utf-8", errors="replace")
                        response_chars = len(failed_answer)
                if outcome in {"parsing_error", "validation_error"}:
                    retryable = True
                    repair = {"answer": failed_answer, "error": f"{type(exc).__name__}: {exc}", "kind": outcome}
            else:
                outcome = "pass"
            if failure is not None and retryable and deadline is not None:
                remaining = deadline - perf_counter()
                if remaining <= retry_delay:
                    last_error = failure = _RequestError(
                        f"{task} exhausted its {total_timeout_seconds:g}-second total time budget "
                        f"(including retry delays): {failure}"
                    )
                    outcome = "request_error"
                    retryable = False
            if self.timing_path is not None:
                append_timing(self.timing_path, {
                    "type": "llm_call", "task": task, "attempt": attempt, "outcome": outcome,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "network_seconds": round(network_seconds, 3),
                    "timeout_seconds": max(0.0, request_timeout),
                    "total_timeout_seconds": total_timeout_seconds,
                    "prompt_chars": prompt_chars, "input_chars": input_chars,
                    "response_chars": response_chars, "chunk_count": len(chunks),
                    "chunk_text_chars": sum(chunk_sizes), "max_chunk_text_chars": max(chunk_sizes, default=0),
                    "segment_count": len(segment_rows),
                    "error_type": type(failure).__name__ if failure else None,
                    "error": str(failure) if failure else None,
                    "usage": payload.get("usage") if isinstance(payload, dict) else None,
                    "retry_delay_seconds": retry_delay if retryable and attempt < self.config.retries else None,
                })
            if failure is None:
                return parsed
            if not retryable or attempt == self.config.retries:
                break
            if retry_delay:
                sleep(retry_delay)
        exception_type = _RequestError if outcome == "request_error" else PipelineError
        raise exception_type(f"model {outcome} failed after {attempt} attempt(s): {last_error}") from last_error

    def _post_with_timeout(self, *, timeout_seconds: float, **kwargs: Any) -> dict[str, Any]:
        # A socket timeout alone does not bound DNS, connection setup and a slowly arriving body together.
        result: Future = Future()

        def send() -> None:
            try:
                result.set_result(self._post(timeout_seconds=timeout_seconds, **kwargs))
            except Exception as exc:
                result.set_exception(exc)

        # shortcut: timed-out requests can finish in the background; use cancellable transport if they accumulate.
        Thread(target=send, daemon=True).start()
        try:
            return result.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            raise _RequestError(f"chat completion exceeded its {timeout_seconds:g}-second attempt time limit",
                                retryable=True) from exc

    def _request_body(
        self, *, system_prompt: str, user_payload: Any, response_schema: dict[str, Any] | None,
        repair: dict[str, str] | None = None,
    ) -> str:
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + JSON_ESCAPING_RULE},
                {"role": "user", "content": compact_json(model_payload(user_payload))},
            ],
        }
        if response_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": response_schema}
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        body = compact_json(payload)
        if len(body) > self.config.max_request_chars:
            raise _RequestError(
                f"chat request contains {len(body)} characters, exceeding max_request_chars="
                f"{self.config.max_request_chars}; use a smaller report or adjust the configured limit"
            )
        if repair:
            feedback = (
                f"The previous answer failed {repair['kind']}: {repair['error']}\n"
                "Correct the failed answer using the original task and evidence above. "
                "Return only a complete JSON object matching the required schema."
            )
            answer = {"role": "assistant", "content": repair["answer"]}
            payload["messages"].extend([answer, {"role": "user", "content": feedback}])
            body = compact_json(payload)
            if len(body) > self.config.max_request_chars:
                # Only the faulty answer may be shortened; task, evidence, schema and error remain intact.
                low, high = 0, len(repair["answer"])
                answer["content"] = "[failed answer truncated]"
                if len(compact_json(payload)) > self.config.max_request_chars:
                    raise _RequestError("chat repair request exceeds max_request_chars="
                                        f"{self.config.max_request_chars}; original context and error cannot fit")
                while low < high:
                    middle = (low + high + 1) // 2
                    answer["content"] = repair["answer"][:middle] + "\n[failed answer truncated]"
                    if len(compact_json(payload)) <= self.config.max_request_chars:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    raise _RequestError("chat repair request exceeds max_request_chars="
                                        f"{self.config.max_request_chars}; no faulty-answer context fits")
                answer["content"] = repair["answer"][:low] + "\n[failed answer truncated]"
                body = compact_json(payload)
        return body

    def _post(
        self, *, system_prompt: str, user_payload: Any, response_schema: dict[str, Any] | None,
        repair: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.api_base_url.rstrip('/')}{self.config.text_endpoint}"
        try:
            address = urlsplit(url)
            if address.scheme not in {"http", "https"} or not address.hostname:
                raise ValueError("API URL must use HTTP(S) and include a host")
            address.port  # Validate malformed ports before entering the retry loop.
            if not self.api_key.strip() or not self.config.model.strip():
                raise ValueError("API key and model must be non-empty")
            if self.config.timeout_seconds <= 0 or self.config.max_request_chars < 1:
                raise ValueError("timeout and request-size limit must be positive")
        except ValueError as exc:
            raise _RequestError(f"chat request configuration error: {exc}") from exc
        body = self._request_body(system_prompt=system_prompt, user_payload=user_payload,
                                  response_schema=response_schema, repair=repair)
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
            with request.urlopen(req, timeout=self.config.timeout_seconds if timeout_seconds is None else timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {408, 409, 425, 429} or (500 <= exc.code < 600 and exc.code not in {501, 505})
            try:
                problem = json.loads(detail).get("error", {})
                if isinstance(problem, dict) and problem.get("code") in {
                    "insufficient_quota", "billing_hard_limit_reached", "invalid_api_key", "model_not_found",
                    "unsupported_model", "unsupported_parameter",
                }:
                    retryable = False
            except (ValueError, AttributeError):
                pass
            raise _RequestError(f"chat completion HTTP {exc.code}: {detail}", retryable=retryable,
                                retry_after=_retry_after(exc.headers)) from exc
        except error.URLError as exc:
            retryable = not isinstance(exc.reason, (ValueError, ssl.SSLError))
            raise _RequestError(f"chat completion request failed: {exc.reason}", retryable=retryable) from exc
        except (TimeoutError, ConnectionError, IncompleteRead) as exc:
            raise _RequestError(f"chat completion request failed: {exc}", retryable=True) from exc
