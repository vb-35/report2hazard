"""Claude API (Messages) transport for ChatClient.

ChatClient builds one provider-neutral chat payload (system, user, optional repair turns, response schema). This
module turns it into a Messages API request and returns the answer in the chat-completions shape that
`extract_response_json` already reads, so retries, repairs, validation and timing logs are shared by both providers.
"""
from __future__ import annotations

from typing import Any

from .errors import PipelineError

DEFAULT_MAX_TOKENS = 64000  # the Messages API requires max_tokens; streaming keeps long answers within HTTP timeouts
# Server-side refusal fallbacks (beta). "default" lets the API pick the fallback model by refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Structured outputs reject numeric, string-length and array-size constraints. The pipeline validators still check
# every answer against the full schema, so these are removed only from what is sent.
UNSUPPORTED_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
                        "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties"}
SUPPORTED_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
# Error types worth another attempt. A stream that fails after HTTP 200 reports only the type in its error event.
TRANSIENT_ERRORS = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}
# Prompt caching. Payload keys that are identical across the calls of one task (e.g. every segment placement of a
# run) are sent first, after the system prompt, and cached for an hour: segment calls are several minutes apart.
# The rest of the payload follows the images and is cached for 5 minutes, which serves validation retries and repairs.
SHARED_PAYLOAD_KEYS = ("candidates", "methods", "point_tokens", "spatial_preflight", "acquisition_providers",
                       "frame", "segments", "unresolved_quoted_names", "unresolved_names", "providers")
LONG_CACHE = {"type": "ephemeral", "ttl": "1h"}
SHORT_CACHE = {"type": "ephemeral"}


def _error_type(body: Any) -> str | None:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error.get("type")
        return body.get("type") if body.get("type") != "error" else None
    return None


