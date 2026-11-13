import argparse
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from multi_hazard_pipeline import anthropic_provider as ap
from multi_hazard_pipeline.config import DEFAULT_CONFIG, LLMConfig
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.llm import ChatClient, extract_response_json


SCHEMA = {"name": "answer", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["rows"],
    "properties": {"rows": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
        "type": "object", "required": ["id", "label"],
        "properties": {"id": {"type": "integer", "minimum": 1}, "label": {"type": "string", "minLength": 1},
                       "when": {"type": "string", "format": "date"}, "ref": {"type": "string", "format": "x-custom"}}}}}}}


def test_provider_defaults_and_switch(monkeypatch):
    monkeypatch.delenv("TW_LLM_PROVIDER", raising=False)
    openai = LLMConfig(provider="openai", api_base_url="http://router/v1")
    assert openai.model == "nvidia/Qwen3.8-27B-NVFP4" and openai.api_key_env_var == "TW_LLM_API_KEY"
    claude = openai.with_provider("anthropic")
    assert (claude.provider, claude.model, claude.api_key_env_var) == ("anthropic", "claude-opus-5-5", "ANTHROPIC_API_KEY")
    assert openai.with_provider("anthropic", "claude-sonnet-5-5").model == "claude-sonnet-5-5"
    assert openai.with_provider(None, "other").model == "other"
    assert claude.with_provider("openai").model == "nvidia/Qwen3.8-27B-NVFP4"
    with pytest.raises(ValueError):
        LLMConfig(provider="gemini")


def test_provider_from_environment(monkeypatch):
    monkeypatch.setenv("TW_LLM_PROVIDER", "Anthropic")
    assert LLMConfig().provider == "anthropic" and LLMConfig().model == "claude-opus-5-5"