def _sdk():
    try:
        import anthropic
    except ImportError as exc:
        raise PipelineError("the Anthropic provider needs the anthropic package: "
                            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt") from exc
    return anthropic


def schema_for_claude(schema: Any) -> Any:
    """Copy a JSON schema without the keywords structured outputs reject; every object gets additionalProperties false."""
    if isinstance(schema, list):
        return [schema_for_claude(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in UNSUPPORTED_KEYWORDS or (key == "format" and value not in SUPPORTED_FORMATS):
            continue
        if key in {"properties", "$defs", "definitions", "patternProperties"} and isinstance(value, dict):
            result[key] = {name: schema_for_claude(item) for name, item in value.items()}
        else:
            result[key] = schema_for_claude(value)
    # Structured outputs allow at most 16 union-typed fields. A field that is one type or null is sent as that type
    # and made optional instead; restore_nulls() puts the null back when the model leaves it out.
    nullable = [name for name, item in (schema.get("properties") or {}).items() if _nullable_type(item)]
    if nullable and isinstance(result.get("required"), list):
        result["required"] = [name for name in result["required"] if name not in nullable]
    types = result.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types) or "properties" in result:
        result["additionalProperties"] = False
    if isinstance(types, list):
        result = _split_types(result)
    return result


OBJECT_KEYWORDS = {"properties", "required", "additionalProperties"}
ARRAY_KEYWORDS = {"items"}
JSON_TYPES = {"string": lambda v: isinstance(v, str), "null": lambda v: v is None,
              "boolean": lambda v: isinstance(v, bool),
              "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
              "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list)}


def _nullable_type(node: Any) -> str | None:
    """The single non-null type of a {"type": [X, "null"]} schema, else None."""
    types = node.get("type") if isinstance(node, dict) else None
    if isinstance(types, list) and "null" in types:
        others = [kind for kind in types if kind != "null"]
        if len(others) == 1:
            return others[0]
    return None


def restore_nulls(value: Any, schema: Any) -> Any:
    """Put back the nulls the model left out for fields that schema_for_claude made optional."""
    if not isinstance(schema, dict):
        return value
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        for name, item in schema["properties"].items():
            if name not in value and _nullable_type(item):
                value[name] = None
            elif name in value:
                value[name] = restore_nulls(value[name], item)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [restore_nulls(item, schema["items"]) for item in value]
    return value


def _split_types(node: dict) -> dict:
    """{"type": ["string", "null"], "enum": [...]} -> anyOf branches, one per type (structured outputs reject the list
    form together with enum). Enum values and type-specific keywords go to the matching branch."""
    node = dict(node)
    types = node.pop("type")
    enum = node.pop("enum", None)
    single = [kind for kind in types if kind != "null"]
    if "null" in types and len(single) == 1:
        types = single  # nullable: sent as the plain type of an optional field (see schema_for_claude)
    keywords = {key: node.pop(key) for key in OBJECT_KEYWORDS | ARRAY_KEYWORDS if key in node}
    branches = []
    for kind in types:
        branch: dict[str, Any] = {"type": kind}
        if enum is not None:
            values = [value for value in enum if JSON_TYPES.get(kind, lambda v: False)(value)]
            if not values:
                continue
            if kind != "null":
                branch["enum"] = values
        if kind == "object":
            branch.update({k: v for k, v in keywords.items() if k in OBJECT_KEYWORDS})
            branch["additionalProperties"] = False
        elif kind == "array":
            branch.update({k: v for k, v in keywords.items() if k in ARRAY_KEYWORDS})
        branches.append(branch)
    if not branches:
        node["type"] = "null"
    elif len(branches) == 1:
        node.update(branches[0])
    else:
        node["anyOf"] = branches
    return node


def _image_block(url: str) -> dict[str, Any]:
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def request_params(chat: dict[str, Any], *, model: str, effort: str | None,
                   images: list[str] | None = None) -> dict[str, Any]:
    """Messages API parameters for a chat-completions payload built by ChatClient._request_body."""
    turns = chat["messages"]
    if not turns or turns[0]["role"] != "system":
        raise PipelineError("chat payload must start with the system prompt")
    messages = [{"role": turn["role"], "content": turn["content"]} for turn in turns[1:]]
    # Stable shared context first (cached for an hour), then images, then the call-specific task text (cached for
    # retries). Images still precede the task text, as the Messages API recommends.
    shared, rest = _split_payload(messages[0]["content"])
    messages[0]["content"] = [
        *([{"type": "text", "text": shared, "cache_control": LONG_CACHE}] if shared else []),
        *(_image_block(url) for url in images or ()),
        {"type": "text", "text": rest, "cache_control": SHORT_CACHE},
    ]
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": chat.get("max_tokens") or DEFAULT_MAX_TOKENS,
        "system": [{"type": "text", "text": turns[0]["content"], "cache_control": LONG_CACHE}],
        "messages": messages,
    }
    output_config: dict[str, Any] = {}
    response_format = chat.get("response_format")
    if response_format:
        output_config["format"] = {"type": "json_schema",
                                   "schema": schema_for_claude(response_format["json_schema"]["schema"])}
    if effort:
        # OpenAI-style "minimal"/"none" map to the lowest Claude effort.
        output_config["effort"] = effort if effort in EFFORTS else "low"
    if output_config:
        params["output_config"] = output_config
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def _split_payload(text: str) -> tuple[str, str]:
    """Split a compact JSON object into its shared keys and the rest, both compact JSON in the original key order."""
    import json

    from .payloads import compact_json

    try:
        payload = json.loads(text)
    except ValueError:
        return "", text
    if not isinstance(payload, dict):
        return "", text
    shared = {k: v for k, v in payload.items() if k in SHARED_PAYLOAD_KEYS}
    if not shared:
        return "", text
    rest = {k: v for k, v in payload.items() if k not in SHARED_PAYLOAD_KEYS}
    return ("Shared context (the same for every call of this task): " + compact_json(shared),
            "This call: " + compact_json(rest))


def as_chat_completion(message: Any) -> dict[str, Any]:
    """Represent a Messages API answer as the chat-completions payload ChatClient parses."""
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
    refusal = None
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        refusal = getattr(details, "explanation", None) or getattr(details, "category", None) or "refused"
    usage = message.usage.to_dict() if hasattr(message.usage, "to_dict") else dict(message.usage or {})
    return {
        "id": message.id,
        "model": message.model,
        "provider": "anthropic",
        "choices": [{
            "finish_reason": STOP_REASONS.get(message.stop_reason, message.stop_reason),
            "message": {"role": "assistant", "content": text, "refusal": refusal},
        }],
        "usage": usage,
    }


class AnthropicTransport:
    """One SDK client per ChatClient. SDK retries are off: ChatClient owns retries and time limits."""

    def __init__(self, api_key: str | None):
        anthropic = _sdk()
        self._sdk_module = anthropic
        from .config import setting_from_env

        # A key that is not scoped to one workspace must name the workspace on every request.
        workspace = setting_from_env("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self._client = anthropic.Anthropic(api_key=api_key or None, max_retries=0, default_headers=headers)

    def send(self, params: dict[str, Any], *, timeout_seconds: float, request_error: type,
             schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request. A schema the API finds too complex to enforce is given in the prompt instead."""
        key = _schema_key(schema)
        if key in _PROMPT_ONLY_SCHEMAS:
            params = _schema_in_prompt(params, schema)
        try:
            return self._send(params, timeout_seconds=timeout_seconds, request_error=request_error)
        except request_error as exc:
            if key is None or key in _PROMPT_ONLY_SCHEMAS or not _schema_too_complex(str(exc)):
                raise
            _PROMPT_ONLY_SCHEMAS.add(key)
            return self._send(_schema_in_prompt(params, schema), timeout_seconds=timeout_seconds,
                              request_error=request_error)

    def _send(self, params: dict[str, Any], *, timeout_seconds: float, request_error: type) -> dict[str, Any]:
        anthropic = self._sdk_module
        client = self._client.with_options(timeout=timeout_seconds)
        # Fallbacks are a beta parameter, so those requests go through the beta namespace.
        api = client.beta.messages if "betas" in params else client.messages
        try:
            with api.stream(**params) as stream:
                return as_chat_completion(stream.get_final_message())
        except anthropic.APIStatusError as exc:
            code = exc.status_code
            kind = _error_type(exc.body)
            retryable = code in {408, 409, 425, 429} or code >= 500 or kind in TRANSIENT_ERRORS
            where = f"HTTP {code}" if code != 200 else "stream error"
            raise request_error(f"Anthropic API {where} ({kind or 'unknown error'}): {exc.message}", retryable=retryable,
                                retry_after=_retry_after_header(exc.response)) from exc
        except anthropic.APITimeoutError as exc:
            raise request_error(f"Anthropic API request timed out: {exc}", retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise request_error(f"Anthropic API request failed: {exc}", retryable=True) from exc


# Schemas the API refused to compile (too many unions or optional fields, "too complex"). Requests with them describe
# the schema in the system prompt; the pipeline still validates every answer against the full schema and repairs it.
_PROMPT_ONLY_SCHEMAS: set[str] = set()
SCHEMA_LIMIT_MARKERS = ("schema is too complex", "too many parameters with union types", "too many optional",
                        "exponential compilation cost")


def _schema_key(schema: dict[str, Any] | None) -> str | None:
    if not schema:
        return None
    import hashlib
    import json

    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()


def _schema_too_complex(message: str) -> bool:
    lowered = message.lower()
    return "invalid_request_error" in lowered and any(marker in lowered for marker in SCHEMA_LIMIT_MARKERS)


def _schema_in_prompt(params: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    import json

    params = dict(params)
    output_config = {k: v for k, v in (params.get("output_config") or {}).items() if k != "format"}
    if output_config:
        params["output_config"] = output_config
    else:
        params.pop("output_config", None)
    # After the call-specific text, not in the system prompt: schemas differ per call (e.g. per segment) and would
    # otherwise break the cached system prompt and shared context.
    instruction = ("Return only one JSON object, with no other text, that is valid against this JSON Schema (use null "
                   "where the schema allows it and nothing applies):\n" + json.dumps(schema["schema"], ensure_ascii=False))
    messages = [dict(m) for m in params["messages"]]
    content = messages[0]["content"]
    messages[0]["content"] = ([*content, {"type": "text", "text": instruction}] if isinstance(content, list)
                              else [{"type": "text", "text": content}, {"type": "text", "text": instruction}])
    params["messages"] = messages
    return params


def _retry_after_header(response: Any) -> float | None:
    from .llm import _retry_after

    return _retry_after(getattr(response, "headers", None))