def test_anthropic_key_is_optional_but_openai_key_is_required(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "TW_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("multi_hazard_pipeline.config.setting_from_env", lambda name: "")
    assert LLMConfig(provider="anthropic").api_key_from_env() == ""  # the SDK finds a token or `ant` profile itself
    with pytest.raises(PipelineError):
        LLMConfig(provider="openai").api_key_from_env()


def test_schema_drops_unsupported_constraints_only():
    converted = ap.schema_for_claude(SCHEMA["schema"])
    item = converted["properties"]["rows"]["items"]
    assert "minItems" not in converted["properties"]["rows"] and "maxItems" not in converted["properties"]["rows"]
    assert item["additionalProperties"] is False
    assert item["properties"]["id"] == {"type": "integer"}
    assert item["properties"]["label"] == {"type": "string"}
    assert item["properties"]["when"] == {"type": "string", "format": "date"}
    assert item["properties"]["ref"] == {"type": "string"}
    assert SCHEMA["schema"]["properties"]["rows"]["minItems"] == 1  # the original stays intact for validation


def test_pipeline_schemas_convert_cleanly():
    from multi_hazard_pipeline import schemas
    from multi_hazard_pipeline.vision import VISION_SCHEMA

    found = [schemas.segment_response_schema(), schemas.consolidation_response_schema(),
             schemas.citation_verification_response_schema(), schemas.translation_response_schema(),
             schemas.event_separation_response_schema(), schemas.classification_response_schema(DEFAULT_CONFIG),
             schemas.review_response_schema(DEFAULT_CONFIG), VISION_SCHEMA]

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    for schema in found:
        converted = ap.schema_for_claude(schema["schema"])
        assert not set(keys(converted)) & ap.UNSUPPORTED_KEYWORDS, schema["name"]


def _client(**changes):
    config = replace(LLMConfig(provider="anthropic"), **changes)
    return ChatClient(api_key="", config=config)


def test_request_params_translate_chat_payload():
    client = _client(max_output_tokens=24000, reasoning_effort="minimal")
    chat = json.loads(client._request_body(system_prompt="Task", user_payload={"a": 1}, response_schema=SCHEMA,
                                           repair={"answer": "{bad", "error": "JSONDecodeError", "kind": "parsing_error"},
                                           images=["data:image/png;base64,AAAA"]))
    params = ap.request_params(chat, model="claude-opus-5-5", effort="minimal",
                               images=["data:image/png;base64,AAAA"])
    assert params["system"][0]["text"].startswith("Task") and params["max_tokens"] == 24000
    assert params["system"][0]["cache_control"] == ap.LONG_CACHE
    assert "temperature" not in params
    assert [m["role"] for m in params["messages"]] == ["user", "assistant", "user"]
    first = params["messages"][0]["content"]
    assert first[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert first[1]["type"] == "text" and first[1]["text"] == '{"a":1}'  # no shared keys: sent unchanged
    assert first[1]["cache_control"] == ap.SHORT_CACHE
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["fallbacks"] == "default" and params["betas"] == [ap.FALLBACK_BETA]
    no_fallback = ap.request_params(chat, model="claude-haiku-5-5", effort=None)
    assert "fallbacks" not in no_fallback and "effort" not in no_fallback["output_config"]


def test_anthropic_rejects_non_data_images():
    with pytest.raises(PipelineError):
        _client()._request_body(system_prompt="T", user_payload={}, response_schema=None, images=["http://x/y.png"])


def _message(text, stop_reason="end_turn", details=None):
    return SimpleNamespace(id="msg_1", model="claude-opus-5-5", stop_reason=stop_reason, stop_details=details,
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(to_dict=lambda: {"input_tokens": 10, "output_tokens": 5}))


def test_answers_map_to_chat_completion_shape():
    assert extract_response_json(ap.as_chat_completion(_message('{"ok": true}'))) == {"ok": True}
    with pytest.raises(ValueError, match="length"):
        extract_response_json(ap.as_chat_completion(_message('{"ok"', "max_tokens")))
    refused = ap.as_chat_completion(_message("", "refusal", SimpleNamespace(explanation="declined", category="cyber")))
    with pytest.raises(ValueError, match="declined"):
        extract_response_json(refused)


def test_chat_client_uses_anthropic_transport_with_repairs(monkeypatch, tmp_path):
    sent = []

    class FakeTransport:
        def __init__(self, api_key):
            self.api_key = api_key

        def send(self, params, *, timeout_seconds, request_error, schema=None):
            sent.append(params)
            text = '{"rows": []}' if len(sent) == 1 else '{"rows": [{"id": 1, "label": "x"}]}'
            return ap.as_chat_completion(_message(text))

    monkeypatch.setattr(ap, "AnthropicTransport", FakeTransport)
    client = _client()
    client.timing_path = tmp_path / "timings.jsonl"

    def validate(answer):
        if not answer["rows"]:
            raise ValueError("at least one row")

    result = client.complete_json(system_prompt="Task", user_payload={"x": 1}, validate=validate, response_schema=SCHEMA)
    assert result == {"rows": [{"id": 1, "label": "x"}]}
    assert len(sent) == 2 and sent[1]["messages"][-1]["role"] == "user"  # second call carried the repair turn
    timings = [json.loads(line) for line in client.timing_path.read_text(encoding="utf-8").splitlines()]
    assert [t["outcome"] for t in timings] == ["validation_error", "pass"]
    assert timings[0]["provider"] == "anthropic" and timings[1]["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_from_config_needs_base_url_only_for_openai(monkeypatch):
    monkeypatch.setenv("TW_LLM_API_KEY", "k")
    with pytest.raises(PipelineError):
        ChatClient.from_config(LLMConfig(provider="openai", api_base_url=""))
    assert ChatClient.from_config(LLMConfig(provider="anthropic", api_base_url="")).config.provider == "anthropic"


def test_default_config_keeps_self_hosted_provider(monkeypatch):
    # The repository default is unchanged unless TW_LLM_PROVIDER says otherwise.
    if DEFAULT_CONFIG.llm.provider == "openai":
        assert DEFAULT_CONFIG.llm.model == "nvidia/Qwen3.8-27B-NVFP4"


@pytest.mark.parametrize("runner", ["claude", "codex"])
@pytest.mark.parametrize("full_access", [False, True])
def test_agent_commands(monkeypatch, tmp_path, runner, full_access):
    from segment_mapping import agent_launch

    monkeypatch.setattr(agent_launch.shutil, "which", lambda name: f"/bin/{name}")
    a = argparse.Namespace(runner=runner, model="m1", effort="high", full_access=full_access)
    run_dir = tmp_path / "runs" / "event_m1"
    command, env = agent_launch.build_command(a, run_dir, tmp_path / "reports" / "r.pdf", run_dir / "agent" / "f.txt")
    assert command[0] == f"/bin/{runner}"
    if runner == "claude":
        assert command[1] == "-p" and "--model" in command and "--effort" in command
        mode = command[command.index("--permission-mode") + 1]
        assert mode == ("bypassPermissions" if full_access else "auto")
        assert str(run_dir) in command and str(tmp_path / "reports") in command
        assert str(tmp_path / "runs") not in command  # never the parent, which may hold other runs
        assert env == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
    else:
        assert command[1] == "exec" and command[-1] == "-" and "--ephemeral" in command
        assert ("--dangerously-bypass-approvals-and-sandbox" in command) == full_access
        assert ("sandbox_workspace_write.network_access=true" in command) == (not full_access)
        assert "model_reasoning_effort=high" in command


def test_agent_dry_run_writes_prompt(monkeypatch, tmp_path):
    from segment_mapping import agent_launch

    monkeypatch.setattr(agent_launch.shutil, "which", lambda name: f"/bin/{name}")
    a = argparse.Namespace(runner="codex", model=None, effort=None, full_access=False, dry_run=True,
                           run=str(tmp_path / "run"), report=str(tmp_path / "r.pdf"))
    assert agent_launch.launch(a, "PROMPT")["exit_code"] == 0
    assert (tmp_path / "run" / "agent" / "prompt.txt").read_text(encoding="utf-8") == "PROMPT"
    (tmp_path / "run" / "run.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        agent_launch.launch(a, "PROMPT")


def _status_error(status, body):
    import anthropic
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.APIStatusError("failed", response=response, body=body)


@pytest.mark.parametrize("status,body,retryable", [
    (200, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, True),  # mid-stream
    (200, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, False),
    (529, {"type": "error", "error": {"type": "overloaded_error"}}, True),
    (400, {"type": "error", "error": {"type": "invalid_request_error"}}, False),
    (429, None, True),
])
def test_stream_and_http_errors_are_classified(status, body, retryable):
    from multi_hazard_pipeline.llm import _RequestError

    transport = ap.AnthropicTransport.__new__(ap.AnthropicTransport)
    transport._sdk_module = ap._sdk()

    class Failing:
        def stream(self, **params):
            raise _status_error(status, body)

    client = SimpleNamespace(messages=Failing(), beta=SimpleNamespace(messages=Failing()))
    transport._client = SimpleNamespace(with_options=lambda **kw: client)
    with pytest.raises(_RequestError) as caught:
        transport.send({"model": "claude-opus-5-5"}, timeout_seconds=5, request_error=_RequestError)
    assert caught.value.retryable is retryable


def test_launcher_stops_the_agent_when_recording_fails(monkeypatch, tmp_path):
    import sys
    from segment_mapping import agent_launch

    monkeypatch.setattr(agent_launch, "build_command",
                        lambda *args: ([sys.executable, "-c", "import time; time.sleep(60)"], {}))
    started = []

    def on_start(process):
        started.append(process)
        raise OSError("manifest could not be written")

    a = argparse.Namespace(runner="claude", model=None, effort=None, full_access=False, dry_run=False,
                           run=str(tmp_path / "run"), report=str(tmp_path / "r.pdf"))
    with pytest.raises(OSError):
        agent_launch.launch(a, "PROMPT", log=lambda m: None, on_start=on_start)
    assert started and started[0].poll() is not None  # stopped and reaped, not left running


def test_workspace_header_is_sent_when_configured(monkeypatch):
    monkeypatch.setattr("multi_hazard_pipeline.config.setting_from_env",
                        lambda name: "wrkspc_123" if name == "ANTHROPIC_WORKSPACE_ID" else "")
    transport = ap.AnthropicTransport("sk-test")
    assert transport._client.default_headers.get("anthropic-workspace-id") == "wrkspc_123"
    monkeypatch.setattr("multi_hazard_pipeline.config.setting_from_env", lambda name: "")
    assert "anthropic-workspace-id" not in ap.AnthropicTransport("sk-test")._client.default_headers


def test_claude_agent_uses_its_own_sign_in_not_an_api_key(monkeypatch, tmp_path):
    import subprocess
    from segment_mapping import agent_launch

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr(agent_launch, "build_command", lambda *args: (["agent"], {}))
    seen = {}

    class Fake:
        pid, stdin, stdout = 1, None, iter(())

        def __init__(self, command, **kwargs):
            seen.update(kwargs["env"])
            self.stdin = type("In", (), {"write": lambda s, t: None, "close": lambda s: None})()

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", Fake)
    a = argparse.Namespace(runner="claude", model=None, effort=None, full_access=False, dry_run=False,
                           run=str(tmp_path / "run"), report=str(tmp_path / "r.pdf"))
    agent_launch.launch(a, "PROMPT", log=lambda m: None)
    assert "ANTHROPIC_API_KEY" not in seen


def test_nullable_enums_become_any_of():
    converted = ap.schema_for_claude({"type": "object", "properties": {
        "candidate": {"type": ["string", "null"], "enum": ["S1", "S2", None], "description": "pick one"},
        "side": {"type": ["string", "null"], "enum": ["left", None]},
        "count": {"type": ["integer", "null"], "minimum": 0},
        "only_null": {"type": ["string", "null"], "enum": [None]}}, "required": ["candidate"]})
    props = converted["properties"]
    # Nullable fields are sent as their plain type and become optional (unions are capped at 16 per schema).
    assert props["candidate"] == {"description": "pick one", "type": "string", "enum": ["S1", "S2"]}
    assert props["count"] == {"type": "integer"}
    assert converted["required"] == []
    mixed = ap.schema_for_claude({"type": ["string", "integer", "null"], "enum": ["a", 1, None]})
    assert mixed == {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "integer", "enum": [1]}, {"type": "null"}]}
    original = {"type": "object", "properties": {"a": {"type": ["string", "null"]}, "rows": {"type": "array", "items": {
        "type": "object", "properties": {"b": {"type": ["integer", "null"]}, "c": {"type": "string"}}}}}}
    assert ap.restore_nulls({"rows": [{"c": "x"}, {"b": 2, "c": "y"}]}, original) ==         {"a": None, "rows": [{"b": None, "c": "x"}, {"b": 2, "c": "y"}]}

    def type_lists(node):
        if isinstance(node, dict):
            return isinstance(node.get("type"), list) or any(type_lists(v) for v in node.values())
        if isinstance(node, list):
            return any(type_lists(v) for v in node)
        return False

    assert not type_lists(converted)


def test_too_complex_schema_is_described_in_the_prompt_instead(monkeypatch):
    from multi_hazard_pipeline.llm import _RequestError

    monkeypatch.setattr(ap, "_PROMPT_ONLY_SCHEMAS", set())
    transport = ap.AnthropicTransport.__new__(ap.AnthropicTransport)
    sent = []

    def fake_send(params, *, timeout_seconds, request_error):
        sent.append(params)
        if "format" in (params.get("output_config") or {}):
            raise request_error("Anthropic API HTTP 400 (invalid_request_error): Schema is too complex.")
        return ap.as_chat_completion(_message('{"rows": []}'))

    transport._send = fake_send
    params = {"model": "m", "system": [{"type": "text", "text": "Task"}],
              "messages": [{"role": "user", "content": [{"type": "text", "text": "This call: {}"}]}], "max_tokens": 10,
              "output_config": {"format": {"type": "json_schema", "schema": {}}, "effort": "low"}}
    transport.send(params, timeout_seconds=5, request_error=_RequestError, schema=SCHEMA)
    assert len(sent) == 2
    assert sent[1]["output_config"] == {"effort": "low"}
    schema_text = sent[1]["messages"][0]["content"][-1]["text"]  # after the call text: the system prompt stays cached
    assert '"minItems": 1' in schema_text  # the full original schema, constraints included
    transport.send(params, timeout_seconds=5, request_error=_RequestError, schema=SCHEMA)
    assert len(sent) == 3 and "format" not in sent[2]["output_config"]  # remembered: no second failed attempt
    other = dict(SCHEMA, name="other")

    def bad_request(params, **kwargs):
        raise _RequestError("Anthropic API HTTP 400 (invalid_request_error): messages: field required")

    transport._send = bad_request
    with pytest.raises(_RequestError):  # other 400s are not mistaken for schema limits
        transport.send(params, timeout_seconds=5, request_error=_RequestError, schema=other)
